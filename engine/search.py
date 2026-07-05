"""
搜索模块 - 基于 Playwright 的搜索引擎

实现流程：
  1. 启动/复用 Playwright Chromium 浏览器
  2. 注入 stealth 脚本，抹掉 webdriver 痕迹
  3. 加载已持久化的 Cookie（登录态复用）
  4. 访问搜索页面，等待 JS 渲染完成
  5. 解析搜索结果列表
  6. 缓存元数据到 SQLite
"""
import re
from typing import Optional
from playwright.async_api import async_playwright, Browser, BrowserContext, Page

from .anti_detect import (
    STEALTH_SCRIPT, random_ua, human_delay, load_cookies, save_cookies
)
from .db import get_cached_search, set_cached_search, save_video_meta


# 站点配置（由 app.py 在启动时注入）
SITE_CONFIG: dict = {}


# 浏览器实例复用（进程级单例）
_browser: Optional[Browser] = None
_context: Optional[BrowserContext] = None


async def get_browser() -> Browser:
    """
    获取或创建浏览器实例（复用，不每次启动）

    Playwright 的 Browser 对象是重量级的，整个进程生命周期中
    只启动一次，后续通过创建新 Page 来处理请求。
    """
    global _browser
    if _browser is None:
        pw = await async_playwright().start()
        _browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
                "--disable-gpu",
            ]
        )
    return _browser


async def get_context() -> BrowserContext:
    """
    获取或创建浏览器上下文（复用 Cookie 存储）
    
    BrowserContext 是一个隔离的浏览会话，有自己的 Cookie、localStorage。
    复用 Context 可以保持登录态，避免每次都重新登录。
    """
    global _context
    if _context is None:
        browser = await get_browser()
        _context = await browser.new_context(
            user_agent=random_ua(),
            viewport={"width": 1920, "height": 1080},
            locale="en-US",
            timezone_id="America/New_York",
        )
        # 加载已持久化的 Cookie
        saved_cookies = load_cookies()
        if saved_cookies:
            await _context.add_cookies(saved_cookies)
    return _context


async def build_page() -> Page:
    """创建配置好 stealth 的页面"""
    context = await get_context()
    page = await context.new_page()
    
    # 注入 stealth 脚本，在页面加载前运行
    await page.add_init_script(STEALTH_SCRIPT)
    return page


async def search_videos(
    keyword: str = "",
    category: str = "",
    page_num: int = 1,
    max_results: int = 30,
    timeout: int = 30,
    min_delay: float = 2.0,
) -> list[dict]:
    """
    搜索视频
    
    Args:
        keyword: 搜索关键词（为空则按分类浏览）
        category: 分类 ID
        page_num: 页码
        max_results: 最大返回结果数
        timeout: 页面加载超时（秒）
        min_delay: 最小请求间隔（秒）
    
    Returns:
        视频结果列表，每项包含 video_id, title, duration, qualities, thumbnail_url, link_url, views, rating
    """
    # 先查缓存（缓存数量足够才直接用）
    cache_key = f"{keyword}_{category}_{page_num}"
    cached = await get_cached_search(cache_key, "")
    if cached and len(cached) >= max_results:
        return cached[:max_results]
    
    # 构建搜索 URL
    base = SITE_CONFIG.get("site_base_url", "")
    if keyword:
        path = SITE_CONFIG.get("search_path", "/video/search?search={keyword}&page={page}")
        search_url = base + path.format(keyword=keyword, page=page_num)
    elif category:
        path = SITE_CONFIG.get("category_path", "/video?c={category}&page={page}")
        search_url = base + path.format(category=category, page=page_num)
    else:
        search_url = f"{base}/video?page={page_num}"
    
    page = await build_page()
    results = []
    
    try:
        # 人类化延迟
        await human_delay(min_delay, min_delay + 1.0)
        
        # 访问搜索页（networkidle 优先，超时降级为 load）
        try:
            await page.goto(search_url, wait_until="networkidle", timeout=timeout * 1000)
        except Exception:
            # 页面有持续后台请求（analytics 等），networkidle 永远不触发，降级
            await page.goto(search_url, wait_until="load", timeout=timeout * 1000)
        
        # 处理 Cookie 弹窗
        try:
            cookie_btn = await page.wait_for_selector(
                "button:has-text('Ok'), button:has-text('Accept'), .ageDisclaimerButtons button",
                timeout=5000
            )
            if cookie_btn:
                await cookie_btn.click()
                await page.wait_for_timeout(2000)
        except Exception:
            pass  # 没有弹窗就继续
        
        # 处理年龄确认门：找 "I am 18 or older" 之类的按钮
        try:
            age_btn = await page.wait_for_selector(
                "a:has-text('18'), button:has-text('18'), .ageDisclaimerButtons a, .ageDisclaimerButtons button",
                timeout=3000
            )
            if age_btn:
                await age_btn.click()
                await page.wait_for_timeout(3000)
        except Exception:
            pass
        
        # 等待页面内容出现（用 body 里任意元素判断页面已渲染）
        await page.wait_for_selector("body", timeout=timeout * 1000)
        await page.wait_for_timeout(2000)
        
        # 解析视频列表
        video_cards = await page.query_selector_all("li.pcVideoListItem")
        if not video_cards:
            video_cards = await page.query_selector_all("div.js-videoThumb")
        
        for card in video_cards[:max_results]:
            try:
                result = await _parse_video_card(card)
                if result:
                    results.append(result)
                    await save_video_meta(result["video_id"], result)
            except Exception:
                continue
        
        # 缓存本次搜索结果
        await set_cached_search(cache_key, "", results)
        
        # 持久化 Cookie（登录态下次复用）
        context = await get_context()
        cookies = await context.cookies()
        save_cookies(cookies)
        
        return results
    finally:
        await page.close()


