# -*- coding: utf-8 -*-
"""Chrome 可执行文件定位 + CDP 端口/标签页管理。

原项目把 CHROME_PATH 硬编码在 3 个文件里，用户只能改源码。
现在的查找顺序：环境变量 CHROME_PATH → 注册表 App Paths → 常见安装路径 → PATH。
"""

import os
import shutil
import subprocess
import time
from urllib.parse import quote

try:
    import requests
except ImportError:  # pragma: no cover - 运行期提示
    requests = None


class ChromeError(RuntimeError):
    pass


_COMMON_PATHS = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files\Google\Chrome Beta\Application\chrome.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
)


def _from_registry():
    try:
        import winreg
    except ImportError:
        return None
    key = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, key) as handle:
                path = winreg.QueryValue(handle, None)
            if path and os.path.exists(path):
                return path
        except OSError:
            continue
    return None


def _from_local_appdata():
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        return None
    path = os.path.join(base, "Google", "Chrome", "Application", "chrome.exe")
    return path if os.path.exists(path) else None


def find_chrome():
    """返回 Chrome（或 Edge）可执行文件路径，找不到返回 None。"""
    env_path = os.environ.get("CHROME_PATH")
    if env_path and os.path.exists(env_path):
        return env_path
    for finder in (_from_registry, _from_local_appdata):
        path = finder()
        if path:
            return path
    for path in _COMMON_PATHS:
        if os.path.exists(path):
            return path
    for exe in ("chrome", "chrome.exe", "msedge", "msedge.exe"):
        found = shutil.which(exe)
        if found:
            return found
    return None


# ---------------- CDP 端口 / 标签页 ----------------

def cdp_url(port, suffix="/json/version"):
    return "http://127.0.0.1:%d%s" % (int(port), suffix)


def cdp_alive(port, timeout=1.5):
    """CDP 调试端口是否就绪。"""
    if requests is None:
        return False
    try:
        resp = requests.get(cdp_url(port), timeout=timeout)
        return resp.status_code == 200
    except Exception:
        return False


def list_targets(port, timeout=5):
    """列出所有调试目标（标签页/Service Worker 等）。"""
    if requests is None:
        raise ChromeError("缺少依赖 requests，请先 pip install requests")
    try:
        resp = requests.get(cdp_url(port, "/json/list"), timeout=timeout)
        data = resp.json()
        return data if isinstance(data, list) else []
    except Exception as exc:
        raise ChromeError("读取 CDP 目标列表失败: %s" % exc)


def _pages(targets):
    return [t for t in targets if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]


def open_tab(port, url, timeout=8):
    """新开一个标签页，返回它的 webSocketDebuggerUrl（新版 Chrome 要求 PUT）。"""
    if requests is None:
        return None
    endpoint = cdp_url(port, "/json/new?%s" % quote(url, safe=""))
    for method in ("put", "get"):
        try:
            resp = getattr(requests, method)(endpoint, timeout=timeout)
            if resp.status_code == 200:
                return (resp.json() or {}).get("webSocketDebuggerUrl")
        except Exception:
            continue
    return None


def ensure_zhipin_target(port, url="https://www.zhipin.com/web/geek/job"):
    """返回一个可用的 zhipin 页面 WS 地址；没有就新开标签页（不劫持用户当前页面）。"""
    pages = _pages(list_targets(port))
    for target in pages:
        if "zhipin.com" in (target.get("url") or ""):
            return target["webSocketDebuggerUrl"]
    ws_url = open_tab(port, url)
    if ws_url:
        return ws_url
    # 兜底：任意页面（例如用户手动打开的其他页），最后手段
    return pages[0]["webSocketDebuggerUrl"] if pages else None


def launch_chrome(profile_dir, port, url, chrome_path=None, wait_s=20):
    """以远程调试模式启动 Chrome（用项目专用 profile，不影响日常浏览器）。"""
    exe = chrome_path or find_chrome()
    if not exe:
        raise ChromeError(
            "未找到 Chrome。请安装 Chrome，或设置环境变量 CHROME_PATH 指向 chrome.exe"
        )
    os.makedirs(profile_dir, exist_ok=True)
    args = [
        exe,
        "--remote-debugging-port=%d" % int(port),
        "--remote-allow-origins=*",
        "--user-data-dir=%s" % profile_dir,
        "--no-first-run",
        "--no-default-browser-check",
        url,
    ]
    subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    deadline = time.time() + max(5, int(wait_s))
    while time.time() < deadline:
        if cdp_alive(port):
            return True
        time.sleep(0.5)
    return False
