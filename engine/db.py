"""
数据库模块 - SQLite 异步操作

管理三张表：
  - search_cache: 搜索缓存（搜过的词不再重复搜）
  - download_tasks: 下载任务状态（排队/下载中/完成/失败）
  - download_records: 已下载记录（下过的不重复下）
"""
import aiosqlite
import json
import time
from pathlib import Path
from typing import Optional


DB_PATH: str = "data.db"


async def get_db() -> aiosqlite.Connection:
    """获取数据库连接，自动启用 WAL 模式和外键"""
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    return db


async def init_db():
    """初始化数据库表结构"""
    db = await get_db()
    try:
        await db.executescript("""
            -- 搜索缓存：搜过的查询直接取缓存，避免重复爬
            CREATE TABLE IF NOT EXISTS search_cache (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                query TEXT NOT NULL,
                category TEXT DEFAULT '',
                results_json TEXT NOT NULL,
                created_at REAL NOT NULL,
                UNIQUE(query, category)
            );

            -- 视频元数据：已爬取的视频信息，不用重复爬详情页
            CREATE TABLE IF NOT EXISTS video_meta (
                video_id TEXT PRIMARY KEY,
                title TEXT,
                duration INTEGER,
                qualities TEXT,          -- JSON: {"480p": "url", "720p": "url"}
                thumbnail_url TEXT,
                link_url TEXT,
                views INTEGER DEFAULT 0,
                rating REAL DEFAULT 0,
                tags TEXT,              -- JSON: ["tag1", "tag2"]
                categories TEXT,        -- JSON: ["cat1", "cat2"]
                scraped_at REAL NOT NULL
            );

            -- 下载任务：追踪每个下载的状态
            CREATE TABLE IF NOT EXISTS download_tasks (
                task_id TEXT PRIMARY KEY,
                video_id TEXT NOT NULL,
                quality TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                progress REAL DEFAULT 0,
                file_path TEXT,
                file_size INTEGER DEFAULT 0,
                error TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );

            -- 下载记录：下过的不重复下
            CREATE TABLE IF NOT EXISTS download_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                video_id TEXT NOT NULL,
                quality TEXT NOT NULL,
                file_path TEXT NOT NULL,
                file_size INTEGER DEFAULT 0,
                downloaded_at REAL NOT NULL,
                UNIQUE(video_id, quality)
            );
        """)
        await db.commit()
    finally:
        await db.close()


# ============ 搜索缓存 ============

async def get_cached_search(query: str, category: str = "") -> Optional[list[dict]]:
    """查询是否有缓存的搜索结果（24 小时内有效）"""
    db = await get_db()
    try:
        cutoff = time.time() - 86400  # 24 小时
        cursor = await db.execute(
            "SELECT results_json FROM search_cache WHERE query = ? AND category = ? AND created_at > ?",
            (query, category, cutoff)
        )
        row = await cursor.fetchone()
        if row:
            return json.loads(row["results_json"])
        return None
    finally:
        await db.close()


async def set_cached_search(query: str, category: str, results: list[dict]):
    """缓存搜索结果"""
    db = await get_db()
    try:
        await db.execute(
            "INSERT OR REPLACE INTO search_cache (query, category, results_json, created_at) VALUES (?, ?, ?, ?)",
            (query, category, json.dumps(results, ensure_ascii=False), time.time())
        )
        await db.commit()
    finally:
        await db.close()


# ============ 视频元数据 ============

async def get_video_meta(video_id: str) -> Optional[dict]:
    """获取已缓存的视频元数据"""
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM video_meta WHERE video_id = ?", (video_id,))
        row = await cursor.fetchone()
        if row is None:
            return None
        return dict(row)
    finally:
        await db.close()