async def _parse_video_card(card) -> Optional[dict]:
    """
    解析单个视频卡片（兼容新旧版 DOM 结构）
    """
    try:
        # 提取链接和视频 ID
        # 优先从 data-video-vkey 属性获取（新版 DOM）
        vkey = await card.get_attribute("data-video-vkey")
        if vkey:
            video_id = vkey
        else:
            # 旧版：从 href 中提取 viewkey=
            link_elem = await card.query_selector("a[href*='viewkey']")
            if not link_elem:
                link_elem = await card.query_selector("a")
            if not link_elem:
                return None
            
            href = await link_elem.get_attribute("href") or ""
            video_id_match = re.search(r"viewkey=([a-zA-Z0-9]+)", href)
            if not video_id_match:
                return None
            video_id = video_id_match.group(1)
        
        link_elem = await card.query_selector("a")
        
        # 提取标题（多种可能的选择器）
        title = ""
        for sel in [".title a", ".videoTitle", ".title", "a[title]", "[title]"]:
            title_elem = await card.query_selector(sel)
            if title_elem:
                title = await title_elem.inner_text()
                if not title:
                    title = await title_elem.get_attribute("title") or ""
                if title:
                    break
        if not title:
            title = await link_elem.get_attribute("title") or ""
        title = title.strip()
        
        # 提取时长
        duration = 0
        for sel in [".duration", ".videoDuration", "var.duration", ".marker-overlays"]:
            dur_elem = await card.query_selector(sel)
            if dur_elem:
                dur_text = await dur_elem.inner_text()
                duration = _parse_duration(dur_text)
                if duration > 0:
                    break
        
        # 提取封面图
        img_elem = await card.query_selector("img")
        thumbnail_url = ""
        if img_elem:
            thumbnail_url = (
                await img_elem.get_attribute("data-thumb_url")
                or await img_elem.get_attribute("data-src")
                or await img_elem.get_attribute("src")
                or ""
            )
        
        # 提取浏览量
        views = 0
        for sel in [".views var", ".videoViews", ".views"]:
            views_elem = await card.query_selector(sel)
            if views_elem:
                views_text = await views_elem.inner_text()
                views = _parse_number(views_text)
                if views > 0:
                    break
        
        # 提取评分
        rating = 0.0
        for sel in [".rating-container .value", ".videoRating", ".rating .value"]:
            rating_elem = await card.query_selector(sel)
            if rating_elem:
                rating_text = await rating_elem.inner_text()
                try:
                    rating = float(rating_text.replace("%", ""))
                except ValueError:
                    pass
                if rating > 0:
                    break
        
        base = SITE_CONFIG.get("site_base_url", "")
        video_path_tpl = SITE_CONFIG.get("video_path", "/view_video.php?viewkey={video_id}")
        
        return {
            "video_id": video_id,
            "title": title,
            "duration": duration,
            "qualities": {},  # 详情页才有画质列表，搜索页不包含
            "thumbnail_url": thumbnail_url,
            "link_url": base + video_path_tpl.format(video_id=video_id),
            "views": views,
            "rating": rating,
            "tags": [],
            "categories": [],
        }
    except Exception:
        # 单个卡片解析失败不中断整体
        return None


def _parse_duration(text: str) -> int:
    """将时长字符串转为秒数，例如 '12:34' → 754"""
    text = text.strip()
    parts = text.split(":")
    if len(parts) == 2:
        return int(parts[0]) * 60 + int(parts[1])
    elif len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    return 0


def _parse_number(text: str) -> int:
    """将带单位的数字字符串转为整数，例如 '1.2M' → 1200000"""
    text = text.strip().upper().replace(",", "")
    multipliers = {"K": 1000, "M": 1000000, "B": 1000000000}
    
    for suffix, mult in multipliers.items():
        if suffix in text:
            try:
                return int(float(text.replace(suffix, "")) * mult)
            except ValueError:
                return 0
    
    try:
        return int(text)
    except ValueError:
        return 0


async def close_browser():
    """关闭浏览器实例（进程退出时调用）"""
    global _browser, _context
    if _context:
        await _context.close()
        _context = None
    if _browser:
        await _browser.close()
        _browser = None
