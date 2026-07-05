# Pron-download

搜-打-撤：为 AI Agent 打造的 PornHub 视频搜索下载 MCP 服务。

## 技术架构

```
搜索:  Playwright (stealth) → 解析 DOM → 缓存 SQLite
下载:  Playwright 解析 HLS/m3u8 → aria2 (WinTLS) 多线程拉分片 → 二进制合并
服务:  uvicorn (FastAPI + FastMCP) → /mcp 工具 + /files 文件服务
```

**为什么用 aria2？** Windows 上 aria2 走 WinTLS (SCHANNEL)，和 Edge/Chrome 共享系统 TLS 栈，PornHub CDN 无法区分。curl_cffi/httpx 的 OpenSSL 指纹会被拦。

## 快速开始

```bash
# === Linux ===
pip install -r requirements.txt
pip install curl-cffi
playwright install chromium
sudo apt install aria2
# Ubuntu 24.04 用 GnuTLS，CDN 不拦 ✅ 已实测

# === Windows (PowerShell) ===
pip install -r requirements.txt
playwright install chromium
# aria2: https://github.com/aria2/aria2/releases → aria2c.exe 放 PATH

# === 启动（通用） ===
python app.py
# 或 uvicorn app:app --host 0.0.0.0 --port 8765
```

## MCP 配置

```json
{
  "mcpServers": {
    "pron-download": {
      "url": "http://your-server-ip:8765/mcp",
      "transport": "streamable-http"
    }
  }
}
```

## MCP 工具

| 工具 | 用途 | 关键参数 |
|---|---|---|
| `search_videos` | 搜索视频 | keyword, category, page |
| `download_video` | 异步下载（不阻塞） | video_id, quality ("best"=最高画质) |
| `download_status` | 查下载进度 | task_id |
| `get_video_info` | 看画质列表 | video_id |
| `list_downloads` | 列出已下载 | limit |
| `rename_download` | 重命名文件 | video_id, new_name, quality |
| `delete_download` | 删记录 + 文件 | video_id, quality |

## 配置文件 `config.ini`

```ini
[site]         # 目标网站 URL、搜索/embed 路径模板
[storage]      # 下载目录、文件服务模式 (http/local)、监听端口
[search]       # 最大结果数、超时、请求间隔
[download]     # aria2 并发数、超时
[anti_detect]  # 代理、重试次数、Cookie 文件路径
```

## 项目结构

```
├── app.py                      # FastAPI + FastMCP 主入口
├── tools.py                    # MCP 工具注册
├── config.ini                  # 配置文件
├── requirements.txt
└── engine/
    ├── search.py               # Playwright 搜索 + DOM 解析
    ├── download.py             # aria2 下载引擎 (HLS + 直链)
    ├── anti_detect.py          # Stealth 脚本 / UA 池 / Cookie 持久化
    └── db.py                   # SQLite (搜索缓存 + 下载记录)
```

## 依赖

| 类型 | 依赖 |
|---|---|
| Python | fastmcp, fastapi, uvicorn, playwright, aiosqlite |
| 系统 | **aria2** (Windows: 放 PATH；Linux: `apt install aria2`) |
| 可选 | curl-cffi (仅 Linux，Windows 自动跳过) |

## 平台兼容性

| | Windows | Linux |
|---|---|---|
| **HLS 下载** | aria2 + WinTLS ✅ | aria2 + GnuTLS ✅ 已实测 |
| **直链 mp4** | aria2 可能不通（DNS），浏览器兜底 | 同左 |
| **curl_cffi** | 自动禁用 | 自动启用（备用） |
