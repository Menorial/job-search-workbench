# -*- coding: utf-8 -*-
"""AI 精评匹配（核心）。

输出每个职位的：总分 / 6 维度分 / 匹配理由 / 风险点 / 话术要点。

缓存设计（相对旧版修正的关键点）：
- 缓存键 = job_id + 简历指纹 + 缓存版本。旧版只按 job_id，
  简历一改分数还是旧的，而且完全无感知。
- 评分结果做类型归一（模型偶尔返回 "85分"，旧版会让排序直接 TypeError）。
- 候选并发评（默认 3 并发），50 个职位从 ~3 分钟降到 ~1 分钟。
"""

import argparse
import json
import os
import random
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from common.console import setup_console
from common.text import coerce_int

from config import (
    CACHE_FILE,
    CACHE_VERSION,
    CONCURRENCY,
    DEEPSEEK_MODEL,
    DIM_KEYS,
    MATCH_THRESHOLD,
    MAX_CANDIDATES,
    OUTPUT_DIR,
    check_config,
    weights_text,
)
from data_store import job_key, load_all_jobs
from filter import coarse_filter
from llm import LLMError, chat_json, log_usage, usage_snapshot

SYSTEM_PROMPT_TEMPLATE = """你是资深招聘顾问，负责评估「候选人简历」与「招聘职位」的匹配度。

输入：候选人结构化简历 + 一个职位信息。
输出：只输出 JSON，不要输出其他文字，结构如下：
{
  "score": 0到100的整数（总分）,
  "dims": {"skill": 技能匹配分, "exp": 经验匹配分, "edu": 学历匹配分, "city": 城市匹配分, "salary": 薪资匹配分, "company": 公司匹配分},
  "reasons": ["2-4条匹配理由，具体到技能/项目/经验"],
  "risks": ["1-3条风险点，如学历不满足、薪资差距大、经验不符"],
  "cover_tips": "一句话打招呼建议（30字内，突出最匹配的点）"
}

评分标准：
- 总分 = 各维度加权：%(weights)s
- 技能：职位要求技能与简历技能重合度越高分越高
- 经验：职位要求年限与简历年限匹配度
- 学历：职位要求学历 <= 简历学历则满分
- 城市：职位城市在期望城市内则高分
- 薪资：职位薪资与期望薪资重叠度高则高分
- 公司：公司规模/行业符合偏好则高分
- 总分低于 60 通常意味着明显不匹配，请如实打分"""


def build_system_prompt():
    """权重来自 config，改权重不会出现「prompt 和文档两套标准」。"""
    return SYSTEM_PROMPT_TEMPLATE % {"weights": weights_text()}


def _job_prompt(resume, job):
    """构造单个职位的评分输入（压缩以控制 token）。"""
    resume = resume or {}
    education = resume.get("education") if isinstance(resume.get("education"), dict) else {}
    projects = resume.get("projects") or []
    resume_brief = {
        "年限": resume.get("years_of_experience", 0),
        "学历": education.get("degree", ""),
        "学校专业": "%s %s" % (education.get("school", ""), education.get("major", "")),
        "城市": resume.get("cities", []),
        "期望薪资": resume.get("salary_expectation", {}),
        "技能": resume.get("skills", []),
        "岗位方向": resume.get("job_intent", []),
        "亮点": resume.get("highlights", []),
        "项目": projects[:3],
        "公司偏好": resume.get("company_pref", {}),
    }
    job_brief = {
        "职位": job.get("job_name", ""),
        "公司": job.get("company_name", ""),
        "薪资": job.get("salary", ""),
        "地点": job.get("location", ""),
        "经验要求": job.get("experience", ""),
        "学历要求": job.get("education", ""),
        "公司类型": job.get("company_type", ""),
        "公司规模": job.get("company_size", ""),
        "技能标签": job.get("skills", ""),
        "福利": job.get("welfare", ""),
        "职位描述(前500字)": (job.get("job_description") or "")[:500],
    }
    return json.dumps({"候选人简历": resume_brief, "职位": job_brief}, ensure_ascii=False)


