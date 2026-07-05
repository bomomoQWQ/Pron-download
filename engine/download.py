"""
下载模块 — Playwright + aria2 (WinTLS) 统一视频下载

所有下载统一走 aria2：
  - HLS/m3u8 → 解析分片列表，aria2 多线程下载 → 二进制合并
  - 直链 mp4  → aria2 直接下载

aria2 在 Windows 上使用 WinTLS (SCHANNEL)，与 Edge/Chrome 共享系统 TLS 栈，
绕过 CDN 指纹检测。避免 curl_cffi 的 OpenSSL DLL 兼容性问题。
"""
import asyncio
import shutil
from pathlib import Path
from typing import Optional

from .db import (
    create_download_task, update_task_progress, get_task_status,
    is_downloaded, record_download
)


# 服务配置（由 app.py 在启动时注入）
SERVICE_CONFIG: dict = {}


class DownloadConfig:
    """下载配置"""
    def __init__(
        self,
        download_dir: str = "./downloads",
        max_concurrent: int = 3,
        timeout: int = 600,
        resume: bool = True,
        rate_limit: int = 0,
        proxy: str = "",
    ):
        self.download_dir = Path(download_dir)
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.max_concurrent = max_concurrent
        self.timeout = timeout
        self.resume = resume
        self.rate_limit = rate_limit
        self.proxy = proxy
        self._semaphore = asyncio.Semaphore(max_concurrent)


# 全局下载配置（由 app.py 初始化）
download_config: Optional[DownloadConfig] = None


# ============ 视频详情获取 ============

async def fetch_video_details(video_id: str, timeout: int = 30) -> Optional[dict]:
    """
    获取视频 embed 页的元数据（画质列表等），同时返回浏览器 Cookie 供后续下载使用。

    返回格式: (info_dict, cookies_list) 或 (None, None)
    """
    import json as json_module
    from engine.search import build_page

    page = await build_page()
    try:
        base = SERVICE_CONFIG.get("site_base_url", "")
        embed_path = SERVICE_CONFIG.get("embed_path", "/embed/{video_id}")
        embed_url = base + embed_path.format(video_id=video_id)

        try:
            await page.goto(embed_url, wait_until="networkidle", timeout=timeout * 1000)
        except Exception:
            await page.goto(embed_url, wait_until="load", timeout=timeout * 1000)

        html = await page.content()
        cookies = await page.context.cookies()

        # 括号匹配提取 flashvars JSON
        start = html.find("flashvars")
        if start == -1:
            return None, None

        brace_start = html.find("{", start)
        if brace_start == -1:
            return None, None

        depth = 0
        i = brace_start
        while i < len(html):
            if html[i] == "{":
                depth += 1
            elif html[i] == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1

        if depth != 0:
            return None, None

        json_str = html[brace_start:i + 1]
        flashvars = json_module.loads(json_str)

        qualities = {}
        for media_def in flashvars.get("mediaDefinitions", []):
            url = media_def.get("videoUrl", "")
            quality = media_def.get("quality", "")
            fmt = media_def.get("format", "")

            if not url:
                continue
            if not quality:
                if fmt == "mp4":
                    quality = "direct"
                else:
                    continue

            qualities[quality] = url

        info = {
            "video_id": video_id,
            "title": flashvars.get("video_title", ""),
            "duration": int(flashvars.get("video_duration", 0)),
            "qualities": qualities,
            "thumbnail_url": flashvars.get("image_url", ""),
            "link_url": flashvars.get("link_url", ""),
            "views": 0,
            "rating": float(flashvars.get("rating", 0)),
        }
        return info, cookies
    finally:
        await page.close()


# ============ 下载入口 ============

