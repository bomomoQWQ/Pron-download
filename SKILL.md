---
name: pron-download
description: "PornHub 视频搜索下载 MCP 服务。搜-打-撤三件套：Playwright 搜索 → aria2 下载 HLS 分片 → HTTP 文件服务。Windows (WinTLS) / Linux (GnuTLS) 双平台兼容，均实测通过。Triggers: '下载视频', '搜视频', 'pron', 'pornhub download', '视频下载', '搜-打-撤'."
---

# Pron-download — PornHub 视频搜索下载 MCP 服务

为 AI Agent 提供 PornHub 视频搜索和下载能力。单进程运行，零外部服务，开箱即用。

## 核心流程

```
search_videos("japanese", max_results=5)
    │ → [video_id, title, duration, ...]
    │
    ├─ get_video_info(video_id)
    │     → {qualities: {"480": "m3u8_url", "720": "m3u8_url"}}
    │
    └─ download_video(video_id, quality="best")
          → {task_id: "dl_xxx"}
          │
          └─ download_status(task_id)  // 轮询
               → {status: "completed", download_url: "http://your-server-ip:8765/files/xxx.mp4"}
```

完成下载后，AI Agent 通过 HTTP 文件服务直接获取视频文件，无需本地路径。

## 可用工具

### `search_videos`
搜索视频。支持关键词、分类浏览、翻页。

| 参数 | 默认值 | 说明 |
|---|---|---|
| `keyword` | `""` | 搜索词，支持中日英。为空则按分类浏览 |
| `category` | `""` | 分类 ID（如 `111`=Japanese） |
| `page` | `1` | 页码 |
| `max_results` | `30` | 返回条数上限 |

返回：`{total, results: [{video_id, title, duration, thumbnail_url, views, rating}]}`

### `get_video_info`
查视频的完整画质列表。下载前应调此工具确定有哪些画质可用。

| 参数 | 说明 |
|---|---|
| `video_id` | 视频 ID |

返回：`{video_id, title, duration, qualities: {"480": "url", "720": "url", ...}}`

### `download_video`
异步下载，立即返回 task_id，不阻塞。通过 `download_status` 轮询进度。

| 参数 | 默认值 | 说明 |
|---|---|---|
| `video_id` | 必填 | 视频 ID |
| `quality` | `"best"` | `"best"` 自动选最高 HLS 画质，也可指定 `"480"`, `"720"` 等 |

返回：`{task_id, status: "queued", message}`

### `download_status`
查询下载进度。完成后 `download_url` 字段即为可访问的 HTTP 文件地址。

| 参数 | 说明 |
|---|---|
| `task_id` | 下载时返回的任务 ID |

返回：`{status, progress, file_size, download_url, error}`

状态流转：`queued` → `fetching`(5%) → `downloading`(10%) → `completed`(100%) 或 `failed`

### `list_downloads`
列出已下载的所有视频。

### `delete_download`
删除下载记录和对应文件。

## 内置文件服务

下载完成后的视频通过 HTTP 对外提供。沙箱环境中的 AI Agent 没有本地文件系统访问权限，通过这个轻量 HTTP 服务器获取文件。

```
GET http://服务IP:8765/files/{filename}
```

**安全特性**：
- 只暴露 `config.ini` 中配置的下载目录，路径穿越攻击被拦截（返回 403）
- 每个视频一个 URL，不存在目录遍历
- `serve_mode=local` 可切换为返回本地路径（同机 Agent 直接读）

**典型用法**——AI Agent 拿到 `download_url` 后：

```
download_url = http://your-server-ip:8765/files/6a349008472d1_best.mp4
→ 直接 HTTP GET 或 wget/curl 下载到本地
```

## 服务质量

### 搜索
- 每次搜索约 5-10 秒（取决于网络）
- 返回封面 + 时长 + 标题等元数据
- 结果缓存 24 小时，重复搜索秒出

### 下载
- HLS 视频：aria2 3 线程并行，典型速度 **10-20MB/s**，250MB 视频约 **15-25 秒**
- 直链 mp4：aria2 优先（15s 超时），失败则 Playwright 浏览器下载兜底
- `quality="best"` 自动选最高 HLS 画质，不选直链
- 相同 video_id+quality 不重复下载，直接返回缓存

## 架构

```
┌──────────────────────────────────────────────────┐
│                 FastAPI + FastMCP                 │
│            0.0.0.0:8765 (uvicorn)                │
│  /mcp ← MCP 工具    /files ← 文件服务   /health  │
└────────┬──────────────────────────┬──────────────┘
         │                          │
    ┌────▼────┐              ┌──────▼──────┐
    │ Playwright│              │   aria2     │
    │ (Chromium)│              │ (下载引擎)  │
    │          │              │             │
    │ · search │              │ · HLS 分片  │
    │ · embed  │              │ · 直链 mp4  │
    │ · cookie │              │ · 二进制合并│
    └────┬─────┘              └──────┬──────┘
         │                          │
         ▼                          ▼
    ┌──────────────────────────────────────┐
    │             TLS 层                    │
    │  Windows: WinTLS (SCHANNEL) ✅        │
    │  Linux:   GnuTLS ✅ 已实测            │
    └──────────────────────────────────────┘
```

### 双平台

| 组件 | Windows | Linux |
|---|---|---|
| **aria2 TLS** | WinTLS — 和 Chrome 同栈 | GnuTLS — 自有指纹，CDN 不拦 ✅ |
| **Playwright** | Chromium 无头 | Chromium 无头 |
| **安装 aria2** | GitHub 下载 exe → PATH | `apt install aria2` |
| **Python 包** | `pip install -r requirements.txt` | 同左 |

## 反检测策略

1. **TLS 指纹**：Windows WinTLS = Chrome SCHANNEL；Linux GnuTLS 实测 CDN 不拦
2. **HTTP 头**：完整浏览器头（UA、Accept、Sec-Fetch-*），下载时带 Referer/Origin
3. **浏览器指纹**：Playwright 注入 stealth 脚本，抹 webdriver、伪装 chrome 对象
4. **行为节奏**：搜索间隔 2-3 秒随机延迟
5. **Cookie 持久化**：`cookies.json` 复用登录态

## 启动与配置

```bash
# === Linux ===
pip install -r requirements.txt
playwright install chromium
sudo apt install aria2       # Ubuntu 24.04 GnuTLS，CDN 不拦 ✅

# === Windows ===
pip install -r requirements.txt
playwright install chromium
# aria2: https://github.com/aria2/aria2/releases → aria2c.exe 放 PATH

# === 启动 ===
python app.py
```

`config.ini` 可配置：站点 URL、下载目录、并发数、端口、HTTP 服务模式。

## 故障排查

| 现象 | 原因 | 处理 |
|---|---|---|
| `无法获取视频信息` | embed 页加载或 flashvars 解析失败 | 检查网络、重试 |
| `aria2 未下载到任何分片` | Cookie 失效或 CDN 拒绝 | 重启服务刷新 Cookie，多试几次 |
| 下载卡在 10% | 选中了直链 mp4 | 指定 `quality="480"` 走 HLS |
| 视频画面跳动/卡顿 | （已修复：分片自然排序 + 5 次重试） | 不应再出现 |

## 限制

- 单站点（PornHub），多站点需改 config.ini + 搜索选择器
- 直链 mp4 在某些网络下 aria2 不通，自动走浏览器兜底（较慢，不常用）
- 单进程，无分布式
