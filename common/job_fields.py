# -*- coding: utf-8 -*-
"""职位字段归一化（把爬取/历史数据里的脏值清洗成可展示、可统计的值）。

背景：详情页的「城市 经验 学历」有时挤在同一行，历史数据里会出现
`'本科完工结'`（结算方式混进学历）、`'成都 1-3年 本科'`（整行混进经验），
直接拿去画图既难看又失真。
"""

import re

# 注意顺序：长的、更具体的放前面（'初中及以下' 必须先于 '初中'）
DEGREE_ORDER = (
    "学历不限", "博士", "硕士", "本科", "大专",
    "中专/中技", "中技", "中专", "高中", "初中及以下", "初中",
)

_EXP_RANGE = re.compile(r"(\d+)\s*[-~—–]\s*(\d+)\s*年")
_EXP_SINGLE = re.compile(r"(\d+)\s*年")


def normalize_education(value):
    """'本科完工结' -> '本科'，'不限' -> '学历不限'，识别不了 -> '其他'。"""
    text = str(value or "").strip()
    if not text:
        return "不限"
    for word in DEGREE_ORDER:
        if word in text:
            return word
    if "不限" in text:
        return "学历不限"
    return "其他"


def normalize_experience(value):
    """'成都 1-3年 本科' -> '1-3年'，'5天/周 6个月' -> '实习'。"""
    text = str(value or "").strip()
    if not text:
        return "未标注"
    if "应届" in text or "在校" in text:
        return "应届/在校"
    if "不限" in text or "无经验" in text:
        return "经验不限"
    if "天/周" in text or "个月" in text:
        return "实习"
    match = _EXP_RANGE.search(text)
    if match:
        return "%s-%s年" % (match.group(1), match.group(2))
    match = _EXP_SINGLE.search(text)
    if match:
        return "%s年" % match.group(1)
    return "未标注"
