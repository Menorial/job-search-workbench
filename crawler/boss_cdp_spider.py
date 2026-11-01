#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Boss直聘爬虫 —— 基于 Chrome CDP（真实浏览器上下文）。

相对旧版的关键改动：
1. 详情抓取以「页面真的渲染出职位描述」为成功判据；空描述不再写入断点、
   不会再被永久跳过（旧版会把加载失败的页面当成成功缓存）。
2. 关键词统一走 json.dumps + encodeURIComponent，不再把用户输入直接拼进 JS。
3. 跨页按 job_id 去重；翻页遇到 code != 0 明确告警，不再静默截断。
4. 断点文件改为 JSONL 追加写（旧版每抓一条重写整个 JSON，O(n^2)）。
5. Ctrl+C 中断也会把已抓数据落盘；详情失败自动重试（默认最多 3 次）。
6. 复用一条 CDP 长连接，并等待「元素出现」而不是固定 sleep(3)。
"""

import argparse
import csv
import difflib
import json
import os
import random
import re
import sys
import time
from datetime import datetime
from urllib.parse import quote

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from common.cdp import CDPClient, CDPError
from common.chrome import ChromeError, cdp_alive, ensure_zhipin_target, find_chrome, launch_chrome
from common.console import setup_console
from common.events import EventEmitter
from common.history import HISTORY_FILE, record as record_history
from common.job_fields import normalize_education
from common.log import get_logger, setup_logging
from common.paths import CHROME_PROFILE_DIR, DATA_DIR, RESULTS_DIR, ensure_dir
from common.salary import parse_salary
from common.text import coerce_int, first_line, sanitize_filename
from cities import CITY_MAP, city_pattern, normalize_city

CDP_PORT = 9222
SEARCH_URL = "https://www.zhipin.com/web/geek/job"
DETAIL_URL = "https://www.zhipin.com/job_detail/%s.html"

# 输出字段（固定顺序，保证不同批次 JSON/CSV 列一致）
FIELDS = [
    "job_id", "job_name", "company_name", "salary", "salary_min", "salary_max",
    "location", "experience", "education", "company_type", "company_size",
    "skills", "job_labels", "welfare", "days_per_week", "least_month",
    "security_id", "lid", "hr_name", "hr_title",
    "job_description", "job_address", "job_url",
]

# 断点：新版 JSONL（追加写），旧版 JSON 仍可读
PROGRESS_JSONL = os.path.join(RESULTS_DIR, "_detail_progress.jsonl")
PROGRESS_LEGACY = os.path.join(RESULTS_DIR, "_detail_progress.json")

MAX_DETAIL_ATTEMPTS = 3          # 同一职位详情最多尝试次数（失败可自动重试）
MAX_CONSECUTIVE_FAILURES = 5     # 连续失败多少条后停止，避免把封禁刷得更狠

# 学历关键词（用于从「城市 经验 学历」这类混排文本里认出学历）
_DEGREE_HINTS = ("博士", "硕士", "本科", "大专", "中专", "中技", "高中", "初中")
_EXP_RE = re.compile(r"(\d+\s*[-~]\s*\d+\s*年|\d+\s*年|应届|在校|经验不限|无经验)")
_CITY_RE = None


# ============================================================
# 工具
# ============================================================

def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _json_literal(value):
    """把 Python 字符串转成安全的 JS 字符串字面量。"""
    return json.dumps("" if value is None else str(value), ensure_ascii=False)


def write_json_atomic(path, data):
    """先写临时文件再替换，避免中途崩溃留下半截 JSON。"""
    ensure_dir(os.path.dirname(path))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def resolve_city(value):
    """城市输入 -> (城市名, 城市代码)。支持名称、'成都市'、城市代码、序号。"""
    value = str(value or "").strip()
    if not value:
        return None, None
    if value.isdigit():
        if value in CITY_MAP.values():
            for name, code in CITY_MAP.items():
                if code == value:
                    return name, code
        index = int(value) - 1
        names = list(CITY_MAP.keys())
        if 0 <= index < len(names):
            return names[index], CITY_MAP[names[index]]
        return None, None
    name = normalize_city(value)
    if name:
        return name, CITY_MAP[name]
    return None, None


def suggest_cities(value, limit=6):
    """给输错的城市名一个提示（'成都省' -> '成都'）。"""
    try:
        return difflib.get_close_matches(str(value), list(CITY_MAP.keys()), n=limit, cutoff=0.4)
    except Exception:
        return []


def _city_re():
    global _CITY_RE
    if _CITY_RE is None:
        _CITY_RE = re.compile(r"(?:%s)" % city_pattern())
    return _CITY_RE


def _fallback_from_page_text(text, experience, education):
    """从详情页正文兜底提取经验/学历（城市名取自 cities.CITY_MAP，不再硬编码）。"""
    exp = experience or ""
    edu = education or ""
    if exp and edu:
        return exp, edu
    city_re = _city_re()
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or len(line) > 60 or not city_re.search(line):
            continue
        tokens = [t for t in re.split(r"[\s·|]+", line) if t]
        for token in tokens[1:]:
            if not edu:
                degree = _degree_from_token(token)
                if degree:
                    edu = degree
                    continue
            if not exp and _EXP_RE.search(token):
                exp = token
        if exp or edu:
            break
    return exp, edu


def _degree_from_token(token):
    """从单个 token 认出学历；认不出返回空串。"""
    token = (token or "").strip()
    if not token:
        return ""
    if token in ("学历不限", "不限"):
        return "学历不限"
    for word in _DEGREE_HINTS:
        if word in token:
            return normalize_education(token)
    return ""


def classify_info_lines(lines):
    """从详情页头部信息行里拆分经验/学历。

    真实页面可能是「一个 span 一行」，也可能整行「成都 1-3年 本科」，
    所以先按空白切 token 再逐个判断，避免整行塞进经验字段。
    """
    exp, edu = "", ""
    for line in lines or []:
        for token in re.split(r"[\s·|]+", str(line)):
            token = token.strip()
            if not token or len(token) > 12:
                continue
            if not edu:
                degree = _degree_from_token(token)
                if degree:
                    edu = degree
                    continue
            if not exp and _EXP_RE.search(token):
                exp = token
    return exp, edu


# ============================================================
# Chrome / CDP
# ============================================================

def setup_chrome(args, log):
    """启动专用 Chrome 并等待登录。"""
    exe = find_chrome()
    if not exe:
        log.error("❌ 未找到 Chrome。请安装 Chrome，或设置环境变量 CHROME_PATH 指向 chrome.exe")
        return 1
    log.info("🚀 启动 Chrome: %s" % exe)
    log.info("   profile: %s" % CHROME_PROFILE_DIR)
    if not launch_chrome(CHROME_PROFILE_DIR, args.cdp_port, SEARCH_URL, exe):
        log.error("❌ 调试端口 %d 未就绪。" % args.cdp_port)
        log.error("   常见原因：已有 Chrome 正在使用同一个 profile 且未开启调试端口。")
        log.error("   请关闭所有 Chrome 窗口后重试（日常浏览器不受影响）。")
        return 1

    log.info("✅ Chrome 已启动，请在窗口中登录 Boss直聘（最长等待 %d 秒）" % args.login_wait)
    ws_url = ensure_zhipin_target(args.cdp_port)
    if not ws_url:
        log.error("❌ 未找到可用的 Boss直聘标签页")
        return 1

    logged = False
    deadline = time.time() + max(5, args.login_wait)
    try:
        with CDPClient(ws_url, default_timeout=20) as client:
            while time.time() < deadline:
                try:
                    state = client.evaluate(
                        "(() => ({url: location.href,"
                        " needLogin: location.pathname.indexOf('/web/user') === 0"
                        " || !!document.querySelector('.login-form')}))()",
                        await_promise=False,
                    ) or {}
                except CDPError:
                    state = {}
                if state.get("url") and not state.get("needLogin"):
                    logged = True
                    break
                time.sleep(2)
    except CDPError as exc:
        log.warning("⚠️ 无法确认登录状态: %s" % exc)

    if logged:
        log.info("✅ 已登录，登录态已保存在 profile 中，下次无需重复登录\n")
    else:
        log.warning("⚠️ 未确认登录完成。Chrome 会保持打开，登录后可直接运行爬虫。")
    return 0


def check_env(log):
    log.info("🔍 环境检查...")
    exe = find_chrome()
    if exe:
        log.info("✅ Chrome: %s" % exe)
    else:
        log.error("❌ 未找到 Chrome（可设置环境变量 CHROME_PATH）")
    ok = bool(exe)
    for module in ("websocket", "requests"):
        try:
            __import__(module)
            log.info("✅ %s 已安装" % module)
        except ImportError:
            log.error("❌ 缺少依赖 %s，请 pip install -r requirements.txt" % module)
            ok = False
    log.info("ℹ️ CDP 端口 %d: %s" % (CDP_PORT, "已就绪" if cdp_alive(CDP_PORT) else "未就绪（先运行 --setup）"))
    log.info("📂 数据目录: %s" % DATA_DIR)
    return 0 if ok else 1


# ============================================================
# 列表抓取
# ============================================================

def map_job(raw):
    """把列表接口的原始对象映射成统一字段。"""
    salary_text = raw.get("salaryDesc") or raw.get("salary") or ""
    salary_range = parse_salary(salary_text)
    city = raw.get("cityName") or ""
    location = " ".join(
        [part for part in (city, raw.get("areaDistrict"), raw.get("businessDistrict")) if part]
    ).strip()
    job_id = raw.get("encryptJobId") or raw.get("jobId") or ""
    return {
        "job_id": job_id,
        "job_name": raw.get("jobName") or "",
        "company_name": raw.get("brandName") or raw.get("companyName") or "",
        "salary": salary_text,
        "salary_min": salary_range[0] if salary_range else "",
        "salary_max": salary_range[1] if salary_range else "",
        "location": location,
        "experience": raw.get("jobExperience") or raw.get("experience") or raw.get("workYear") or "",
        "education": raw.get("jobDegree") or raw.get("education") or raw.get("educationLevel") or "",
        "company_type": raw.get("companyType") or raw.get("brandIndustry") or "",
        "company_size": raw.get("companySize") or raw.get("brandScaleName") or "",
        "skills": ", ".join(raw.get("skills") or []),
        "job_labels": ", ".join(raw.get("jobLabels") or []),
        "welfare": ", ".join(raw.get("welfareList") or []),
        "days_per_week": raw.get("daysPerWeekDesc") or "",
        "least_month": raw.get("leastMonthDesc") or "",
        "security_id": raw.get("securityId") or "",
        "lid": raw.get("lid") or "",
        "hr_name": raw.get("bossName") or "",
        "hr_title": raw.get("bossTitle") or "",
        "job_description": "",
        "job_address": "",
        "job_url": (DETAIL_URL % job_id) if job_id else "",
    }


def fetch_job_page(client, keyword, city_code, page, page_size=30):
    """抓取列表的一页（一次 CDP 往返），返回 {code, message, jobs}。"""
    page_js = """(async () => {
        const url = 'https://www.zhipin.com/wapi/zpgeek/search/joblist.json'
                  + '?query=' + encodeURIComponent(%s)
                  + '&city=%s&page=%d&pageSize=%d';
        try {
            const resp = await fetch(url, {
                credentials: 'include',
                headers: {'Accept': 'application/json, text/plain, */*',
                          'X-Requested-With': 'XMLHttpRequest'}
            });
            if (!resp.ok) {
                return {code: -resp.status, message: 'HTTP ' + resp.status, jobs: []};
            }
            const data = await resp.json();
            return {
                code: data.code,
                message: data.message || '',
                jobs: (data.zpData && data.zpData.jobList) || []
            };
        } catch (e) {
            return {code: -1, message: String(e), jobs: []};
        }
    })()""" % (_json_literal(keyword), city_code, int(page), int(page_size))
    return client.evaluate(page_js, await_promise=True, timeout=45) or {}


def search_jobs(client, keyword, city_code, pages, slow, log, debug=False, emit=None):
    """逐页调用列表接口，返回职位 dict 列表。

    逐页（而不是一段 JS 里循环）是为了能上报进度、逐页失败可解释、页间可中断。
    """
    emit = emit or (lambda *args, **kwargs: None)
    log.info("🔍 搜索: %s（城市代码 %s，%d 页）" % (keyword, city_code, pages))
    emit("phase", name="search", total=pages)

    search_url = "%s?query=%s&city=%s" % (SEARCH_URL, quote(keyword, safe=""), city_code)
    try:
        client.navigate(search_url, timeout=25)
    except CDPError as exc:
        log.error("  ❌ 打开搜索页失败: %s" % exc)
        emit("error", scope="search", message="打开搜索页失败: %s" % exc)
        return []

    delay = random.uniform(5, 8) if slow else 2.0
    jobs = []
    seen = set()
    duplicates = 0
    fetched_pages = 0
    stop = "complete"

    for page in range(1, pages + 1):
        try:
            result = fetch_job_page(client, keyword, city_code, page)
        except CDPError as exc:
            log.error("  ❌ 第 %d 页请求失败: %s" % (page, exc))
            emit("error", scope="search", message="第 %d 页请求失败: %s" % (page, exc))
            stop = "cdp_error"
            break

        code = result.get("code")
        if code != 0:
            message = result.get("message") or ""
            if code == 37 or "环境异常" in message:
                log.error("  ❌ 触发反爬（code %s）：%s" % (code, message))
                log.error("     建议：减少页数、加 --slow，或等 10-30 分钟后再跑。")
            elif code == -1:
                log.error("  ❌ 请求异常: %s" % message)
            else:
                log.error("  ❌ 接口返回 code=%s: %s" % (code, message))
            emit("error", scope="search", code=code, message=message or "接口返回错误")
            stop = "api_code"
            break

        raw_page = result.get("jobs") or []
        if not raw_page:
            log.info("  第 %d 页没有数据，结束翻页" % page)
            stop = "empty"
            break

        fetched_pages = page
        added = []
        for raw in raw_page:
            item = map_job(raw)
            job_id = item.get("job_id")
            if job_id and job_id in seen:
                duplicates += 1
                continue
            if job_id:
                seen.add(job_id)
            jobs.append(item)
            added.append(item)

        emit("page", page=page, pages=pages, count=len(raw_page), total=len(jobs))
        log.info("  第 %d/%d 页：%d 条（累计 %d）" % (page, pages, len(raw_page), len(jobs)))
        for item in added:
            log.info("  ✅ %s | %s | %s | 经验:%s | 学历:%s" % (
                item["job_name"], item["salary"], item["company_name"],
                item["experience"], item["education"],
            ))

        if len(raw_page) < 30:
            stop = "last_page"
            break
        if page < pages:
            time.sleep(delay)

    if stop == "complete":
        log.info("  已完成全部 %d 页" % fetched_pages)
    elif stop == "last_page":
        log.info("  已到最后一页")

    if debug and jobs:
        log.debug("  字段示例: %s" % json.dumps(jobs[0], ensure_ascii=False)[:300])

    log.info("")
    log.info("📊 共获取 %d 个职位（%d 页%s）" % (
        len(jobs), fetched_pages,
        "，已去重 %d 条" % duplicates if duplicates else "",
    ))
    emit("list_done", count=len(jobs), pages=fetched_pages, stop=stop, duplicates=duplicates)
    return jobs

# ============================================================
# 详情抓取
# ============================================================

def _page_hint(client):
    """详情页拿不到内容时，判断原因给用户看。"""
    try:
        info = client.evaluate(
            "(() => ({url: location.href, "
            "text: (document.body ? document.body.innerText : '').slice(0, 300)}))()",
            await_promise=False,
            timeout=8,
        ) or {}
    except CDPError:
        return "页面读取失败"
    url = info.get("url") or ""
    text = info.get("text") or ""
    if "/web/user" in url or ("登录" in text and "注册" in text):
        return "跳到了登录页，Cookie 可能已过期，请重新 --setup"
    if "环境异常" in text or "验证" in text:
        return "触发风控验证"
    if any(word in text for word in ("职位不存在", "已下线", "停止招聘", "职位已关闭")):
        return "职位已下线"
    return "页面内容异常"


def fetch_detail_page(client, job_id, log):
    """打开详情页并从 DOM 提取。只有描述真的有内容才算成功。"""
    url = DETAIL_URL % quote(job_id, safe="")
    try:
        client.navigate(url, timeout=25)
    except CDPError as exc:
        return {"ok": False, "error": "导航失败: %s" % exc}

    if not client.wait_for_selector(".job-sec-text", timeout_ms=12000):
        return {"ok": False, "error": "页面未渲染出职位描述（%s）" % _page_hint(client)}

    extract_js = """(() => {
        const texts = Array.from(document.querySelectorAll('.job-sec-text'))
            .map(el => (el.innerText || '').trim())
            .filter(t => t.length > 20);
        const infoEl = document.querySelector('.job-primary .info-primary');
        const infoLines = infoEl
            ? (infoEl.innerText || '').split('\\n').map(s => s.trim()).filter(Boolean)
            : [];
        const addrEl = document.querySelector('.location-address')
            || document.querySelector('.job-location')
            || document.querySelector('.job-address');
        return {
            job_description: texts[0] || '',
            job_address: addrEl ? (addrEl.innerText || '').trim() : '',
            info_lines: infoLines,
            body_head: (document.body ? document.body.innerText : '').slice(0, 1500)
        };
    })()"""
    try:
        data = client.evaluate(extract_js, await_promise=False, timeout=15) or {}
    except CDPError as exc:
        return {"ok": False, "error": "提取失败: %s" % exc}

    description = (data.get("job_description") or "").strip()
    if len(description) < 20:
        return {"ok": False, "error": "职位描述为空（可能被风控或页面结构变化）"}

    experience, education = classify_info_lines(data.get("info_lines"))
    if not experience or not education:
        fallback_exp, fallback_edu = _fallback_from_page_text(
            data.get("body_head"), experience, education
        )
        experience = experience or fallback_exp
        education = education or fallback_edu

    return {
        "ok": True,
        "source": "page",
        "job_description": description,
        "job_address": first_line(data.get("job_address")),
        "experience": experience,
        "education": education,
    }


def fetch_detail_api(client, job):
    """详情 API 兜底（只有页面提取失败时才会走到这里）。"""
    job_id = job.get("job_id") or ""
    if not job.get("security_id") or not job.get("lid"):
        return {"ok": False, "error": "缺少 security_id / lid，无法调用详情接口"}

    detail_js = """(async () => {
        const url = 'https://www.zhipin.com/wapi/zpgeek/job/detail.json'
                  + '?jobId=' + encodeURIComponent(%s)
                  + '&securityId=' + encodeURIComponent(%s)
                  + '&lid=' + encodeURIComponent(%s);
        try {
            const resp = await fetch(url, {
                credentials: 'include',
                headers: {'Accept': 'application/json, text/plain, */*',
                          'X-Requested-With': 'XMLHttpRequest'}
            });
            const data = await resp.json();
            return {code: data.code, message: data.message || '', data: data.zpData || null};
        } catch (e) {
            return {code: -1, message: String(e), data: null};
        }
    })()""" % (
        _json_literal(job_id),
        _json_literal(job.get("security_id")),
        _json_literal(job.get("lid")),
    )

    try:
        result = client.evaluate(detail_js, await_promise=True, timeout=25) or {}
    except CDPError as exc:
        return {"ok": False, "error": "详情接口调用失败: %s" % exc}

    if result.get("code") != 0:
        return {
            "ok": False,
            "code": result.get("code"),
            "error": result.get("message") or "详情接口返回错误",
        }

    data = result.get("data") or {}
    job_info = data.get("jobInfo") or {}
    boss_info = data.get("bossInfo") or {}
    brand_info = data.get("brandComInfo") or {}
    description = (job_info.get("postDescription") or "").strip()
    if len(description) < 20:
        return {"ok": False, "error": "详情接口返回的描述为空"}

    return {
        "ok": True,
        "source": "api",
        "job_description": description,
        "job_address": first_line(job_info.get("address")),
        "experience": job_info.get("experienceName") or "",
        "education": job_info.get("degreeName") or "",
        "hr_name": boss_info.get("name") or "",
        "hr_title": boss_info.get("title") or "",
        "company_size": brand_info.get("scaleName") or "",
        "company_type": brand_info.get("industryName") or "",
        "skills": ", ".join(job_info.get("showSkills") or []),
    }


def apply_detail(job, detail):
    """把详情字段合并到职位上（只覆盖有值的字段）。"""
    for key in (
        "job_description", "job_address", "experience", "education",
        "hr_name", "hr_title", "company_size", "company_type", "skills",
    ):
        value = detail.get(key)
        if value:
            job[key] = value


# ============================================================
# 断点（JSONL，追加写）
# ============================================================

def load_progress(log=None):
    """读取已抓详情。兼容旧版 _detail_progress.json。"""
    progress = {}

    if os.path.exists(PROGRESS_LEGACY):
        try:
            with open(PROGRESS_LEGACY, "r", encoding="utf-8") as handle:
                legacy = json.load(handle)
            if isinstance(legacy, dict):
                for job_id, record in legacy.items():
                    if not isinstance(record, dict):
                        continue
                    record = dict(record)
                    record.setdefault("ok", len(record.get("job_description") or "") >= 20)
                    record.setdefault("attempts", 1)
                    record["job_id"] = job_id
                    progress[job_id] = record
        except Exception as exc:
            if log:
                log.warning("⚠️ 旧断点文件读取失败（忽略）: %s" % exc)

    if os.path.exists(PROGRESS_JSONL):
        broken = 0
        with open(PROGRESS_JSONL, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    broken += 1
                    continue  # 上次崩溃留下的半行
                job_id = record.get("job_id")
                if job_id:
                    progress[job_id] = record
        if broken and log:
            log.warning("⚠️ 断点文件有 %d 行损坏（上次中断的半行），已跳过" % broken)

    return progress


def append_progress(record):
    """追加一条断点记录（O(1)，不用重写整个文件）。"""
    ensure_dir(RESULTS_DIR)
    with open(PROGRESS_JSONL, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def detail_done(record):
    return bool(record) and record.get("ok") is True and len(record.get("job_description") or "") >= 20


def merge_cached_details(jobs, progress):
    """把断点里的详情合并到本次结果（只补列表接口本来没有的字段）。"""
    merged = 0
    detail_keys = ("job_description", "job_address", "experience", "education")
    for job in jobs:
        record = progress.get(job.get("job_id") or "")
        if not detail_done(record):
            continue
        for key in detail_keys:
            if record.get(key):
                job[key] = record[key]
        merged += 1
    return merged


def collect_details(client, jobs, limit, slow, progress, log, emit=None):
    """抓取详情。返回统计 dict；中断时已抓内容已写进断点。"""
    emit = emit or (lambda *args, **kwargs: None)
    pending = []
    for job in jobs:
        job_id = job.get("job_id")
        if not job_id:
            continue
        record = progress.get(job_id) or {}
        if detail_done(record):
            continue
        if coerce_int(record.get("attempts"), 0) >= MAX_DETAIL_ATTEMPTS:
            continue
        pending.append(job)

    target_count = min(max(0, limit), len(pending))
    skipped = len(jobs) - len(pending)
    log.info("")
    log.info("📝 详情抓取：本次最多 %d 个（待抓 %d，已完成/超限跳过 %d）" % (
        target_count, len(pending), skipped,
    ))
    emit("phase", name="detail", total=target_count, pending=len(pending), skipped=skipped)

    stats = {"success": 0, "failed": 0, "skipped": 0, "aborted": None}
    consecutive_failures = 0

    for index, job in enumerate(pending[:target_count], 1):
        job_id = job["job_id"]
        record = progress.get(job_id) or {}
        attempt = coerce_int(record.get("attempts"), 0) + 1
        log.info("")
        log.info("  [%d/%d] %s" % (index, target_count, job.get("job_name") or job_id))

        # 请求之间只等一次（旧版每条等两次，总耗时几乎翻倍）
        if index > 1:
            wait = random.uniform(6, 10) if slow else random.uniform(1.5, 2.5)
            if consecutive_failures:
                wait *= 1.5 + min(consecutive_failures, 3) * 0.5
            log.debug("    等待 %.1fs..." % wait)
            time.sleep(wait)

        detail = fetch_detail_page(client, job_id, log)

        if not detail.get("ok"):
            log.debug("    页面方式失败: %s" % detail.get("error"))
            detail = fetch_detail_api(client, job)
            if detail.get("code") == 37:
                log.error("    ⚠️ 详情接口触发反爬（code 37），停止抓取。已抓内容已保存，稍后重跑会自动续抓。")
                stats["aborted"] = "anti-crawl"
                break

        if detail.get("ok"):
            apply_detail(job, detail)
            record = {
                "job_id": job_id,
                "ok": True,
                "source": detail.get("source", ""),
                "attempts": attempt,
                "updated_at": _now(),
                "job_description": job.get("job_description", ""),
                "job_address": job.get("job_address", ""),
                "experience": job.get("experience", ""),
                "education": job.get("education", ""),
                "hr_name": job.get("hr_name", ""),
                "hr_title": job.get("hr_title", ""),
                "company_size": job.get("company_size", ""),
                "company_type": job.get("company_type", ""),
            }
            append_progress(record)
            progress[job_id] = record
            stats["success"] += 1
            consecutive_failures = 0
            emit("detail", index=index, total=target_count, ok=True,
                 title=job.get("job_name") or job_id,
                 source=detail.get("source", ""),
                 description_len=len(job.get("job_description") or ""))
            log.info("    ✅ 成功(%s) | 经验:%s | 学历:%s | 描述 %d 字" % (
                detail.get("source", "?"), job.get("experience"), job.get("education"),
                len(job.get("job_description") or ""),
            ))
        else:
            error = detail.get("error") or "未知错误"
            append_progress({
                "job_id": job_id,
                "ok": False,
                "attempts": attempt,
                "updated_at": _now(),
                "error": str(error)[:300],
            })
            progress[job_id] = {"ok": False, "attempts": attempt, "error": str(error)}
            stats["failed"] += 1
            consecutive_failures += 1
            log.warning("    ❌ 失败（第 %d/%d 次）: %s" % (attempt, MAX_DETAIL_ATTEMPTS, error))
            emit("detail", index=index, total=target_count, ok=False,
                 title=job.get("job_name") or job_id, error=str(error)[:160])

            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                log.error("    ⚠️ 连续失败 %d 次，停止抓取（大概率被风控，稍后再跑会自动续抓）"
                          % consecutive_failures)
                stats["aborted"] = "consecutive-failures"
                break

    log.info("")
    log.info("📊 详情结果：本次成功 %d，失败 %d，累计已完成 %d" % (
        stats["success"], stats["failed"],
        sum(1 for job in jobs if detail_done(progress.get(job.get("job_id") or ""))),
    ))
    if stats["aborted"]:
        log.warning("   本次提前停止（%s），已抓数据已保存。" % stats["aborted"])
    emit("detail_done", success=stats["success"], failed=stats["failed"],
         aborted=stats["aborted"], total=target_count)
    return stats


# ============================================================
# 保存
# ============================================================

def save_results(jobs, keyword, city, fmt="json", out_dir=None):
    """保存结果（文件名做非法字符清洗，写入用「临时文件 + 替换」）。"""
    out_dir = ensure_dir(out_dir or RESULTS_DIR)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    base = "boss_%s_%s_%s" % (
        sanitize_filename(city, 20, "unknown"),
        sanitize_filename(keyword, 40, "unknown"),
        stamp,
    )

    if fmt == "csv":
        path = os.path.join(out_dir, base + ".csv")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS, extrasaction="ignore")
            writer.writeheader()
            for job in jobs:
                writer.writerow({key: job.get(key, "") for key in FIELDS})
        os.replace(tmp, path)
    else:
        path = os.path.join(out_dir, base + ".json")
        write_json_atomic(path, [{key: job.get(key, "") for key in FIELDS} for job in jobs])

    get_logger().info("📁 结果已保存: %s" % path)
    return path


# ============================================================
# 入口
# ============================================================

def build_parser():
    parser = argparse.ArgumentParser(description="BOSS直聘爬虫 - 基于 Chrome CDP")
    parser.add_argument("--check", action="store_true", help="检查环境")
    parser.add_argument("--setup", action="store_true", help="启动 Chrome CDP 并等待登录")
    parser.add_argument("-k", "--keyword", type=str, help="搜索关键词")
    parser.add_argument("-c", "--city", type=str, default="全国", help="城市名称/城市代码/序号")
    parser.add_argument("-p", "--pages", type=int, default=3, help="爬取页数（每页 30 条）")
    parser.add_argument("-f", "--format", type=str, default="json", choices=["json", "csv"],
                        help="输出格式")
    parser.add_argument("-d", "--detail", type=int, default=0, help="本次抓取详情的条数（0=不抓）")
    parser.add_argument("--slow", action="store_true", help="慢速模式：拉长请求间隔")
    parser.add_argument("--cdp-port", type=int, default=CDP_PORT, help="Chrome 调试端口")
    parser.add_argument("--login-wait", type=int, default=90, help="--setup 等待登录的秒数")
    parser.add_argument("--out", type=str, default=None, help="输出目录（默认 data/boss_results）")
    parser.add_argument("--events", type=str, default=None,
                        help="把进度写成 JSONL 事件文件（供网页面板实时读取）")
    parser.add_argument("--debug", action="store_true", help="输出调试信息")
    return parser


def main(argv=None):
    setup_console()
    args = build_parser().parse_args(argv)
    log = setup_logging(verbose=args.debug)
    emitter = EventEmitter(args.events)

    if args.check:
        return check_env(log)
    if args.setup:
        return setup_chrome(args, log)

    started = time.time()

    def fail(message, **extra):
        log.error(message)
        emitter.emit("error", message=message, **extra)
        emitter.emit("done", status="failed", elapsed=round(time.time() - started, 1))
        record_history(kind="crawl", status="failed", error=message[:200], **extra)
        return 1

    if args.pages < 1:
        return fail("❌ 页数必须 >= 1", pages=args.pages)
    if args.detail < 0:
        return fail("❌ 详情条数不能为负数", detail=args.detail)

    try:
        keyword = (args.keyword or input("请输入搜索关键词: ")).strip()
    except (EOFError, KeyboardInterrupt):
        return fail("❌ 已取消")
    if not keyword:
        return fail("❌ 关键词不能为空")

    city_name, city_code = resolve_city(args.city)
    if not city_name:
        suggestions = suggest_cities(args.city)
        if suggestions:
            log.error("   你是不是想输入: %s" % "、".join(suggestions))
        log.error("   可运行 python crawler\\fetch_cities.py 更新城市列表，或在面板「爬取」页点「更新城市列表」")
        return fail("❌ 未知城市: %s" % args.city)

    log.info("")
    log.info("=" * 60)
    log.info("  关键词: %s" % keyword)
    log.info("  城市:   %s (%s)" % (city_name, city_code))
    log.info("  页数:   %d 页（约 %d 条）" % (args.pages, args.pages * 30))
    log.info("  详情:   %s" % ("%d 条" % args.detail if args.detail else "不抓"))
    if args.slow:
        log.info("  模式:   🐢 慢速")
    log.info("=" * 60)
    log.info("")

    emitter.emit("start", keyword=keyword, city=city_name, city_code=city_code,
                 pages=args.pages, detail=args.detail, slow=bool(args.slow),
                 format=args.format)

    if not cdp_alive(args.cdp_port):
        return fail("❌ 未检测到 Chrome 调试端口 %d，请先启动 Chrome 并登录" % args.cdp_port,
                    keyword=keyword, city=city_name)

    try:
        ws_url = ensure_zhipin_target(args.cdp_port)
    except ChromeError as exc:
        return fail("❌ %s" % exc, keyword=keyword, city=city_name)
    if not ws_url:
        return fail("❌ 未找到可用的 Chrome 标签页", keyword=keyword, city=city_name)

    jobs = []
    stats = {}
    try:
        with CDPClient(ws_url, default_timeout=60) as client:
            jobs = search_jobs(client, keyword, city_code, args.pages, args.slow,
                               log, args.debug, emit=emitter.emit)
            if jobs:
                progress = load_progress(log)
                merged = merge_cached_details(jobs, progress)
                if merged:
                    log.info("♻️ 已从断点补全 %d 条职位详情" % merged)
                if args.detail > 0:
                    stats = collect_details(client, jobs, args.detail, args.slow,
                                            progress, log, emit=emitter.emit)
    except KeyboardInterrupt:
        log.warning("")
        log.warning("⏹️ 已手动中断，正在保存已抓到的数据...")
    except CDPError as exc:
        log.error("❌ CDP 异常: %s" % exc)
        emitter.emit("error", message="CDP 异常: %s" % exc)
        if not jobs:
            emitter.emit("done", status="failed")
            record_history(kind="crawl", status="failed",
                           error=("CDP 异常: %s" % exc)[:160],
                           keyword=keyword, city=city_name, pages=args.pages,
                           detail=args.detail)
            return 1

    if not jobs:
        return fail("❌ 未获取到任何职位", keyword=keyword, city=city_name)

    try:
        path = save_results(jobs, keyword, city_name, args.format, args.out)
    except OSError as exc:
        return fail("❌ 保存失败: %s" % exc, keyword=keyword, city=city_name)

    detail_ok = stats.get("success", 0)
    elapsed = round(time.time() - started, 1)
    emitter.emit("saved", path=path, jobs=len(jobs), detail_success=detail_ok)
    emitter.emit("done", status="ok", jobs=len(jobs), detail_success=detail_ok, elapsed=elapsed)
    record_history(kind="crawl", status="ok", keyword=keyword, city=city_name, pages=args.pages,
                   detail=args.detail, jobs=len(jobs), details_ok=detail_ok,
                   aborted=stats.get("aborted") or "", saved=path, elapsed=elapsed,
                   format=args.format, slow=bool(args.slow))

    if args.detail > 0:
        log.info("💡 想补全更多详情，直接重跑本命令即可（已抓的会自动跳过）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
