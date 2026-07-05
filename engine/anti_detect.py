"""
反检测模块 - 浏览器指纹伪装、UA 池、Cookie 持久化

核心原则：模拟真人浏览器的行为特征
  - 浏览器指纹（Canvas/WebGL/navigator 通过 Playwright stealth）
  - HTTP 头（UA 池随机化）
  - 行为节奏（随机延迟、人类化操作间隔）
  - Cookie 持久化（登录态复用）
"""
import json
import random
from pathlib import Path


# ============ UA 池 ============

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
]


def random_ua() -> str:
    """随机返回一个 User-Agent"""
    return random.choice(USER_AGENTS)


# ============ Playwright Stealth 脚本 ============

STEALTH_SCRIPT = """
// 抹掉 webdriver 标记
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});

// 伪装 chrome 对象
window.chrome = {
    runtime: {},
    loadTimes: function() {},
    csi: function() {},
    app: {}
};

// 伪装 plugins 数组（真实浏览器有插件）
Object.defineProperty(navigator, 'plugins', {
    get: () => [1, 2, 3, 4, 5]
});

// 伪装 languages
Object.defineProperty(navigator, 'languages', {
    get: () => ['en-US', 'en']
});

// 覆盖 permissions 查询
const originalQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (parameters) => (
    parameters.name === 'notifications' ?
        Promise.resolve({state: Notification.permission}) :
        originalQuery(parameters)
);
"""


# ============ Cookie 持久化 ============

COOKIE_FILE: str = "cookies.json"


def load_cookies() -> list[dict]:
    """从磁盘加载 Cookie"""
    path = Path(COOKIE_FILE)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []
    return []


def save_cookies(cookies: list[dict]):
    """保存 Cookie 到磁盘"""
    Path(COOKIE_FILE).write_text(
        json.dumps(cookies, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )


# ============ 人类化延迟 ============

import asyncio


async def human_delay(min_sec: float = 1.0, max_sec: float = 3.0):
    """随机延迟，模拟人类的操作间隔"""
    await asyncio.sleep(random.uniform(min_sec, max_sec))