async def download_video(
    video_id: str,
    quality: str = "best",
    serve_host: str = "localhost",
    serve_port: int = 8765,
    serve_mode: str = "http",
) -> dict:
    """下载视频（异步，不阻塞 MCP 工具返回）"""
    if download_config is None:
        raise RuntimeError("下载模块未初始化，请先设置 download_config")

    # 检查是否已下载
    if await is_downloaded(video_id, quality):
        records = await _find_downloaded(video_id, quality)
        if records:
            file_path = Path(records["file_path"])
            if file_path.exists():
                return {
                    "task_id": "cached",
                    "status": "completed",
                    "download_url": _build_download_url(file_path.name, serve_host, serve_port, serve_mode),
                    "file_path": str(file_path),
                    "file_size": records.get("file_size", 0),
                    "cached": True,
                }

    task_id = await create_download_task(video_id, quality)
    asyncio.create_task(_do_download(task_id, video_id, quality))
    return {
        "task_id": task_id,
        "status": "queued",
        "download_url": None,
        "message": "下载任务已加入队列，请通过 download_status 查询进度",
    }


async def _find_downloaded(video_id: str, quality: str) -> Optional[dict]:
    """查找已下载的记录"""
    from .db import get_db
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT * FROM download_records WHERE video_id = ? AND quality = ?",
            (video_id, quality)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None
    finally:
        await db.close()


async def _do_download(task_id: str, video_id: str, quality: str):
    """实际执行下载：判断直链/HLS，走对应引擎"""
    config = download_config

    try:
        await update_task_progress(task_id, "fetching", 0)

        # 1. 获取画质 URL（同时拿 Cookie）
        result = await fetch_video_details(video_id)
        if not result or not result[0]:
            await update_task_progress(task_id, "failed", 0, error="无法获取视频信息")
            return

        details, browser_cookies = result
        if not details.get("qualities"):
            await update_task_progress(task_id, "failed", 0, error="视频无可用画质")
            return

        # 2. 选择画质
        video_url = _select_quality(details["qualities"], quality)
        if not video_url:
            await update_task_progress(
                task_id, "failed", 0,
                error=f"未找到画质 {quality}，可用画质: {list(details['qualities'].keys())}"
            )
            return

        # 3. 判断下载方式并执行
        is_hls = "m3u8" in video_url.lower() or "hls" in video_url.lower()

        if is_hls:
            downloaded_size, filepath = await _download_aria2(
                task_id, video_id, quality, video_url, config, browser_cookies)
        else:
            downloaded_size, filepath = await _download_direct(
                task_id, video_id, quality, video_url, config, browser_cookies)

        # 4. 完成
        await update_task_progress(task_id, "completed", 100, str(filepath), downloaded_size)
        await record_download(video_id, quality, str(filepath), downloaded_size)

    except Exception as e:
        await update_task_progress(task_id, "failed", 0, error=str(e))


# ============ 直链下载 (Playwright 解析 URL → aria2 下载) ============

