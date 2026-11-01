# -*- coding: utf-8 -*-
"""技能词清洗（分析报告与面板共用）。

Boss 的 skills 字段里混了不少非技能词（福利/工作条件/学历），直接统计会失真。
"""

import re

# 完整匹配黑名单
BLACKLIST = {
    "接受", "不接受", "居家办公", "周末双休", "甲方公司", "乙方公司", "现场办公",
    "技能", "无经验要求", "经验不限", "计算机相关专业", "学历不限", "公司", "科技",
    "互联网", "软件", "企业", "产品", "团队", "能力", "优先", "加分", "熟练",
    "熟悉", "了解", "掌握", "负责", "参与", "实习生", "实习", "应届", "本科",
    "硕士", "大专", "博士", "B端", "C端", "To", "B)", "C)", "B）", "C）", "to",
    "好", "优", "强", "大", "小", "中", "以上", "以下", "及以上", "及以下",
    "具备", "具有", "拥有", "良好", "优秀", "扎实", "一定", "相关",
    "经验", "工作", "岗位", "职位", "招聘", "全职", "兼职", "远程", "提供",
}

# 包含这些片段的词也丢掉
CONTAINS_BLACKLIST = (
    "接受", "居家", "双休", "甲方", "乙方", "办公", "优先", "经验", "学历",
    "专业", "相关", "以上", "福利", "薪资", "年假", "五险",
)

_SPLIT = re.compile(r"[,\s/、，；;|]+")


def clean_skills(value):
    """接受 skills 字符串或列表，返回去噪后的技能词列表。"""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        raw = " ".join(str(item) for item in value)
    else:
        raw = str(value)

    result = []
    for word in _SPLIT.split(raw):
        word = word.strip()
        if not word or len(word) < 2:
            continue
        if word in BLACKLIST:
            continue
        if any(bad in word for bad in CONTAINS_BLACKLIST):
            continue
        result.append(word)
    return result
