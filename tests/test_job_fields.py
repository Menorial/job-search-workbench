# -*- coding: utf-8 -*-
"""职位字段归一化测试（历史数据里的脏值要能被清洗掉）。"""

import pytest

from common.job_fields import normalize_education, normalize_experience


@pytest.mark.parametrize("value,expected", [
    ("本科", "本科"),
    ("本科完工结", "本科"),
    ("大专月结", "大专"),
    ("大专周结", "大专"),
    ("学历不限", "学历不限"),
    ("不限", "学历不限"),
    ("", "不限"),
    (None, "不限"),
    ("中专/中技", "中专/中技"),
    ("初中及以下", "初中及以下"),
    ("硕士", "硕士"),
    ("看不懂的学历", "其他"),
])
def test_normalize_education(value, expected):
    assert normalize_education(value) == expected


@pytest.mark.parametrize("value,expected", [
    ("3-5年", "3-5年"),
    ("1-3年", "1-3年"),
    ("5年", "5年"),
    ("在校/应届", "应届/在校"),
    ("应届生", "应届/在校"),
    ("经验不限", "经验不限"),
    ("无经验", "经验不限"),
    ("成都 1-3年 本科", "1-3年"),
    ("深圳 经验不限 本科", "经验不限"),
    ("5天/周 6个月", "实习"),
    ("", "未标注"),
])
def test_normalize_experience(value, expected):
    assert normalize_experience(value) == expected
