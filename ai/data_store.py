# -*- coding: utf-8 -*-
"""爬取结果的统一读取：合并 data/boss_results/ 下所有 JSON 并按 job_id 去重。

原来 filter 只读「最新一个文件」、dashboard 读「全部文件」，同一份数据两个答案。
现在两边都走这里，行为一致。
"""

import glob
import hashlib
import json
import os
import threading

from config import RESULTS_DIR

_LOCK = threading.Lock()
_CACHE = {"signature": None, "jobs": []}


def result_files():
    """返回结果文件列表，按修改时间从旧到新（新的覆盖旧的）。"""
    if not os.path.isdir(RESULTS_DIR):
        return []
    files = []
    for path in glob.glob(os.path.join(RESULTS_DIR, "*.json")):
        name = os.path.basename(path)
        if name.startswith("_") or name.endswith(".tmp"):
            continue
        try:
            files.append((path, os.path.getmtime(path), os.path.getsize(path)))
        except OSError:
            continue
    files.sort(key=lambda item: item[1])
    return files


def _signature(files):
    return tuple((path, round(mtime, 3), size) for path, mtime, size in files)


def job_key(job):
    """稳定的职位标识；没有 job_id 时用关键字段拼一个伪 key。"""
    job_id = job.get("job_id") or job.get("jobId") or job.get("encryptJobId")
    if job_id:
        return str(job_id)
    raw = "|".join(str(job.get(field) or "") for field in
                   ("job_name", "company_name", "salary", "location"))
    return "hash:" + hashlib.md5(raw.encode("utf-8")).hexdigest()[:16]


def merge_job(old, new):
    """new 覆盖 old 的非空字段；old 的独有内容保留（补齐不同批次抓到的字段）。"""
    merged = dict(old)
    for key, value in new.items():
        if value not in (None, "", [], {}):
            merged[key] = value
        elif key not in merged:
            merged[key] = value
    return merged


def load_all_jobs(refresh=False):
    """合并所有结果文件（按 job_id 去重），带 mtime 缓存。"""
    files = result_files()
    signature = _signature(files)
    with _LOCK:
        if not refresh and _CACHE["signature"] == signature:
            return _CACHE["jobs"]

    merged = {}
    for path, _mtime, _size in files:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except Exception:
            continue  # 坏文件跳过，不影响整体
        if not isinstance(data, list):
            continue
        for job in data:
            if not isinstance(job, dict) or not job.get("job_name"):
                continue
            key = job_key(job)
            merged[key] = merge_job(merged[key], job) if key in merged else dict(job)

    jobs = list(merged.values())
    with _LOCK:
        _CACHE["signature"] = signature
        _CACHE["jobs"] = jobs
    return jobs


def load_latest_file_jobs():
    """只读最新一个文件（旧行为，保留给「只看本次爬取」的场景）。"""
    files = result_files()
    if not files:
        return []
    path = files[-1][0]
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        return []
    return data if isinstance(data, list) else []


def summary(jobs):
    """数据概况，方便命令行打印。"""
    files = result_files()
    with_detail = sum(1 for job in jobs if len(job.get("job_description") or "") >= 20)
    return {
        "files": len(files),
        "jobs": len(jobs),
        "with_detail": with_detail,
        "latest_file": os.path.basename(files[-1][0]) if files else "",
    }
