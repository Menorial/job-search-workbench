# -*- coding: utf-8 -*-
"""规则粗筛（零成本，先筛掉明显不合适的候选）。

按「城市 / 学历 / 薪资 / 经验 / 技能」给每个职位打一个 0-100 的规则分，
只用于排序候选，最终由 AI 决定（规则不淘汰，避免误杀）。
"""

import argparse
import json
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from common.console import setup_console
from common.salary import parse_salary, salary_from_job
from common.text import coerce_int

from config import MAX_CANDIDATES, OUTPUT_DIR
from data_store import load_all_jobs

# 学历等级（数字越大越高）
DEGREE_LEVEL = {
    "不限": 0, "学历不限": 0, "初中": 1, "高中": 1, "中专": 2, "中技": 2,
    "大专": 3, "本科": 4, "硕士": 5, "博士": 6,
}
DEGREE_ORDER = sorted(DEGREE_LEVEL.items(), key=lambda item: -item[1])


def _parse_salary(text):
    """兼容旧接口：返回 (min_k, max_k) 或 None。"""
    return parse_salary(text)


def _parse_years(text):
    """从 '3-5年' / '在校/应届' / '经验不限' 解析经验年限，返回 (min, max) 或 None。"""
    if not text:
        return None
    text = str(text)
    if "应届" in text or "在校" in text:
        return 0, 1
    if "无经验" in text:
        return 0, 1
    if "不限" in text:
        return 0, 99
    match = re.search(r"(\d+)\s*[-~—–]\s*(\d+)\s*年", text)
    if match:
        return int(match.group(1)), int(match.group(2))
    match = re.search(r"(\d+)\s*年", text)
    if match:
        value = int(match.group(1))
        return value, value
    return None


def _degree_of(job):
    """取职位学历要求等级。"""
    education = str(job.get("education") or job.get("jobDegree") or "")
    for keyword, level in DEGREE_ORDER:
        if keyword in education:
            return level
    return 0


def job_city_name(job):
    """取职位城市名（优先 cityName，其次 location 第一段）。"""
    city = (job.get("cityName") or job.get("city") or "").strip()
    if city:
        return city
    location = (job.get("location") or "").strip()
    return location.split()[0] if location.split() else ""


def _match_city(job, resume_cities):
    """职位城市是否在期望城市里（模糊匹配，支持 '成都' vs '成都·高新区'）。"""
    if not resume_cities:
        return True
    city = job_city_name(job)
    if not city:
        return False
    for expected in resume_cities:
        expected = str(expected or "").strip()
        if expected and (expected in city or city in expected):
            return True
    return False


def _skill_hit(skill, text):
    """技能命中判断：英文按词边界（避免 Java 命中 JavaScript），中文按包含。"""
    skill = skill.lower().strip()
    if not skill:
        return False
    if re.search(r"[a-z]", skill):
        pattern = r"(?<![a-z0-9+#])" + re.escape(skill) + r"(?![a-z0-9+#])"
        return re.search(pattern, text) is not None
    return skill in text


def score_job(job, resume):
    """对单个职位做规则评分（0-100），返回 (score, reasons)。"""
    score = 50.0
    reasons = []
    resume = resume or {}

    # 1. 城市
    cities = resume.get("cities") or []
    if not _match_city(job, cities):
        score -= 35
        reasons.append("城市不匹配")
    elif cities:
        score += 10

    # 2. 学历
    need_level = _degree_of(job)
    my_degree = (resume.get("education") or {}).get("degree", "") \
        if isinstance(resume.get("education"), dict) else ""
    my_level = DEGREE_LEVEL.get(my_degree, 4)
    if need_level > my_level:
        score -= 20
        reasons.append("学历要求(%s)高于简历" % job.get("education"))
    elif need_level > 0:
        score += 5

    # 3. 薪资
    salary = salary_from_job(job)
    expectation = resume.get("salary_expectation") or {}
    expected_min = coerce_int(expectation.get("min"), 0)
    if salary and expected_min:
        if salary[1] < expected_min / 1000.0:
            score -= 15
            reasons.append("薪资低于期望")
        elif salary[0] >= expected_min / 1000.0:
            score += 8

    # 4. 经验
    years = _parse_years(job.get("experience"))
    my_years = coerce_int(resume.get("years_of_experience"), 0)
    if years:
        if years[0] > my_years + 2:
            score -= 10
            reasons.append("经验要求偏高")
        elif years[0] <= my_years:
            score += 5

    # 5. 技能（职位名 + 技能标签 + 标签 + 描述前 400 字）
    my_skills = [str(skill) for skill in (resume.get("skills") or []) if skill]
    job_text = " ".join([
        str(job.get("job_name") or ""),
        str(job.get("skills") or ""),
        str(job.get("job_labels") or ""),
        str(job.get("job_description") or "")[:400],
    ]).lower()
    if my_skills:
        hits = sum(1 for skill in my_skills if _skill_hit(skill, job_text))
        score += (hits / float(len(my_skills))) * 20
        if hits:
            reasons.append("命中 %d 项技能" % hits)

    return max(0, min(100, round(score))), reasons


def load_latest_results():
    """加载全部结果（合并去重）。函数名保留以兼容旧调用。"""
    jobs = load_all_jobs()
    if jobs:
        print("📂 已加载 %d 个职位（合并 data/boss_results/ 下全部结果并去重）" % len(jobs))
    return jobs


def coarse_filter(jobs, resume, top_n=None):
    """按规则分排序，返回前 top_n 个。返回 [(job, score, reasons)]。"""
    top_n = top_n or MAX_CANDIDATES
    scored = []
    for job in jobs or []:
        if not job.get("job_name"):
            continue
        value, reasons = score_job(job, resume)
        scored.append((job, value, reasons))
    scored.sort(key=lambda item: -item[1])
    return scored[:top_n]


def main(argv=None):
    setup_console()
    parser = argparse.ArgumentParser(description="规则粗筛自测")
    parser.add_argument("-r", "--resume", help="简历结构化 JSON 路径", default=None)
    parser.add_argument("-n", "--top", type=int, default=15, help="显示前 N 条")
    args = parser.parse_args(argv)

    resume_path = args.resume or os.path.join(OUTPUT_DIR, "resume_structured.json")
    if not os.path.exists(resume_path):
        print("❌ 未找到简历结构化数据，请先运行: python run.py parse -f 简历路径")
        return 1
    with open(resume_path, "r", encoding="utf-8") as handle:
        resume = json.load(handle)

    jobs = load_latest_results()
    if not jobs:
        print("❌ 没有职位数据，请先运行爬虫")
        return 1
    print("共 %d 个职位" % len(jobs))
    candidates = coarse_filter(jobs, resume)
    print("粗筛后 TOP %d：" % len(candidates))
    for job, value, reasons in candidates[:args.top]:
        print("  [%3d] %s | %s | %s | %s" % (
            value, job.get("job_name"), job.get("salary"),
            job.get("company_name"), "; ".join(reasons),
        ))
    return 0


if __name__ == "__main__":
    sys.exit(main())