async def _download_direct(task_id: str, video_id: str, quality: str,
                           video_url: str, config: DownloadConfig,
                           cookies: list[dict]) -> tuple[int, Path]:
    """
    直链 mp4 下载 — 尝试 aria2 (WinTLS)，失败则回退 Playwright 浏览器下载。

    PornHub direct 画质通过 /video/get_media 端点流式返回 MP4。
    该端点托管在 www.pornhub.com，在某些网络下 aria2 可能无法直连。
    """
    filename = f"{video_id}_{quality}.mp4"
    out = config.download_dir / filename

    cookie_str = "; ".join(
        f"{c['name']}={c['value']}" for c in cookies
        if c.get("name") and c.get("value")
    )
    ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

    await update_task_progress(task_id, "downloading", 10)

    # 尝试 1: aria2 (15 秒超时)
    cmd = [
        "aria2c", "--check-certificate=false",
        "--header", f"Cookie: {cookie_str}",
        "--header", f"User-Agent: {ua}",
        "--header", "Referer: https://www.pornhub.com/",
        "--header", "Origin: https://www.pornhub.com",
        "-x1", "-t1", "-o", filename,
        "-d", str(config.download_dir),
        "--timeout=15", "--max-tries=1",
        "--allow-overwrite=true", "--console-log-level=warn",
        video_url,
    ]

    try:
        proc = await asyncio.create_subprocess_exec(*cmd,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        await asyncio.wait_for(proc.communicate(), timeout=20)
        if out.exists():
            return out.stat().st_size, out
    except Exception:
        pass  # aria2 失败，尝试浏览器方案

    # 尝试 2: Playwright 浏览器下载
    from engine.search import build_page
    import json as _json

    page = await build_page()
    try:
        # 先导航到 embed 页建立 cookie 上下文
        embed_url = (SERVICE_CONFIG.get("site_base_url", "")
                     + SERVICE_CONFIG.get("embed_path", "/embed/{video_id}").format(video_id=video_id))
        await page.goto(embed_url, wait_until="domcontentloaded", timeout=15000)

        safe_url = _json.dumps(video_url)
        async with page.expect_download(timeout=config.timeout * 1000) as di:
            await page.evaluate(f"window.location.href = {safe_url}")
        download = await di.value
        await download.save_as(str(out))
    finally:
        await page.close()

    if not out.exists():
        raise RuntimeError("直链下载失败：aria2 和浏览器方案均未成功")

    return out.stat().st_size, out


# ============ M3U8 解析 ============

def _parse_m3u8_segments(content: str, base_url: str) -> list[str]:
    """
    从变体(mp4 variant) m3u8 播放列表中提取所有 .ts 分片 URL。

    注意：仅处理变体播放列表（已由上游完成 master→variant 解析），
    不做递归下载，所以不依赖浏览器外的 HTTP 客户端。
    """
    segments = []
    base_dir = base_url.rsplit("/", 1)[0]

    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("http"):
            segments.append(line)
        else:
            segments.append(f"{base_dir}/{line}")

    return segments


# ============ 状态查询 ============

async def get_status(task_id: str) -> Optional[dict]:
    """查询下载任务状态"""
    task = await get_task_status(task_id)
    if task is None:
        return None

    result = dict(task)
    if task["status"] == "completed" and task.get("file_path"):
        file_name = Path(task["file_path"]).name
        host = SERVICE_CONFIG.get("serve_host", "localhost")
        port = SERVICE_CONFIG.get("serve_port", 8765)
        result["download_url"] = _build_download_url(
            file_name, host, port, SERVICE_CONFIG.get("serve_mode", "http")
        )

    return result


# ============ aria2 下载引擎 ============

async def _download_aria2(
    task_id: str, video_id: str, quality: str,
    m3u8_url: str, config: DownloadConfig,
    cookies: list[dict],
) -> tuple[int, Path]:
    """
    Playwright 解析 HLS 链 → aria2 多线程下载分片 → 二进制合并

    为什么用 aria2：
      - aria2 在 Windows 上使用 WinTLS (SCHANNEL)，
        与 Edge/Chrome 共享系统 TLS 栈，CDN 无法区分。
      - curl_cffi/httpx 用的 OpenSSL/BoringSSL 被 CDN 拦截。
    """
    from engine.search import build_page

    filename = f"{video_id}_{quality}"
    out_dir = config.download_dir
    tmp_dir = out_dir / f"_aria2_{video_id}_{quality}"
    tmp_dir.mkdir(exist_ok=True)

    cookie_str = "; ".join(
        f"{c['name']}={c['value']}" for c in cookies
        if c.get("name") and c.get("value")
    )
    ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

    await update_task_progress(task_id, "fetching", 5)

    # 1. Playwright 解析 m3u8 链获取所有分片 URL
    page = await build_page()
    try:
        embed_url = f"https://www.pornhub.com/embed/{video_id}"
        await page.goto(embed_url, wait_until="networkidle", timeout=30000)

        m3u8_text = await page.evaluate("""async () => {
            const html = document.documentElement.outerHTML;
            const m = html.match(/flashvars\\s*=\\s*(\\{[^;]+\\})/);
            const fv = JSON.parse(m[1]);
            for (const md of (fv.mediaDefinitions||[])) {
                if (md.videoUrl && md.videoUrl.includes("m3u8")) {
                    try { const r = await fetch(md.videoUrl); return await r.text(); } catch(e) {}
                }
            }
            return null;
        }""")

        if not m3u8_text:
            raise RuntimeError("无法获取 m3u8")

        # 主播放列表 → 变体播放列表
        if "#EXT-X-STREAM-INF" in m3u8_text:
            for line in m3u8_text.splitlines():
                s = line.strip()
                if s and not s.startswith("#"):
                    base = m3u8_url.rsplit("/", 1)[0]
                    vurl = s if s.startswith("http") else f"{base}/{s}"
                    m3u8_text = await page.evaluate(
                        f"async () => {{ try {{ const r = await fetch('{vurl}'); return await r.text(); }} catch(e) {{ return null; }} }}")
                    break

        if not m3u8_text:
            raise RuntimeError("无法获取变体 m3u8")

        segments = _parse_m3u8_segments(m3u8_text, m3u8_url)
        if not segments:
            raise RuntimeError("无分片")
    finally:
        await page.close()

    # 2. 写 URL 列表给 aria2 批量下载
    url_file = tmp_dir / "urls.txt"
    url_file.write_text("\n".join(segments), encoding="utf-8")

    await update_task_progress(task_id, "downloading", 10)

    cmd = [
        "aria2c",
        "--check-certificate=false",
        "--header", f"Cookie: {cookie_str}",
        "--header", f"User-Agent: {ua}",
        "--header", "Referer: https://www.pornhub.com/",
        "--header", "Origin: https://www.pornhub.com",
        "-x", str(config.max_concurrent),
        "-j", str(config.max_concurrent),
        "-i", str(url_file),
        "-d", str(tmp_dir),
        "--timeout", str(config.timeout),
        "--max-tries=5",
        "--retry-wait=3",
        "--auto-file-renaming=false",
        "--allow-overwrite=true",
        "--console-log-level=warn",
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)

    try:
        await asyncio.wait_for(proc.communicate(), timeout=config.timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()

    # 3. 合并分片（自然排序: seg-1, seg-2, ..., seg-10, seg-11）
    out = out_dir / f"{filename}.mp4"
    seg_files = sorted(
        tmp_dir.glob("*.ts"),
        key=lambda p: int(p.stem.split("-")[1]) if "-" in p.stem and p.stem.split("-")[1].isdigit() else 0
    )

    if not seg_files:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise RuntimeError("aria2 未下载到任何分片")

    # 检查完整性：缺片超过 5% 则报错
    total_expected = len(segments)
    missing = total_expected - len(seg_files)
    if missing > max(1, total_expected * 0.05):
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise RuntimeError(f"分片缺失: {missing}/{total_expected}，超过 5% 阈值")

    with open(out, "wb") as f:
        for sf in seg_files:
            f.write(sf.read_bytes())

    shutil.rmtree(tmp_dir, ignore_errors=True)
    return out.stat().st_size, out


# ============ 画质选择 ============

def _select_quality(qualities: dict[str, str], requested: str) -> Optional[str]:
    """从画质列表中选出最佳匹配，优先 HLS 后用直链"""
    if not qualities:
        return None

    if requested == "best":
        # PornHub 画质 key 不带 "p"（如 "480"、"720"），
        # 优先 HLS（数字 key），最后才选 direct（直链 mp4 兜底）
        order = ["2160", "1440", "1080", "720", "480", "360", "240"]
        for q in order:
            if q in qualities:
                return qualities[q]
            # 也尝试带 p 后缀的 key
            if q + "p" in qualities:
                return qualities[q + "p"]
        # HLS 画质都找不到，才考虑 direct
        if "direct" in qualities:
            return qualities["direct"]
        return next(iter(qualities.values()))

    return qualities.get(requested)


# ============ 工具函数 ============

def _build_download_url(filename: str, host: str, port: int, mode: str) -> str:
    """根据配置生成下载链接"""
    if mode == "http":
        return f"http://{host}:{port}/files/{filename}"
    return filename
