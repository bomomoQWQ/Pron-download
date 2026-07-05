"""
MCP 工具定义 - 暴露给 AI Agent 的工具接口

搜-打-撤核心工具：
  - search_videos: 搜索视频
  - download_video: 下载视频（异步，不阻塞）
  - download_status: 查询下载进度
  - get_video_info: 查看视频详情
  - list_downloads: 列出已下载内容
  - delete_download: 删除已下载文件

所有工具返回结构化的 dict/list，方便 AI 理解和操作。
"""
from engine.search import search_videos as engine_search
from engine.download import download_video as engine_download, get_status as engine_status
import engine.download as download_engine
from engine.db import list_downloads as db_list_downloads, delete_download as db_delete_download


def register_tools(mcp):
    """注册所有 MCP 工具到 FastMCP 实例"""

    @mcp.tool
    async def search_videos(
        keyword: str = "",
        category: str = "",
        page: int = 1,
        max_results: int = 30,
    ) -> dict:
        """
        搜索视频。
        
        Args:
            keyword: 搜索关键词，为空则按分类浏览
            category: 分类 ID，例如 1=Asian, 111=Japanese
            page: 页码，从 1 开始
            max_results: 最多返回多少条结果
        
        Returns:
            {"total": int, "results": [{"video_id": str, "title": str, "duration": int, "qualities": list, "thumbnail_url": str, "views": int, "rating": float}]}
        """
        results = await engine_search(
            keyword=keyword,
            category=category,
            page_num=page,
            max_results=max_results,
        )
        return {
            "total": len(results),
            "results": results,
            "keyword": keyword,
            "category": category,
            "page": page,
        }

    @mcp.tool
    async def download_video(
        video_id: str,
        quality: str = "best",
    ) -> dict:
        """
        下载视频。任务异步执行，不阻塞，完成后通过 download_status 查询。
        
        Args:
            video_id: 视频 ID（从搜索结果中获得）
            quality: 画质，默认 "best" 表示最高画质。可选 "1080p", "720p", "480p" 等
        
        Returns:
            {"task_id": str, "status": "queued", "message": str}
            如果已下载过，会直接返回完成状态和文件链接
        """
        if download_engine.download_config is None:
            return {"error": "下载模块未初始化，请检查配置"}

        result = await engine_download(
            video_id=video_id,
            quality=quality,
        )
        return result

    @mcp.tool
    async def download_status(task_id: str) -> dict:
        """
        查询下载任务的进度。
        
        Args:
            task_id: 下载时返回的任务 ID
        
        Returns:
            {"task_id": str, "status": str, "progress": float, "download_url": str|null, "error": str|null}
            status 可能的值: queued（排队）, fetching（获取信息）, downloading（下载中）, completed（完成）, failed（失败）
        """
        result = await engine_status(task_id)
        if result is None:
            return {"error": f"未找到任务 {task_id}"}
        return result

    @mcp.tool
    async def get_video_info(video_id: str) -> dict:
        """
        查看视频详细信息（含完整画质列表）。
        
        Args:
            video_id: 视频 ID
        
        Returns:
            视频的完整元数据，包含 qualities 字段 {"720p": "url", "1080p": "url", ...}
        """
        from engine.db import get_video_meta, save_video_meta
        from engine.download import fetch_video_details
        
        # 先看缓存
        meta = await get_video_meta(video_id)
        
        # 如果缓存有数据但画质为空，尝试从 embed 页补全
        if meta and isinstance(meta.get("qualities"), str):
            import json
            try:
                q = json.loads(meta["qualities"])
            except (json.JSONDecodeError, TypeError):
                q = {}
            if not q:
                meta = None  # 画质为空，当没缓存处理
        
        if meta is None:
            # 搜 embed 页面获取画质列表
            result = await fetch_video_details(video_id)
            if result and result[0]:
                details = result[0]
                await save_video_meta(video_id, details)
                return details
            return {"error": f"未找到视频 {video_id} 的信息"}
        
        # 将 JSON 字符串转回 Python 对象
        import json
        for field in ("qualities", "tags", "categories"):
            if field in meta and isinstance(meta[field], str):
                try:
                    meta[field] = json.loads(meta[field])
                except (json.JSONDecodeError, TypeError):
                    pass
        return dict(meta)

    @mcp.tool
    async def list_downloads(limit: int = 50) -> dict:
        """
        列出最近的下载记录。
        
        Args:
            limit: 最多返回多少条
        
        Returns:
            {"total": int, "downloads": [{"video_id": str, "quality": str, "file_path": str, "file_size": int, "downloaded_at": float}]}
        """
        downloads = await db_list_downloads(limit)
        return {
            "total": len(downloads),
            "downloads": downloads,
        }

    @mcp.tool
    async def delete_download(video_id: str, quality: str = "") -> dict:
        """
        删除下载记录（可以选择是否同时删除文件）。
        
        Args:
            video_id: 视频 ID
            quality: 画质，为空则删除该视频所有画质的记录
        
        Returns:
            {"deleted": bool, "message": str}
        """
        import os
        from engine.db import get_db
        
        # 先查文件路径
        db = await get_db()
        try:
            if quality:
                cursor = await db.execute(
                    "SELECT file_path FROM download_records WHERE video_id = ? AND quality = ?",
                    (video_id, quality)
                )
            else:
                cursor = await db.execute(
                    "SELECT file_path FROM download_records WHERE video_id = ?", (video_id,)
                )
            rows = await cursor.fetchall()
            
            # 删文件
            for row in rows:
                filepath = row["file_path"]
                if filepath and os.path.exists(filepath):
                    os.remove(filepath)
        finally:
            await db.close()
        
        # 删数据库记录
        await db_delete_download(video_id, quality or None)
        
        return {"deleted": True, "message": f"已删除 {video_id} 的下载记录"}

    @mcp.tool
    async def rename_download(
        video_id: str,
        new_name: str = "",
        quality: str = "",
    ) -> dict:
        """
        重命名已下载的视频文件。

        Args:
            video_id: 视频 ID
            new_name: 新文件名（不含扩展名）。为空则自动使用视频标题
            quality: 画质。为空则重命名该视频所有画质的文件

        Returns:
            {"renamed": [{"old": str, "new": str}], "message": str}
        """
        import os
        import re
        from pathlib import Path
        from engine.db import get_db, get_video_meta

        db = await get_db()
        try:
            # 查文件记录
            if quality:
                cursor = await db.execute(
                    "SELECT file_path, quality FROM download_records WHERE video_id = ? AND quality = ?",
                    (video_id, quality)
                )
            else:
                cursor = await db.execute(
                    "SELECT file_path, quality FROM download_records WHERE video_id = ?", (video_id,)
                )
            rows = await cursor.fetchall()
            if not rows:
                return {"error": f"未找到 {video_id} 的下载记录"}

            # 确定新名字
            if not new_name:
                meta = await get_video_meta(video_id)
                new_name = (meta.get("title") or video_id) if meta else video_id

            # 安全化文件名（去非法字符，限长）
            safe = re.sub(r'[\\/:*?"<>|]', '', new_name).strip()[:120]

            renamed = []
            for row in rows:
                old = row["file_path"]
                q = row["quality"]
                if not old or not os.path.exists(old):
                    continue

                ext = Path(old).suffix
                new_path = str(Path(old).parent / f"{safe}_{q}{ext}")

                if old == new_path:
                    continue

                os.rename(old, new_path)
                renamed.append({"old": old, "new": new_path})

                # 更新数据库
                await db.execute(
                    "UPDATE download_records SET file_path = ? WHERE video_id = ? AND quality = ?",
                    (new_path, video_id, q)
                )
                # 也更新 download_tasks 里的路径
                await db.execute(
                    "UPDATE download_tasks SET file_path = ? WHERE video_id = ? AND quality = ?",
                    (new_path, video_id, q)
                )

            await db.commit()

            if renamed:
                return {
                    "renamed": renamed,
                    "message": f"已重命名 {len(renamed)} 个文件 → {safe}"
                }
            return {"message": "文件已是目标名称，无需重命名"}
        finally:
            await db.close()
