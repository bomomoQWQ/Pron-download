"""
主入口 - FastAPI + FastMCP 混合应用

一个 uvicorn 进程同时承载：
  • /mcp  → MCP 协议接口（AI Agent 调工具走这里）
  • /files → HTTP 文件下载（沙箱 Agent 拿文件走这里）
  • /health → 健康检查

启动方式：
  python app.py                      # 直接跑
  uvicorn app:app --host 0.0.0.0 --port 8765  # 生产模式
"""
import configparser
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastmcp import FastMCP
from fastmcp.utilities.lifespan import combine_lifespans

from engine.db import init_db
from engine.download import DownloadConfig
import engine.download as download_engine
from engine.download import SERVICE_CONFIG
from engine.search import SITE_CONFIG


# ============ 配置加载 ============

CONFIG_PATH = Path(__file__).parent / "config.ini"

def load_config() -> dict:
    """加载 config.ini，返回配置字典"""
    cfg = configparser.ConfigParser()
    if CONFIG_PATH.exists():
        cfg.read(CONFIG_PATH, encoding="utf-8")
    
    return {
        # 站点
        "site_base_url": cfg.get("site", "base_url", fallback=""),
        "search_path": cfg.get("site", "search_path", fallback="/video/search?search={keyword}&page={page}"),
        "category_path": cfg.get("site", "category_path", fallback="/video?c={category}&page={page}"),
        "embed_path": cfg.get("site", "embed_path", fallback="/embed/{video_id}"),
        "video_path": cfg.get("site", "video_path", fallback="/view_video.php?viewkey={video_id}"),
        # 存储
        "download_dir": cfg.get("storage", "download_dir", fallback="./downloads"),
        "serve_mode": cfg.get("storage", "serve_mode", fallback="http"),
        "serve_host": cfg.get("storage", "serve_host", fallback="0.0.0.0"),
        "serve_port": cfg.getint("storage", "serve_port", fallback=8765),
        # 搜索
        "max_results": cfg.getint("search", "max_results", fallback=30),
        "search_timeout": cfg.getint("search", "timeout", fallback=30),
        "min_delay": cfg.getfloat("search", "min_delay", fallback=2.0),
        # 下载
        "max_concurrent": cfg.getint("download", "max_concurrent", fallback=3),
        "download_timeout": cfg.getint("download", "timeout", fallback=600),
        "resume": cfg.getboolean("download", "resume", fallback=True),
        "rate_limit": cfg.getint("download", "rate_limit", fallback=0),
        # 反检测
        "proxy": cfg.get("anti_detect", "proxy", fallback=""),
        "max_retries": cfg.getint("anti_detect", "max_retries", fallback=3),
        "retry_base_delay": cfg.getfloat("anti_detect", "retry_base_delay", fallback=5.0),
        "min_request_delay": cfg.getfloat("anti_detect", "min_request_delay", fallback=1.0),
        "max_request_delay": cfg.getfloat("anti_detect", "max_request_delay", fallback=3.0),
        # SQLite
        "db_path": cfg.get("sqlite", "db_path", fallback="data.db"),
    }


config = load_config()

# 项目根目录
PROJECT_ROOT = Path(__file__).parent.resolve()

# 相对路径转绝对（统一以项目根目录为基准）
def _resolve_path(path: str) -> str:
    """把相对路径转为基于项目根目录的绝对路径"""
    p = Path(path)
    if p.is_absolute():
        return str(p)
    return str(PROJECT_ROOT / p)

DB_PATH = _resolve_path(config["db_path"])


# ============ MCP Server ============

# 创建 FastMCP 实例
mcp = FastMCP("Pron-download")

# 注册工具
from tools import register_tools
register_tools(mcp)


# ============ FastAPI 应用 ============

