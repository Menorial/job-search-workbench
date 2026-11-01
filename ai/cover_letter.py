# -*- coding: utf-8 -*-
"""打招呼话术生成：为高匹配职位生成 3 个版本，汇总成 top10.md。

话术也做了缓存（同一职位 + 同一简历不重复花钱），重跑 top10 基本免费。
"""

import argparse
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from common.console import setup_console
from common.paths import ensure_dir

from config import LETTER_CACHE_FILE, MATCH_THRESHOLD, OUTPUT_DIR, check_config
from data_store import job_key
from llm import LLMError, chat_json, log_usage

SYSTEM_PROMPT = """你是求职投递专家。根据候选人的简历亮点和目标职位，生成 3 个版本的 Boss直聘打招呼话术。

输出只输出 JSON：
{
  "short": "简短版（30-40字）：一句话突出最匹配的点 + 表达兴趣",
  "skill": "强调技能版（50-70字）：突出 2-3 个核心技能与岗位要求吻合",
  "project": "提项目版（50-70字）：用一个具体项目经历证明能力"
}

要求：
- 语气真诚、不卑不亢、口语化（像真人发消息，不要像模板）
- 中文，不要用 emoji 堆砌（最多1个）
- 不要写"您好"以外多余寒暄，直接进入重点
- 每条以能自然衔接"请问这个岗位还在招吗/方便沟通吗"结尾"""


def _load_letter_cache():
    if not os.path.exists(LETTER_CACHE_FILE):
        return {}
    try:
        with open(LETTER_CACHE_FILE, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data.get("entries", {}) if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_letter_cache(entries):
    ensure_dir(os.path.dirname(LETTER_CACHE_FILE))
    payload = {"version": 1, "entries": entries}
    tmp = LETTER_CACHE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)
    os.replace(tmp, LETTER_CACHE_FILE)


def generate_letters(resume, job, matched, cache=None, resume_hash=""):
    """为单个职位生成 3 版话术（命中缓存直接返回）。"""
    if cache is None:
        cache = {}
    key = "%s|%s" % (job_key(job), resume_hash)
    if key in cache:
        return cache[key]

    job_info = {
        "职位": job.get("job_name", ""),
        "公司": job.get("company_name", ""),
        "要求": {
            "技能": job.get("skills", ""),
            "经验": job.get("experience", ""),
            "学历": job.get("education", ""),
            "描述前300字": (job.get("job_description") or "")[:300],
        },
        "匹配理由": matched.get("reasons", []),
        "AI建议要点": matched.get("cover_tips", ""),
    }
    resume_info = {
        "技能": resume.get("skills", []),
        "亮点": resume.get("highlights", []),
        "项目": (resume.get("projects") or [])[:2],
    }
    try:
        letters = chat_json([
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(
                {"候选人": resume_info, "职位": job_info}, ensure_ascii=False)},
        ], temperature=0.6)
    except LLMError as exc:
        print("    ⚠️ 话术生成失败: %s" % str(exc)[:80])
        return None

    result = {
        "short": str(letters.get("short") or "").strip(),
        "skill": str(letters.get("skill") or "").strip(),
        "project": str(letters.get("project") or "").strip(),
    }
    cache[key] = result
    return result


def generate_top10(resume, matched_results, top_n=10, on_progress=None):
    """生成 top10.md（on_progress 用于网页面板上报进度）。"""
    emit = on_progress or (lambda *args, **kwargs: None)
    if not check_config():
        return None
    if not matched_results:
        print("❌ 无匹配结果，请先运行: python run.py match")
        return None

    from matcher import resume_fingerprint

    ensure_dir(OUTPUT_DIR)
    resume_hash = resume_fingerprint(resume)
    cache = _load_letter_cache()

    above = [item for item in matched_results if item.get("score", 0) >= MATCH_THRESHOLD]
    if above:
        pool = above
    else:
        pool = matched_results
        print("⚠️ 没有职位达到推荐线（>=%d），下面列出分数最高的 %d 个（仅供参考）"
              % (MATCH_THRESHOLD, top_n))

    lines = ["# 推荐投递 TOP%d" % min(top_n, len(pool)), ""]
    lines.append("> 推荐线：%d 分 · 共 %d 个职位达标 · 数据来自 data/boss_results"
                 % (MATCH_THRESHOLD, len(above)))
    lines.append("")

    count = 0
    total = min(top_n, len(pool))
    emit("phase", name="top10", total=total)
    for item in pool[:top_n]:
        count += 1
        job = item.get("job") or {}
        emit("progress", current=count, total=total, phase="top10",
             label=job.get("job_name", ""))
        lines.append("## %d. %s @ %s" % (count, job.get("job_name", ""), job.get("company_name", "")))
        lines.append("")
        lines.append("**匹配分: %s/100**" % item.get("score"))
        lines.append("")
        lines.append("- 薪资: %s | 地点: %s" % (job.get("salary", ""), job.get("location", "")))
        lines.append("- 经验: %s | 学历: %s" % (job.get("experience", ""), job.get("education", "")))
        if job.get("company_size"):
            lines.append("- 公司规模: %s" % job["company_size"])
        if job.get("skills"):
            lines.append("- 技能标签: %s" % job["skills"])
        if job.get("job_url"):
            lines.append("- 职位链接: %s" % job["job_url"])
        lines.append("")

        reasons = item.get("reasons") or []
        if reasons:
            lines.append("**匹配理由:**")
            for reason in reasons:
                lines.append("- %s" % reason)
            lines.append("")
        risks = item.get("risks") or []
        if risks:
            lines.append("**风险点:**")
            for risk in risks:
                lines.append("- %s" % risk)
            lines.append("")

        print("  ✍️ 生成 %s 话术..." % job.get("job_name", ""))
        letters = generate_letters(resume, job, item, cache, resume_hash)
        if letters:
            lines.append("**打招呼话术:**")
            lines.append("- 简短版: %s" % letters.get("short", ""))
            lines.append("- 技能版: %s" % letters.get("skill", ""))
            lines.append("- 项目版: %s" % letters.get("project", ""))
        else:
            lines.append("**打招呼话术:** %s" % (item.get("cover_tips") or ""))
        lines.append("")

    _save_letter_cache(cache)

    out_file = os.path.join(OUTPUT_DIR, "top10.md")
    tmp = out_file + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines))
    os.replace(tmp, out_file)
    print("💾 已保存: %s" % out_file)
    emit("saved", path=out_file, count=count)
    log_usage("top10")
    return out_file


def main(argv=None):
    setup_console()
    parser = argparse.ArgumentParser(description="生成推荐投递 + 话术")
    parser.add_argument("-r", "--resume", help="简历结构化 JSON 路径", default=None)
    parser.add_argument("-m", "--matched", help="匹配结果 JSON 路径", default=None)
    parser.add_argument("-n", "--top", type=int, default=10)
    args = parser.parse_args(argv)

    resume_path = args.resume or os.path.join(OUTPUT_DIR, "resume_structured.json")
    matched_path = args.matched or os.path.join(OUTPUT_DIR, "matched_jobs.json")
    if not os.path.exists(resume_path) or not os.path.exists(matched_path):
        print("❌ 缺少简历或匹配结果文件，请先运行 parse 和 match")
        return 1
    with open(resume_path, "r", encoding="utf-8") as handle:
        resume = json.load(handle)
    with open(matched_path, "r", encoding="utf-8") as handle:
        matched = json.load(handle)
    return 0 if generate_top10(resume, matched, top_n=args.top) else 1


if __name__ == "__main__":
    sys.exit(main())