def resume_fingerprint(resume):
    """简历内容指纹：简历变了，缓存自动失效。"""
    import hashlib

    raw = json.dumps(resume or {}, ensure_ascii=False, sort_keys=True)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:12]


def cache_key(job_id, resume_hash):
    return "%s|%s|v%d" % (job_id, resume_hash, CACHE_VERSION)


def normalize_result(raw):
    """把模型输出归一成稳定的结构（分数一定是 0-100 的 int）。"""
    raw = raw if isinstance(raw, dict) else {}
    dims_raw = raw.get("dims") if isinstance(raw.get("dims"), dict) else {}
    dims = {key: coerce_int(dims_raw.get(key), 0, 0, 100) for key in DIM_KEYS}

    def _str_list(value, limit):
        if not isinstance(value, (list, tuple)):
            return []
        items = [str(item).strip() for item in value if str(item).strip()]
        return items[:limit]

    return {
        "score": coerce_int(raw.get("score"), 0, 0, 100),
        "dims": dims,
        "reasons": _str_list(raw.get("reasons"), 6),
        "risks": _str_list(raw.get("risks"), 4),
        "cover_tips": str(raw.get("cover_tips") or "").strip()[:120],
    }


def load_cache():
    """返回 (entries, usable)。旧格式/版本不符时返回空表并标记不可用。"""
    if not os.path.exists(CACHE_FILE):
        return {}, True
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except Exception:
        return {}, False
    if not isinstance(raw, dict):
        return {}, False
    if raw.get("version") != CACHE_VERSION or not isinstance(raw.get("entries"), dict):
        return {}, False
    return raw["entries"], True


def save_cache(entries):
    os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
    payload = {
        "version": CACHE_VERSION,
        "model": DEEPSEEK_MODEL,
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "entries": entries,
    }
    tmp = CACHE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)
    os.replace(tmp, CACHE_FILE)


def _score_one(resume, job, system_prompt):
    try:
        raw = chat_json([
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": _job_prompt(resume, job)},
        ], temperature=0.3)
        return normalize_result(raw), None
    except LLMError as exc:
        return None, str(exc)[:120]


