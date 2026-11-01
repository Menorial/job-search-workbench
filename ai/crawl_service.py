# -*- coding: utf-8 -*-
"""网页面板里的爬取任务管理。

职责：
1. 校验前端传来的参数（城市/页数/详情数/格式），拼成安全的命令行（列表传参，不用 shell）
2. 以子进程方式运行 crawler/boss_cdp_spider.py，读取它的事件流与 stdout
3. 维护任务状态、日志缓冲、进度百分比，供 /api/crawl/* 轮询
4. 支持停止（结束子进程）、查看历史运行、下载结果文件

设计取舍：
- 用子进程而不是进程内调用：爬虫阻塞且可能崩溃/被强杀，隔离在子进程里
  不会把整个面板拖下水；同一套 CLI 代码也继续可单独使用。
- 进度用 JSONL 事件（spider --events）而不是解析日志文本，不依赖 emoji 文案。
"""

import json
import os
import subprocess
import sys
import threading
import time
from collections import deque

from common.chrome import cdp_alive, find_chrome
from common.paths import (
    AI_OUTPUT_DIR,
    CHROME_PROFILE_DIR,
    LOG_DIR,
    RESULTS_DIR,
    RESUME_DIR,
    ROOT_DIR,
    ensure_dir,
)

from config import CONCURRENCY, MAX_CANDIDATES

# crawler/ 是脚本目录而不是包，这里补上路径以便复用城市表
_CRAWLER_DIR = os.path.join(ROOT_DIR, "crawler")
if _CRAWLER_DIR not in sys.path:
    sys.path.append(_CRAWLER_DIR)

from cities import CITY_MAP, city_names, normalize_city, refresh_full  # noqa: E402

SPIDER = os.path.join(_CRAWLER_DIR, "boss_cdp_spider.py")
FETCH_CITIES = os.path.join(_CRAWLER_DIR, "fetch_cities.py")
AI_RUN = os.path.join(ROOT_DIR, "ai", "run.py")
CDP_PORT = 9222
MAX_LOG_LINES = 800
MAX_EVENTS = 50
RESUME_EXTS = (".pdf", ".docx", ".md", ".txt")

TASK_KINDS = ("crawl", "setup", "cities", "parse", "match", "report", "top10")

POPULAR_CITIES = [
    "北京", "上海", "广州", "深圳", "杭州", "成都",
    "武汉", "南京", "西安", "重庆", "天津", "苏州",
    "郑州", "长沙", "东莞", "青岛", "沈阳", "合肥",
    "佛山", "济南", "厦门", "哈尔滨", "福州", "大连",
]


class CrawlError(Exception):
    """参数错误或任务状态冲突。"""


# ============================================================
# 参数与命令
# ============================================================

def city_options():
    refresh_full()  # 更新过城市列表后，不用重启面板也能生效
    popular = [name for name in POPULAR_CITIES if name in CITY_MAP]
    return {"popular": popular, "all": ["全国"] + city_names()}


def validate_params(payload):
    """把前端参数归一化并校验；非法时抛 CrawlError。"""
    payload = payload or {}

    keyword = str(payload.get("keyword") or "").strip()
    if not keyword:
        raise CrawlError("搜索关键词不能为空")
    if len(keyword) > 50:
        raise CrawlError("关键词过长（最多 50 字）")

    city_input = str(payload.get("city") or "全国").strip() or "全国"
    city = normalize_city(city_input)
    if not city:
        raise CrawlError("未知城市: %s" % city_input)

    raw_pages = payload.get("pages")
    try:
        pages = 3 if raw_pages in (None, "") else int(raw_pages)
    except (TypeError, ValueError):
        raise CrawlError("页数必须是数字")
    if not 1 <= pages <= 30:
        raise CrawlError("页数需在 1-30 之间")

    raw_detail = payload.get("detail")
    try:
        detail = 0 if raw_detail in (None, "") else int(raw_detail)
    except (TypeError, ValueError):
        raise CrawlError("详情数量必须是数字")
    if not 0 <= detail <= 200:
        raise CrawlError("详情数量需在 0-200 之间")

    fmt = str(payload.get("format") or "json").lower()
    if fmt not in ("json", "csv"):
        raise CrawlError("输出格式只能是 json 或 csv")

    return {
        "keyword": keyword,
        "city": city,
        "pages": pages,
        "detail": detail,
        "format": fmt,
        "slow": bool(payload.get("slow")),
    }


