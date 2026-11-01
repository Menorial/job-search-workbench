# -*- coding: utf-8 -*-
"""薪资解析测试（统一实现后，三处统计口径必须一致）。"""

import pytest

from common.salary import parse_salary, salary_from_job, salary_stats


@pytest.mark.parametrize("text,expected", [
    ("15-25K", (15.0, 25.0)),
    ("15K-25K", (15.0, 25.0)),
    ("20K", (20.0, 20.0)),
    ("15-25K·13薪", (15.0, 25.0)),
    ("15000-25000元/月", (15.0, 25.0)),
    ("1-2万", (10.0, 20.0)),
    ("1.5万", (15.0, 15.0)),
    ("年薪20-30万", (16.7, 25.0)),
    ("面议", None),
    ("200-300元/天", None),
    ("50-80元/时", None),
    ("", None),
    (None, None),
])
def test_parse_salary(text, expected):
    assert parse_salary(text) == expected


def test_salary_stats_uses_true_median():
    # 偶数样本取中间两个的均值：15 和 25 -> 20（旧实现取上中位数 25）
    stats = salary_stats([(20.0, 30.0), (10.0, 20.0)])
    assert stats["median_low"] == 15.0
    assert stats["median_high"] == 25.0
    assert stats["sample"] == 2


def test_salary_stats_empty():
    stats = salary_stats([])
    assert stats == {"sample": 0, "min": 0.0, "max": 0.0, "median_low": 0.0, "median_high": 0.0}


def test_salary_from_job_prefers_parsed_fields():
    job = {"salary": "面议", "salary_min": 12, "salary_max": 18}
    assert salary_from_job(job) == (12.0, 18.0)


def test_salary_from_job_falls_back_to_text():
    job = {"salary": "20-30K"}
    assert salary_from_job(job) == (20.0, 30.0)
