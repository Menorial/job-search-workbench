# -*- coding: utf-8 -*-
"""市场分析：本地聚合统计 + DeepSeek 生成 report.md。

省钱设计不变：不把原始职位喂给模型，只喂聚合后的几百 token 统计。
"""

import collections
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from common.console import setup_console
from common.job_fields import normalize_education, normalize_experience
from common.salary import salary_from_job, salary_stats
from common.skills import clean_skills

from config import OUTPUT_DIR, check_config
from data_store import load_all_jobs
from llm import LLMError, chat, log_usage


def _salary_buckets(salaries, step=5.0):
    """把薪资按 5K 一档分桶，给报告里的「区间」表用。"""
    counter = collections.Counter()
    for low, _high in salaries:
        bucket = int(low // step) * int(step)
        counter["%d-%dK" % (bucket, bucket + int(step))] += 1
    ordered = sorted(counter.items(), key=lambda item: int(item[0].split("-")[0]))
    return ordered


def aggregate(jobs):
    """本地聚合：薪资 / 技能 / 城市 / 学历 / 经验。"""
    jobs = jobs or []
    salaries = [value for value in (salary_from_job(job) for job in jobs) if value]
    stats = salary_stats(salaries)

    skill_counter = collections.Counter()
    for job in jobs:
        for skill in clean_skills(job.get("skills")):
            skill_counter[skill] += 1

    city_counter = collections.Counter()
    for job in jobs:
        location = job.get("location") or job.get("cityName") or ""
        city = location.split()[0] if location else "未知"
        city_counter[city] += 1

    edu_counter = collections.Counter()
    for job in jobs:
        edu_counter[normalize_education(job.get("education"))] += 1

    exp_counter = collections.Counter()
    for job in jobs:
        exp_counter[normalize_experience(job.get("experience"))] += 1

    with_detail = sum(1 for job in jobs if len(job.get("job_description") or "") >= 20)

    return {
        "职位总数": len(jobs),
        "有职位描述的条数": with_detail,
        "薪资": {
            "样本数": stats["sample"],
            "中位数下限K": stats["median_low"],
            "中位数上限K": stats["median_high"],
            "最低K": stats["min"],
            "最高K": stats["max"],
            "区间分布": _salary_buckets(salaries),
        },
        "技能TOP20": skill_counter.most_common(20),
        "城市分布": city_counter.most_common(10),
        "学历分布": edu_counter.most_common(8),
        "经验分布": exp_counter.most_common(8),
    }


SYSTEM_PROMPT = """你是一位招聘市场分析师。用户会给你一份职位数据的聚合统计，请生成一份 Markdown 市场分析报告。

报告结构（用 Markdown 标题和表格）：
# 职位市场分析报告
## 一、市场概览（职位总量、薪资中位数、整体供需判断，2-3 句话）
## 二、薪资分析（表格：区间/数量；再给中位数/最低/最高，2-3 句解读）
## 三、热门技能 TOP20（表格：技能/出现次数；解读哪些是硬通货）
## 四、城市分布（表格；解读）
## 五、学历与经验要求（表格；解读）
## 六、求职建议（结合以上数据给求职者 3-5 条具体建议）

要求：
- 所有数字来自给定统计，不要编造
- 语言专业、简洁、中文
- 只输出 Markdown 内容"""


def generate_report(jobs=None):
    """生成市场分析报告 -> ai_output/report.md"""
    if not check_config():
        return None

    if jobs is None:
        jobs = load_all_jobs()
    if not jobs:
        print("❌ 没有职位数据，请先运行爬虫")
        return None

    print("📊 聚合统计 %d 个职位..." % len(jobs))
    stats = aggregate(jobs)

    print("🤖 调用 DeepSeek 生成报告...")
    try:
        report = chat([
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": "职位数据聚合统计（JSON）：\n%s"
                                        % json.dumps(stats, ensure_ascii=False, indent=2)},
        ], temperature=0.4, max_tokens=3000)
    except LLMError as exc:
        print("❌ 报告生成失败: %s" % exc)
        return None

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    out_file = os.path.join(OUTPUT_DIR, "report.md")
    tmp = out_file + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(report)
    os.replace(tmp, out_file)
    print("💾 报告已保存: %s" % out_file)
    log_usage("report")
    return report


def main():
    setup_console()
    return 0 if generate_report() else 1


if __name__ == "__main__":
    sys.exit(main())