def build_command(params, events_path, spider_path=None, python_exe=None):
    """列表传参，绝不经过 shell —— 避免命令注入。"""
    command = [
        python_exe or sys.executable, "-u", spider_path or SPIDER,
        "-k", params["keyword"],
        "-c", params["city"],
        "-p", str(params["pages"]),
        "-f", params["format"],
    ]
    if params.get("detail"):
        command += ["-d", str(params["detail"])]
    if params.get("slow"):
        command.append("--slow")
    if events_path:
        command += ["--events", events_path]
    return command


# ---------- AI 任务（解析简历 / 匹配 / 报告 / 话术） ----------

def _recorded_resume_path():
    """上次解析过的简历路径（允许重新解析这一个，即使它不在 data/resume 下）。"""
    try:
        with open(os.path.join(AI_OUTPUT_DIR, "resume_cache_meta.json"),
                  "r", encoding="utf-8") as handle:
            meta = json.load(handle)
        return os.path.abspath((meta.get("resume") or {}).get("path") or "")
    except Exception:
        return ""


def validate_parse(payload):
    payload = payload or {}
    raw = str(payload.get("path") or "").strip()
    if not raw:
        raise CrawlError("请选择要解析的简历文件")

    path = os.path.abspath(raw)
    root = os.path.abspath(RESUME_DIR)
    try:
        inside = os.path.commonpath([root, path]) == root
    except ValueError:
        inside = False
    if not inside and path != _recorded_resume_path():
        raise CrawlError("只能解析 data/resume 目录下的文件")

    if not os.path.isfile(path):
        raise CrawlError("简历文件不存在: %s" % os.path.basename(path))
    if os.path.splitext(path)[1].lower() not in RESUME_EXTS:
        raise CrawlError("只支持 %s 格式" % " / ".join(ext[1:] for ext in RESUME_EXTS))
    return {"path": path, "force": bool(payload.get("force"))}


def validate_match(payload):
    payload = payload or {}

    raw_max = payload.get("max")
    try:
        max_candidates = MAX_CANDIDATES if raw_max in (None, "") else int(raw_max)
    except (TypeError, ValueError):
        raise CrawlError("候选上限必须是数字")
    if not 1 <= max_candidates <= 500:
        raise CrawlError("候选上限需在 1-500 之间")

    raw_workers = payload.get("concurrency")
    try:
        concurrency = CONCURRENCY if raw_workers in (None, "") else int(raw_workers)
    except (TypeError, ValueError):
        raise CrawlError("并发数必须是数字")
    if not 1 <= concurrency <= 8:
        raise CrawlError("并发数需在 1-8 之间")

    return {
        "max": max_candidates,
        "concurrency": concurrency,
        "latest": bool(payload.get("latest")),
    }


def validate_top10(payload):
    payload = payload or {}
    raw_top = payload.get("top")
    try:
        top = 10 if raw_top in (None, "") else int(raw_top)
    except (TypeError, ValueError):
        raise CrawlError("推荐条数必须是数字")
    if not 1 <= top <= 50:
        raise CrawlError("推荐条数需在 1-50 之间")
    return {"top": top}


def build_ai_command(kind, params, events_path, python_exe=None):
    """构造 ai/run.py 的命令（列表传参，不经 shell）。"""
    command = [python_exe or sys.executable, "-u", AI_RUN]
    if kind == "parse":
        command += ["parse", "-f", params["path"]]
        if params.get("force"):
            command.append("--force")
    elif kind == "match":
        command += ["match", "-n", str(params["max"]), "-j", str(params["concurrency"])]
        if params.get("latest"):
            command.append("--latest")
    elif kind == "report":
        command += ["report"]
    elif kind == "top10":
        command += ["top10", "-n", str(params["top"])]
    else:
        raise CrawlError("未知任务类型: %s" % kind)

    if events_path:
        command += ["--events", events_path]
    return command


