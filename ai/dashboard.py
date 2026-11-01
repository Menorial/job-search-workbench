# -*- coding: utf-8 -*-
"""可视化数据面板（FastAPI + ECharts）。

启动：
    python run.py dashboard            # 本地 8000 端口
    python run.py dashboard -p 8080    # 指定端口

性能说明：
- 职位数据由 data_store 统一加载（按文件 mtime 缓存），不再每个请求重新解析所有 JSON
- /api/jobs 返回的职位描述截断到 1200 字（列表页用不到全文），大幅减小响应体积
"""

import collections
import json
import os
import re
import socket
import sys
import threading
import time
import webbrowser

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from common.console import setup_console
from common.job_fields import normalize_education, normalize_experience
from common.paths import AI_DIR, AI_OUTPUT_DIR as OUTPUT_DIR, RESUME_DIR, ensure_dir
from common.salary import salary_from_job, salary_stats
from common.skills import clean_skills
from common.text import normalize_markdown_lists, sanitize_filename

from config import MATCH_THRESHOLD
from crawl_service import (
    RESUME_EXTS,
    CrawlError,
    CrawlRunner,
    city_options,
    read_history,
    resolve_result_file,
)
from data_store import load_all_jobs, result_files
from settings_service import clear_cache, settings_snapshot, update_settings, usage_summary

try:
    from fastapi import Body, FastAPI, HTTPException, Request
    from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

    import uvicorn

    _DEPS_OK = True
except ImportError:  # 缺依赖时不让 import 直接退出，交给 run_server 提示
    _DEPS_OK = False
    FastAPI = None  # type: ignore
    HTMLResponse = JSONResponse = FileResponse = None  # type: ignore
    Body = HTTPException = Request = None  # type: ignore
    uvicorn = None  # type: ignore

DESCRIPTION_LIMIT = 1200

# 前端 index.html 里有同名版本号；两边不一致时会提示重启面板
APP_VERSION = 5
_STARTED_AT = time.time()

# 爬取任务运行器（同一时间只跑一个任务）
runner = CrawlRunner()

# ============ 数据加载 ============