def match_all(resume, jobs=None, max_candidates=None, concurrency=None, on_progress=None):
    """完整流程：加载职位 -> 规则粗筛 -> AI 精评 -> 排序输出。

    on_progress: 可选回调，接收 ("phase"|"progress"|"saved", **fields)
    """
    emit = on_progress or (lambda *args, **kwargs: None)
    if not check_config():
        return []

    if jobs is None:
        jobs = load_all_jobs()
    if not jobs:
        print("❌ 没有职位数据，请先运行爬虫")
        return []

    max_candidates = max_candidates or MAX_CANDIDATES
    print("📂 共 %d 个职位，规则粗筛中..." % len(jobs))
    candidates = coarse_filter(jobs, resume, top_n=max_candidates)
    print("粗筛后 %d 个候选" % len(candidates))

    resume_hash = resume_fingerprint(resume)
    cache, cache_usable = load_cache()
    if not cache_usable and cache == {} and os.path.exists(CACHE_FILE):
        print("⚠️ 旧缓存已失效（旧版未绑定简历版本），本次全部重新评分")

    system_prompt = build_system_prompt()
    lock = threading.Lock()

    results = []
    pending = []
    # coarse_filter 返回的是 (job, 规则分, 理由) 元组
    for job, _rule_score, _rule_reasons in candidates:
        key = cache_key(job_key(job), resume_hash)
        cached = cache.get(key)
        if cached:
            results.append({"job": job, "from_cache": True, **cached})
        else:
            pending.append(job)

    print("缓存命中 %d 个，需要调用 AI %d 个" % (len(results), len(pending)))
    emit("phase", name="match", total=len(pending), cached=len(results))

    workers = max(1, min(int(concurrency or CONCURRENCY), 8))
    if workers > 1 and len(pending) > 1:
        print("并发数: %d" % workers)

    completed = [0]
    saved_counter = [0]

    def run_one(job):
        if workers > 1:
            time.sleep(random.uniform(0.05, 0.3))  # 打散请求，避免瞬时峰值
        result, error = _score_one(resume, job, system_prompt)
        with lock:
            completed[0] += 1
            index = completed[0]
        if result:
            with lock:
                cache[cache_key(job_key(job), resume_hash)] = result
            print("  [%d/%d] %s ... %d分%s" % (
                index, len(pending), job.get("job_name", ""), result["score"],
                "（可投递）" if result["score"] >= MATCH_THRESHOLD else "",
            ))
            emit("progress", current=index, total=len(pending), phase="match",
                 label=job.get("job_name", ""), score=result["score"])
        else:
            print("  [%d/%d] %s ... 跳过（%s）" % (
                index, len(pending), job.get("job_name", ""), error,
            ))
            emit("progress", current=index, total=len(pending), phase="match",
                 label=job.get("job_name", ""), failed=True)
        return job, result

    if pending:
        if workers > 1 and len(pending) > 1:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [pool.submit(run_one, job) for job in pending]
                for future in as_completed(futures):
                    job, result = future.result()
                    if result:
                        results.append({"job": job, "from_cache": False, **result})
                    with lock:
                        if completed[0] - saved_counter[0] >= 10:
                            save_cache(dict(cache))
                            saved_counter[0] = completed[0]
        else:
            for job in pending:
                job, result = run_one(job)
                if result:
                    results.append({"job": job, "from_cache": False, **result})
                if completed[0] % 10 == 0:
                    save_cache(dict(cache))

    if not results:
        # 全部失败时不要覆盖上一次的 matched_jobs.json / 缓存，
        # 否则一次 Key 失效就会把历史结果清空。
        print("❌ 没有产生任何匹配结果，保留上一次的 matched_jobs.json（检查 API Key / 日志）")
        return []

    save_cache(dict(cache))

    results.sort(key=lambda item: item.get("score", 0), reverse=True)
    new_scored = sum(1 for item in results if not item.get("from_cache"))
    print("")
    print("📊 AI 精评完成: %d 个结果（新评分 %d，缓存命中 %d）" % (
        len(results), new_scored, len(results) - new_scored))

    usage = usage_snapshot()
    if usage["calls"]:
        print("💰 本次调用 %d 次，token 输入 %d / 输出 %d，估算成本 ¥%.3f" % (
            usage["calls"], usage["prompt_tokens"], usage["completion_tokens"], usage["cost"]))

    out_file = os.path.join(OUTPUT_DIR, "matched_jobs.json")
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    payload = []
    for item in results:
        entry = {"job": item["job"], **{key: item[key] for key in
                                        ("score", "dims", "reasons", "risks", "cover_tips")}}
        entry["matched_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        entry["resume_hash"] = resume_hash
        payload.append(entry)
    tmp = out_file + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    os.replace(tmp, out_file)
    print("💾 匹配结果已保存: %s" % out_file)

    above = sum(1 for item in results if item.get("score", 0) >= MATCH_THRESHOLD)
    print("🎯 达到推荐线（>=%d）: %d 个" % (MATCH_THRESHOLD, above))
    emit("saved", path=out_file, count=len(results), above=above,
         avg=round(sum(item.get("score", 0) for item in results) / len(results), 1) if results else 0)
    log_usage("match", {"jobs": len(results), "resume_hash": resume_hash})
    return results


def main(argv=None):
    setup_console()
    parser = argparse.ArgumentParser(description="AI 职位匹配")
    parser.add_argument("-r", "--resume", help="简历结构化 JSON 路径", default=None)
    parser.add_argument("-n", "--max", type=int, default=None, help="AI 精评候选上限")
    parser.add_argument("-j", "--concurrency", type=int, default=None, help="并发数")
    args = parser.parse_args(argv)

    resume_path = args.resume or os.path.join(OUTPUT_DIR, "resume_structured.json")
    if not os.path.exists(resume_path):
        print("❌ 未找到简历结构化数据，请先运行: python run.py parse -f 简历路径")
        return 1
    with open(resume_path, "r", encoding="utf-8") as handle:
        resume = json.load(handle)

    results = match_all(resume, max_candidates=args.max, concurrency=args.concurrency)
    return 0 if results else 1


if __name__ == "__main__":
    sys.exit(main())