# ============================================================
# 进度推导（纯函数，便于测试）
# ============================================================

PHASE_LABEL = {
    "search": "正在抓取列表",
    "detail": "正在抓取详情",
    "parse": "正在解析简历",
    "match": "正在 AI 匹配",
    "report": "正在生成报告",
    "top10": "正在生成推荐话术",
    "crawl": "正在抓取职位",
    "cities": "正在更新城市列表",
}


def derive_progress(events, task=None):
    """从事件流推导「阶段 / 进度百分比 / 文案」。

    爬取任务：列表占 50%，详情占 50%（不抓详情则列表占 100%）。
    AI 任务：直接按 progress 的 current/total 计算。
    """
    task = task or {}
    kind = task.get("kind") or "crawl"
    pages = int(task.get("pages") or 0)
    wants_detail = int(task.get("detail") or 0) > 0

    phase = "starting"
    current = total = 0
    text = "准备中…"
    if task.get("kind") == "setup" and not events:
        text = "正在启动 Chrome，请在浏览器窗口完成登录…"
    if task.get("kind") == "cities" and not events:
        text = "正在从官网更新城市列表…"

    for event in events or []:
        name = event.get("event")
        if name == "start":
            pages = int(event.get("pages") or pages or 0)
            wants_detail = int(event.get("detail") or 0) > 0
            text = "已启动…"
        elif name == "phase":
            phase = event.get("name") or phase
            total = int(event.get("total") or 0)
            if kind in ("crawl", "setup"):
                text = "正在抓取列表…" if phase == "search" else "正在抓取职位详情…"
            else:
                label = PHASE_LABEL.get(phase, "正在处理")
                text = "%s（%d 个）…" % (label, total) if total else "%s…" % label
        elif name == "page":
            phase = "search"
            current = int(event.get("page") or 0)
            total = int(event.get("pages") or total or pages)
            text = "列表第 %d/%d 页，已获取 %d 条" % (
                current, total, int(event.get("total") or 0))
        elif name == "list_done":
            phase = "search"
            current = total = 1
            text = "列表完成，共 %d 条" % int(event.get("count") or 0)
        elif name == "detail":
            phase = "detail"
            current = int(event.get("index") or 0)
            total = int(event.get("total") or total or 0)
            text = "详情 %d/%d：%s" % (current, total, event.get("title") or "")
        elif name == "detail_done":
            phase = "detail"
            current = total = 1
            text = "详情完成（成功 %d，失败 %d）" % (
                int(event.get("success") or 0), int(event.get("failed") or 0))
        elif name == "progress":
            phase = event.get("phase") or phase
            current = int(event.get("current") or current)
            total = int(event.get("total") or total or 0)
            label = PHASE_LABEL.get(phase, "正在处理")
            detail = event.get("label") or ""
            text = "%s %d/%d%s" % (label, current, total, ("：" + detail) if detail else "")
        elif name == "saved":
            phase = "done"
            current = total = 1
            count = int(event.get("jobs") or event.get("count") or 0)
            text = "已完成%s" % (("，%d 条" % count) if count else "")
        elif name == "error":
            text = event.get("message") or "出错"
        elif name == "done" and event.get("status") == "ok" and phase != "done":
            phase = "done"
            current = total = 1
            text = text or "已完成"

    if phase == "done":
        percent = 100.0
    elif kind in ("crawl", "setup"):
        if phase == "detail":
            percent = 50.0 + (50.0 * current / total if total else 0.0)
        elif phase == "search":
            frac = (float(current) / total) if total else 0.0
            percent = frac * (50.0 if wants_detail else 100.0)
        else:
            percent = 0.0
    else:
        percent = (100.0 * current / total) if total else 0.0

    return {
        "phase": phase,
        "current": current,
        "total": total,
        "percent": max(0, min(100, int(round(percent)))),
        "text": text,
    }


