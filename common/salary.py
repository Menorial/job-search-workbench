# -*- coding: utf-8 -*-
"""薪资文本解析（统一实现）。

原项目在 filter.py / analyzer.py / dashboard.py 各有一份，且行为不一致
（有的支持 '20K' 单值、有的只支持 '15-25K'），导致统计偏差。
"""

import re
import statistics

# '15-25K' / '15K-25K' / '15-25千'
_RANGE_K = re.compile(r"(\d+(?:\.\d+)?)\s*[kK千]\s*[-~—–至]\s*(\d+(?:\.\d+)?)\s*[kK千]?")
_RANGE_K2 = re.compile(r"(\d+(?:\.\d+)?)\s*[-~—–至]\s*(\d+(?:\.\d+)?)\s*[kK千]")
# '20K' / '20k'
_SINGLE_K = re.compile(r"(\d+(?:\.\d+)?)\s*[kK千](?![a-zA-Z])")
# '1-2万' / '1.5万'（万 = 10K）
_RANGE_WAN = re.compile(r"(\d+(?:\.\d+)?)\s*[-~—–至]\s*(\d+(?:\.\d+)?)\s*万")
_SINGLE_WAN = re.compile(r"(\d+(?:\.\d+)?)\s*万")
# '15000-25000元/月'
_RANGE_YUAN = re.compile(r"(\d{4,6})\s*[-~—–至]\s*(\d{4,6})\s*元")
_SINGLE_YUAN = re.compile(r"(\d{4,6})\s*元")

_MAX_K = 500.0  # 超过 500K/月 视为解析异常（避免把「13 薪」「人数」当薪资）


def _ok(lo, hi):
    if lo is None or hi is None:
        return None
    if lo > hi:
        lo, hi = hi, lo
    if lo <= 0 or hi > _MAX_K:
        return None
    return (round(float(lo), 1), round(float(hi), 1))


def parse_salary(text):
    """把薪资描述解析成 (最低K, 最高K)，单位「K/月」。

    支持：'15-25K'、'15K-25K'、'20K'、'1-2万'、'15000-25000元/月'、'15-25K·13薪'、
         '年薪 20-30 万'（会折算成月薪）。
    日薪/时薪（'200-300元/天'）与「面议」返回 None —— 不硬换算，避免污染月薪统计。
    """
    if not text:
        return None
    s = str(text).strip()
    if not s or "面议" in s or "元/天" in s or "元/时" in s or "元/小时" in s:
        return None

    # 年薪：按 12 个月折算
    annual = "年薪" in s or "年包" in s

    m = _RANGE_YUAN.search(s)
    if m:
        low, high = float(m.group(1)) / 1000.0, float(m.group(2)) / 1000.0
        return _ok(low / 12.0, high / 12.0) if annual else _ok(low, high)

    m = _RANGE_K.search(s) or _RANGE_K2.search(s)
    if m:
        low, high = float(m.group(1)), float(m.group(2))
        return _ok(low / 12.0, high / 12.0) if annual else _ok(low, high)

    m = _RANGE_WAN.search(s)
    if m:
        low, high = float(m.group(1)) * 10.0, float(m.group(2)) * 10.0
        return _ok(low / 12.0, high / 12.0) if annual else _ok(low, high)

    m = _SINGLE_WAN.search(s)
    if m:
        value = float(m.group(1)) * 10.0
        return _ok(value / 12.0, value / 12.0) if annual else _ok(value, value)

    m = _SINGLE_YUAN.search(s)
    if m:
        value = float(m.group(1)) / 1000.0
        return _ok(value, value)

    m = _SINGLE_K.search(s)
    if m:
        value = float(m.group(1))
        return _ok(value, value)
    return None


def salary_stats(values):
    """对 [(lo, hi), ...] 做统计，中位数用 statistics.median（偶数时取均值）。"""
    values = [v for v in (values or []) if v]
    if not values:
        return {"sample": 0, "min": 0.0, "max": 0.0, "median_low": 0.0, "median_high": 0.0}
    lows = sorted(v[0] for v in values)
    highs = sorted(v[1] for v in values)
    return {
        "sample": len(values),
        "min": round(lows[0], 1),
        "max": round(highs[-1], 1),
        "median_low": round(statistics.median(lows), 1),
        "median_high": round(statistics.median(highs), 1),
    }


def salary_from_job(job):
    """优先用爬虫预解析的 salary_min/max，缺失时回退解析 salary 文本。"""
    if not job:
        return None
    lo, hi = job.get("salary_min"), job.get("salary_max")
    try:
        if lo not in (None, "") and hi not in (None, ""):
            return _ok(float(lo), float(hi))
    except (TypeError, ValueError):
        pass
    return parse_salary(job.get("salary") or "")