async def save_video_meta(video_id: str, meta: dict):
    """保存视频元数据"""
    db = await get_db()
    try:
        await db.execute(
            """INSERT OR REPLACE INTO video_meta
               (video_id, title, duration, qualities, thumbnail_url, link_url,
                views, rating, tags, categories, scraped_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                video_id,
                meta.get("title"),
                meta.get("duration"),
                json.dumps(meta.get("qualities", {}), ensure_ascii=False),
                meta.get("thumbnail_url"),
                meta.get("link_url"),
                meta.get("views", 0),
                meta.get("rating", 0),
                json.dumps(meta.get("tags", []), ensure_ascii=False),
                json.dumps(meta.get("categories", []), ensure_ascii=False),
                time.time(),
            )
        )
        await db.commit()
    finally:
        await db.close()


# ============ 下载任务 ============

async def create_download_task(video_id: str, quality: str) -> str:
    """创建下载任务，返回 task_id"""
    task_id = f"dl_{int(time.time() * 1000)}_{video_id[:8]}"
    now = time.time()
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO download_tasks (task_id, video_id, quality, status, created_at, updated_at) VALUES (?, ?, ?, 'queued', ?, ?)",
            (task_id, video_id, quality, now, now)
        )
        await db.commit()
        return task_id
    finally:
        await db.close()


async def update_task_progress(task_id: str, status: str, progress: float = 0,
                                file_path: Optional[str] = None, file_size: int = 0, error: Optional[str] = None):
    """更新任务状态"""
    db = await get_db()
    try:
        await db.execute(
            """UPDATE download_tasks
               SET status = ?, progress = ?, file_path = ?, file_size = ?, error = ?, updated_at = ?
               WHERE task_id = ?""",
            (status, progress, file_path, file_size, error, time.time(), task_id)
        )
        await db.commit()
    finally:
        await db.close()


async def get_task_status(task_id: str) -> Optional[dict]:
    """查询任务状态"""
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM download_tasks WHERE task_id = ?", (task_id,))
        row = await cursor.fetchone()
        if row is None:
            return None
        return dict(row)
    finally:
        await db.close()


async def get_all_tasks(status: Optional[str] = None) -> list[dict]:
    """获取所有下载任务，可按状态过滤"""
    db = await get_db()
    try:
        if status:
            cursor = await db.execute(
                "SELECT * FROM download_tasks WHERE status = ? ORDER BY created_at DESC", (status,)
            )
        else:
            cursor = await db.execute("SELECT * FROM download_tasks ORDER BY created_at DESC")
        return [dict(row) for row in await cursor.fetchall()]
    finally:
        await db.close()


# ============ 下载记录 ============

async def is_downloaded(video_id: str, quality: str) -> bool:
    """检查是否已经下载过"""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT 1 FROM download_records WHERE video_id = ? AND quality = ?",
            (video_id, quality)
        )
        return await cursor.fetchone() is not None
    finally:
        await db.close()


async def record_download(video_id: str, quality: str, file_path: str, file_size: int):
    """记录下载完成"""
    db = await get_db()
    try:
        await db.execute(
            "INSERT OR REPLACE INTO download_records (video_id, quality, file_path, file_size, downloaded_at) VALUES (?, ?, ?, ?, ?)",
            (video_id, quality, file_path, file_size, time.time())
        )
        await db.commit()
    finally:
        await db.close()


async def list_downloads(limit: int = 50) -> list[dict]:
    """列出最近的下载记录"""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT * FROM download_records ORDER BY downloaded_at DESC LIMIT ?", (limit,)
        )
        return [dict(row) for row in await cursor.fetchall()]
    finally:
        await db.close()


async def delete_download(video_id: str, quality: Optional[str] = None):
    """删除下载记录（不删文件）"""
    db = await get_db()
    try:
        if quality:
            await db.execute(
                "DELETE FROM download_records WHERE video_id = ? AND quality = ?",
                (video_id, quality)
            )
        else:
            await db.execute(
                "DELETE FROM download_records WHERE video_id = ?", (video_id,)
            )
        await db.commit()
    finally:
        await db.close()