# ============================================================
# 任务运行器
# ============================================================

class TaskRunner(object):
    """同一时间只允许一个任务（Chrome 调试端口只有一个，串行更安全）。"""

    def __init__(self, spider_path=None, python_exe=None, log_dir=None):
        self.spider_path = spider_path or SPIDER
        self.python_exe = python_exe or sys.executable
        self.log_dir = log_dir or LOG_DIR
        self._lock = threading.Lock()
        self._proc = None
        self._task = None
        self._lines = deque(maxlen=MAX_LOG_LINES)
        self._seq = 0
        self._status = "idle"
        self._returncode = None
        self._started = None
        self._finished = None
        self._log_path = None
        self._events_path = None
        self._last_error = ""

    # ---------- 环境 ----------
    def env(self):
        return {
            "chrome_found": bool(find_chrome()),
            "cdp_alive": cdp_alive(CDP_PORT),
            "cdp_port": CDP_PORT,
            "profile": CHROME_PROFILE_DIR,
            "python": self.python_exe,
            "spider": self.spider_path,
        }

    # ---------- 启动 ----------
    def start(self, payload):
        """兼容旧调用：等价于 start_crawl。"""
        return self.start_crawl(payload)

    def start_crawl(self, payload):
        params = validate_params(payload)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        events_path = os.path.join(ensure_dir(self.log_dir),
                                   "crawl_%s.events.jsonl" % stamp)
        command = build_command(params, events_path, self.spider_path, self.python_exe)
        return self._launch(command, dict(params, kind="crawl", id=stamp), events_path)

    def start_setup(self, login_wait=90):
        try:
            wait = int(login_wait or 90)
        except (TypeError, ValueError):
            wait = 90
        wait = max(10, min(600, wait))
        stamp = time.strftime("%Y%m%d_%H%M%S")
        command = [self.python_exe, "-u", self.spider_path,
                   "--setup", "--login-wait", str(wait)]
        return self._launch(command, {"kind": "setup", "id": stamp, "login_wait": wait}, None)

    def start_task(self, kind, payload=None):
        """按类型启动任务：crawl / setup / parse / match / report / top10。"""
        payload = payload or {}
        kind = str(kind or "").strip().lower()
        if kind not in TASK_KINDS:
            raise CrawlError("未知任务类型: %s" % kind)
        if kind == "crawl":
            return self.start_crawl(payload)
        if kind == "setup":
            return self.start_setup(payload.get("login_wait", 90))
        if kind == "cities":
            stamp = time.strftime("%Y%m%d_%H%M%S")
            command = [self.python_exe, "-u", FETCH_CITIES]
            return self._launch(command, {"kind": "cities", "id": stamp}, None)

        if kind == "parse":
            params = validate_parse(payload)
        elif kind == "match":
            params = validate_match(payload)
        elif kind == "top10":
            params = validate_top10(payload)
        else:  # report
            params = {}

        stamp = time.strftime("%Y%m%d_%H%M%S")
        events_path = os.path.join(ensure_dir(self.log_dir),
                                   "task_%s.events.jsonl" % stamp)
        command = build_ai_command(kind, params, events_path, self.python_exe)
        return self._launch(command, dict(params, kind=kind, id=stamp), events_path)

    def _launch(self, command, task, events_path):
        with self._lock:
            if self._status == "running":
                raise CrawlError("已有任务在运行，请先停止或等待完成")
            stamp = task.get("id") or time.strftime("%Y%m%d_%H%M%S")
            self._log_path = os.path.join(ensure_dir(self.log_dir), "task_%s.log" % stamp)
            self._events_path = events_path
            self._lines.clear()
            self._seq = 0
            self._task = dict(task)
            self._status = "running"
            self._returncode = None
            self._finished = None
            self._started = time.time()
            self._last_error = ""

            env = dict(os.environ)
            env["PYTHONIOENCODING"] = "utf-8"   # 子进程输出统一 UTF-8，避免中文乱码
            env["PYTHONUNBUFFERED"] = "1"
            creationflags = 0
            if os.name == "nt":
                # 从面板启动时不要额外弹一个控制台窗口
                creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            try:
                proc = subprocess.Popen(
                    command,
                    cwd=ROOT_DIR,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    env=env,
                    creationflags=creationflags,
                )
            except OSError as exc:
                self._status = "failed"
                self._last_error = str(exc)
                raise CrawlError("启动爬虫进程失败: %s" % exc)

            self._proc = proc
            threading.Thread(target=self._reader, args=(proc, self._log_path), daemon=True).start()
            threading.Thread(target=self._waiter, args=(proc,), daemon=True).start()
            return self._snapshot_locked()

    # ---------- 后台线程 ----------
    def _reader(self, proc, log_path):
        try:
            with open(log_path, "w", encoding="utf-8") as log_file:
                for line in iter(proc.stdout.readline, ""):
                    line = line.rstrip("\r\n")
                    if not line:
                        continue
                    log_file.write(line + "\n")
                    log_file.flush()
                    with self._lock:
                        self._seq += 1
                        self._lines.append((self._seq, line))
        except Exception:
            pass
        finally:
            try:
                proc.stdout.close()
            except Exception:
                pass

    def _waiter(self, proc):
        try:
            code = proc.wait()
        except Exception:
            code = -1
        with self._lock:
            self._returncode = code
            self._finished = time.time()
            if self._status == "running":
                self._status = "done" if code == 0 else "failed"
                if code != 0 and not self._last_error:
                    self._last_error = "进程退出码 %d" % code

    # ---------- 状态 ----------
    def _snapshot_locked(self):
        task = dict(self._task or {})
        elapsed = round(((self._finished or time.time()) - self._started), 1) if self._started else 0
        task["elapsed"] = elapsed
        return task

    def status(self, since=0):
        with self._lock:
            lines = [line for seq, line in self._lines if seq > since]
            finished = self._finished or time.time()
            snapshot = {
                "status": self._status,
                "seq": self._seq,
                "lines": lines,
                "returncode": self._returncode,
                "log_file": self._log_path,
                "task": dict(self._task or {}) or None,
                "elapsed": round(finished - self._started, 1) if self._started else 0,
                "error": self._last_error,
            }
        events = self._read_events()
        snapshot["events"] = events[-MAX_EVENTS:]
        if snapshot["status"] == "idle" and not snapshot["task"]:
            snapshot["progress"] = {"phase": "idle", "current": 0, "total": 0,
                                    "percent": 0, "text": "等待开始"}
        else:
            snapshot["progress"] = derive_progress(events, snapshot.get("task"))
        return snapshot

    def _read_events(self):
        if not self._events_path or not os.path.exists(self._events_path):
            return []
        events = []
        try:
            with open(self._events_path, "r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        events.append(json.loads(line))
                    except ValueError:
                        continue
        except OSError:
            return []
        return events

    def stop(self):
        with self._lock:
            proc = self._proc
            if not proc or self._status != "running":
                return False
            self._status = "cancelled"
        try:
            proc.kill()
        except Exception:
            pass
        return True


# ============================================================
# 历史与结果文件
# ============================================================

def read_history(limit=10):
    path = os.path.join(LOG_DIR, "run_history.jsonl")
    if not os.path.exists(path):
        return []
    records = []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return []
    return list(reversed(records[-max(1, int(limit)):]))


def resolve_result_file(name):
    """只允许下载 data/boss_results 下的 json/csv，防目录穿越。"""
    name = os.path.basename(str(name or "").strip())
    if not name or not name.lower().endswith((".json", ".csv")):
        raise CrawlError("只支持下载 json / csv 结果文件")
    path = os.path.join(RESULTS_DIR, name)
    if not os.path.isfile(path):
        raise CrawlError("结果文件不存在: %s" % name)
    return path


# 兼容旧名字：面板最早只有爬取任务
CrawlRunner = TaskRunner