def load_matched():
    path = os.path.join(OUTPUT_DIR, "matched_jobs.json")
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def load_resume():
    path = os.path.join(OUTPUT_DIR, "resume_structured.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle) or {}
    except Exception:
        return {}


def load_report_md():
    path = os.path.join(OUTPUT_DIR, "report.md")
    if not os.path.exists(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
    except Exception:
        return ""


def _read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return default


def _file_meta(path):
    """返回 (格式化时间, 时间戳)；文件不存在时返回 ("", 0)。"""
    try:
        stamp = os.path.getmtime(path)
    except OSError:
        return "", 0
    return time.strftime("%m-%d %H:%M", time.localtime(stamp)), stamp


# ============ 统计（本地，不调 LLM） ============

def compute_stats(jobs):
    salaries = [value for value in (salary_from_job(job) for job in jobs) if value]
    salary = salary_stats(salaries)

    city_counter = collections.Counter()
    for job in jobs:
        location = job.get("location") or job.get("cityName") or ""
        city_counter[location.split()[0] if location else "未知"] += 1

    edu_counter = collections.Counter()
    for job in jobs:
        edu_counter[normalize_education(job.get("education"))] += 1

    exp_counter = collections.Counter()
    for job in jobs:
        exp_counter[normalize_experience(job.get("experience"))] += 1

    skill_counter = collections.Counter()
    for job in jobs:
        for skill in clean_skills(job.get("skills")):
            skill_counter[skill] += 1

    return {
        "total": len(jobs),
        "salary_sample": salary["sample"],
        "salary_min": salary["min"],
        "salary_max": salary["max"],
        "salary_median_low": salary["median_low"],
        "salary_median_high": salary["median_high"],
        "cities": city_counter.most_common(15),
        "education": edu_counter.most_common(8),
        "experience": exp_counter.most_common(10),
        "skills": skill_counter.most_common(20),
    }


def compute_overview(jobs, matched):
    scores = [item.get("score", 0) for item in matched if isinstance(item, dict)]
    return {
        "total_jobs": len(jobs),
        "matched_count": len(matched),
        "avg_score": round(sum(scores) / len(scores), 1) if scores else 0,
        "recommend_count": sum(1 for score in scores if score >= MATCH_THRESHOLD),
        "top_score": max(scores) if scores else 0,
    }


def _matched_map(matched):
    mapping = {}
    for item in matched:
        job = item.get("job") or {}
        job_id = job.get("job_id") or job.get("jobId")
        if job_id:
            mapping[job_id] = item
    return mapping


# ============ 报告 HTML 清洗 ============

_DANGEROUS_TAG = re.compile(
    r"<\s*(script|iframe|object|embed|link|meta|style|form)\b[^>]*>.*?<\s*/\s*\1\s*>"
    r"|<\s*(script|iframe|object|embed|link|meta|style|form)\b[^>]*/?\s*>",
    re.IGNORECASE | re.DOTALL,
)
_INLINE_HANDLER = re.compile(r"\son\w+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", re.IGNORECASE)
_JS_URL = re.compile(r"""href\s*=\s*("|')\s*javascript:[^"']*\1""", re.IGNORECASE)


def sanitize_html(html):
    """报告由模型生成，渲染前去掉脚本/内联事件，避免本地面板被注入。"""
    html = _DANGEROUS_TAG.sub("", html or "")
    html = _INLINE_HANDLER.sub("", html)
    html = _JS_URL.sub('href="#"', html)
    return html


# ============ FastAPI ============

app = FastAPI(title="AI 求职助手面板") if _DEPS_OK else None


if _DEPS_OK:

    @app.get("/api/version")
    def api_version():
        return {
            "version": APP_VERSION,
            "pid": os.getpid(),
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(_STARTED_AT)),
        }

    @app.get("/api/overview")
    def api_overview():
        jobs = load_all_jobs()
        matched = load_matched()
        overview = compute_overview(jobs, matched)
        overview["with_detail"] = sum(
            1 for job in jobs if len(job.get("job_description") or "") >= 20)
        stamps = [item.get("matched_at") for item in matched if item.get("matched_at")]
        overview["matched_at"] = max(stamps) if stamps else ""
        files = result_files()
        overview["files"] = len(files)
        overview["updated_at"] = (
            time.strftime("%m-%d %H:%M", time.localtime(files[-1][1])) if files else ""
        )
        overview["updated_ts"] = files[-1][1] if files else 0
        return {"overview": overview, "threshold": MATCH_THRESHOLD}

    @app.get("/api/stats")
    def api_stats():
        return {"stats": compute_stats(load_all_jobs())}

    @app.get("/api/jobs")
    def api_jobs():
        jobs = [dict(job) for job in load_all_jobs()]
        matched = _matched_map(load_matched())
        for job in jobs:
            job["education"] = normalize_education(job.get("education"))
            job["experience"] = normalize_experience(job.get("experience"))
            item = matched.get(job.get("job_id") or job.get("jobId"))
            if item:
                job["match_score"] = item.get("score", 0)
                job["match_dims"] = item.get("dims", {})
                job["match_reasons"] = item.get("reasons", [])
                job["match_risks"] = item.get("risks", [])
                job["cover_tips"] = item.get("cover_tips", "")
            else:
                job["match_score"] = None
            description = job.get("job_description") or ""
            if len(description) > DESCRIPTION_LIMIT:
                job["job_description"] = description[:DESCRIPTION_LIMIT] + "…"
        return {"jobs": jobs, "total": len(jobs), "threshold": MATCH_THRESHOLD}

    @app.get("/api/resume")
    def api_resume():
        return {"resume": load_resume()}

    @app.get("/api/report")
    def api_report():
        return {"report": load_report_md()}

    @app.get("/api/report_html")
    def api_report_html():
        md_text = load_report_md()
        if not md_text:
            return {"html": "<div class='empty'>暂无报告，请先运行 python run.py report</div>",
                    "updated_at": "", "updated_ts": 0}
        try:
            import markdown as md
        except ImportError:
            return {"html": "<div class='empty'>缺少 markdown 依赖：pip install markdown</div>",
                    "updated_at": "", "updated_ts": 0}
        body = md.markdown(normalize_markdown_lists(md_text),
                           extensions=["tables", "fenced_code", "sane_lists"])
        updated_at, updated_ts = _file_meta(os.path.join(OUTPUT_DIR, "report.md"))
        return {
            "html": "<div class=\"report-box\">%s</div>" % sanitize_html(body),
            "updated_at": updated_at,
            "updated_ts": updated_ts,
        }

    # ---------- 网页爬取 ----------

    @app.get("/api/cities")
    def api_cities():
        return city_options()

    @app.get("/api/crawl/env")
    def api_crawl_env():
        return runner.env()

    @app.post("/api/crawl/start")
    def api_crawl_start(payload: dict):
        try:
            task = runner.start(payload)
        except CrawlError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"task": task}

    @app.post("/api/crawl/setup")
    def api_crawl_setup(payload: dict = Body(default=None)):
        try:
            task = runner.start_setup((payload or {}).get("login_wait", 90))
        except CrawlError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"task": task}

    @app.post("/api/crawl/stop")
    def api_crawl_stop():
        return {"stopped": runner.stop()}

    @app.get("/api/crawl/status")
    def api_crawl_status(since: int = 0):
        return runner.status(since=max(0, since))

    @app.get("/api/crawl/history")
    def api_crawl_history(limit: int = 10):
        return {"items": read_history(min(max(limit, 1), 50))}

    @app.get("/api/crawl/result")
    def api_crawl_result(name: str):
        try:
            path = resolve_result_file(name)
        except CrawlError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        return FileResponse(path, filename=os.path.basename(path))

    # ---------- 通用任务（解析简历 / 匹配 / 报告 / 话术 / 爬取） ----------

    @app.post("/api/tasks/start")
    def api_tasks_start(payload: dict = Body(default=None)):
        payload = payload or {}
        kind = str(payload.get("kind") or "crawl").strip().lower()
        params = {key: value for key, value in payload.items() if key != "kind"}
        try:
            task = runner.start_task(kind, params)
        except CrawlError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"task": task}

    @app.get("/api/tasks/status")
    def api_tasks_status(since: int = 0):
        return runner.status(since=max(0, since))

    @app.post("/api/tasks/stop")
    def api_tasks_stop():
        return {"stopped": runner.stop()}

    # ---------- 简历 ----------

    @app.get("/api/resume/files")
    def api_resume_files():
        files = []
        if os.path.isdir(RESUME_DIR):
            for name in sorted(os.listdir(RESUME_DIR)):
                path = os.path.join(RESUME_DIR, name)
                if not os.path.isfile(path):
                    continue
                if os.path.splitext(name)[1].lower() not in RESUME_EXTS:
                    continue
                stat = os.stat(path)
                files.append({
                    "name": name,
                    "path": path,
                    "size": stat.st_size,
                    "mtime": time.strftime("%m-%d %H:%M", time.localtime(stat.st_mtime)),
                })

        meta = _read_json(os.path.join(OUTPUT_DIR, "resume_cache_meta.json"), {}) or {}
        current = ((meta.get("resume") or {}).get("path")) or ""
        current_abs = os.path.abspath(current) if current else ""
        for item in files:
            item["is_current"] = bool(current_abs) and os.path.abspath(item["path"]) == current_abs
        return {
            "files": files,
            "current": current,
            "current_exists": bool(current_abs) and os.path.isfile(current_abs),
            "structured": load_resume(),
        }

    @app.post("/api/resume/upload")
    async def api_resume_upload(request: Request, name: str = ""):
        filename = os.path.basename(str(name or "").strip())
        ext = os.path.splitext(filename)[1].lower()
        if ext not in RESUME_EXTS:
            raise HTTPException(status_code=400,
                                detail="只支持 %s 格式" % " / ".join(e[1:] for e in RESUME_EXTS))

        body = await request.body()
        if not body:
            raise HTTPException(status_code=400, detail="文件内容为空")
        if len(body) > 10 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="文件不能超过 10MB")

        stem = os.path.splitext(filename)[0]
        safe = sanitize_filename(stem, 60, "resume") + ext
        path = os.path.join(ensure_dir(RESUME_DIR), safe)
        with open(path, "wb") as handle:
            handle.write(body)
        return {"file": {"name": safe, "path": path, "size": len(body)}}

    # ---------- 推荐话术 ----------

    @app.get("/api/top10")
    def api_top10():
        path = os.path.join(OUTPUT_DIR, "top10.md")
        if not os.path.exists(path):
            return {"html": "", "markdown": "", "updated_at": "", "updated_ts": 0}
        try:
            with open(path, "r", encoding="utf-8") as handle:
                raw = handle.read()
        except OSError:
            return {"html": "", "markdown": "", "updated_at": "", "updated_ts": 0}

        html = ""
        try:
            import markdown as md
            html = sanitize_html(md.markdown(
                normalize_markdown_lists(raw),
                extensions=["tables", "fenced_code", "sane_lists"]))
        except ImportError:
            html = "<pre>%s</pre>" % raw
        updated_at, updated_ts = _file_meta(path)
        try:
            matched_ts = os.path.getmtime(os.path.join(OUTPUT_DIR, "matched_jobs.json"))
        except OSError:
            matched_ts = 0
        return {
            "html": html,
            "markdown": raw,
            "updated_at": updated_at,
            "updated_ts": updated_ts,
            # 匹配结果比话术新 -> 话术可能过期
            "stale": bool(updated_ts and matched_ts > updated_ts + 1),
        }

    # ---------- 设置 / 缓存 / 用量 ----------

    @app.get("/api/settings")
    def api_settings():
        return settings_snapshot()

    @app.post("/api/settings")
    def api_settings_update(payload: dict = Body(default=None)):
        try:
            return update_settings(payload or {})
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.post("/api/cache/clear")
    def api_cache_clear(payload: dict = Body(default=None)):
        try:
            return clear_cache((payload or {}).get("kind"))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.get("/api/usage")
    def api_usage():
        return usage_summary()

    @app.get("/", response_class=HTMLResponse)
    def index():
        with open(os.path.join(AI_DIR, "templates", "index.html"), "r", encoding="utf-8") as handle:
            return handle.read()

    @app.get("/favicon.ico")
    def favicon():
        return JSONResponse({}, status_code=204)


def _port_available(port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def run_server(port=8000, open_browser=True):
    setup_console()
    if not _DEPS_OK:
        print("❌ 缺少面板依赖，请先安装: pip install -r requirements.txt")
        print("   （需要 fastapi / uvicorn / markdown）")
        return 1
    if not _port_available(port):
        print("❌ 端口 %d 已被占用，请换一个: python run.py dashboard -p 8080" % port)
        return 1

    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open("http://127.0.0.1:%d" % port)).start()
    print("🌐 面板已启动: http://127.0.0.1:%d" % port)
    print("   按 Ctrl+C 停止")
    try:
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    except KeyboardInterrupt:
        print("\n👋 面板已停止")
    return 0


if __name__ == "__main__":
    port = 8000
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            pass
    sys.exit(run_server(port))