@asynccontextmanager
async def app_lifespan(app: FastAPI):
    """应用生命周期：启动时初始化数据库和下载配置，关闭时清理浏览器"""
    # 启动
    await init_db()
    
    # 注入站点配置到搜索引擎
    SITE_CONFIG["site_base_url"] = config["site_base_url"]
    SITE_CONFIG["search_path"] = config["search_path"]
    SITE_CONFIG["category_path"] = config["category_path"]
    SITE_CONFIG["embed_path"] = config["embed_path"]
    SITE_CONFIG["video_path"] = config["video_path"]
    
    # 注入服务配置到下载模块
    SERVICE_CONFIG["site_base_url"] = config["site_base_url"]
    SERVICE_CONFIG["embed_path"] = config["embed_path"]
    SERVICE_CONFIG["serve_host"] = config["serve_host"]
    SERVICE_CONFIG["serve_port"] = config["serve_port"]
    SERVICE_CONFIG["serve_mode"] = config["serve_mode"]
    
    download_dir = _resolve_path(config["download_dir"])
    Path(download_dir).mkdir(parents=True, exist_ok=True)
    
    download_engine.download_config = DownloadConfig(
        download_dir=download_dir,
        max_concurrent=config["max_concurrent"],
        timeout=config["download_timeout"],
        resume=config["resume"],
        rate_limit=config["rate_limit"],
        proxy=config["proxy"] or None,
    )
    
    print(f"[启动] 下载目录: {download_dir}")
    print(f"[启动] 文件服务模式: {config['serve_mode']}")
    print(f"[启动] 数据库: {DB_PATH}")
    print(f"[启动] MCP 端点: http://{config['serve_host']}:{config['serve_port']}/mcp")
    print(f"[启动] 文件服务: http://{config['serve_host']}:{config['serve_port']}/files")
    
    yield
    
    # 关闭
    from engine.search import close_browser
    await close_browser()
    print("[关闭] 浏览器已关闭")


# 挂载 MCP 到 /mcp
mcp_app = mcp.http_app(path="/", stateless_http=True, json_response=True)

app = FastAPI(
    title="Pron-download MCP Server",
    description="搜-打-撤 视频搜索下载 MCP 服务",
    version="1.0.0",
    lifespan=combine_lifespans(app_lifespan, mcp_app.lifespan),
)

# ============ MCP 路由 ============
# 不用 app.mount()（会产生 /mcp → /mcp/ 的 307 重定向问题），
# 改为手动路由转发，/mcp 和 /mcp/ 都直接处理。

@app.api_route("/mcp", methods=["GET", "POST", "DELETE", "OPTIONS"])
@app.api_route("/mcp/{path:path}", methods=["GET", "POST", "DELETE", "OPTIONS"])
async def mcp_handler(request: Request, path: str = ""):
    """将所有 /mcp/* 请求转发到 FastMCP 应用"""
    scope = dict(request.scope)
    scope["path"] = "/" + path
    scope["raw_path"] = ("/" + path).encode()
    return await mcp_app(scope, request.receive, request._send)


# ============ 文件服务 ============

DOWNLOAD_DIR = Path(_resolve_path(config["download_dir"]))


@app.get("/files/{filename:path}")
async def serve_file(filename: str):
    """
    文件下载端点
    
    沙箱中的 AI Agent 通过 HTTP GET 获取已下载的视频文件。
    URL 示例: http://localhost:8765/files/abc123_720p.mp4
    """
    # 安全检查：防止路径穿越攻击
    file_path = DOWNLOAD_DIR / filename
    file_path = file_path.resolve()
    
    if not str(file_path).startswith(str(DOWNLOAD_DIR.resolve())):
        raise HTTPException(status_code=403, detail="禁止访问")
    
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="文件不存在")
    
    # 流式返回大文件
    return FileResponse(
        path=str(file_path),
        filename=filename,
        media_type="application/octet-stream",
    )


@app.get("/health")
async def health():
    """健康检查端点"""
    return {
        "status": "ok",
        "download_dir": str(DOWNLOAD_DIR),
        "serve_mode": config["serve_mode"],
        "version": "1.0.0",
    }


# ============ 直接启动 ============

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app:app",
        host=config["serve_host"],
        port=config["serve_port"],
        reload=False,
        log_level="info",
    )
