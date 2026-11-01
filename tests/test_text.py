# -*- coding: utf-8 -*-
"""文本工具测试：文件名清洗 / 去重 / 截断 / 数值容错 / 技能清洗。"""

from common.skills import clean_skills
from common.text import (
    coerce_int,
    dedupe_lines,
    normalize_markdown_lists,
    sanitize_filename,
    truncate_middle,
)


def test_sanitize_filename_removes_illegal_chars():
    assert sanitize_filename("C/C++") == "C_C++"
    assert sanitize_filename('a:b*c?"d"<e>f|g') == "a_b_c__d__e_f_g"
    assert "/" not in sanitize_filename("a/b/c")
    # Windows 保留名要避开
    assert sanitize_filename("CON").startswith("_")
    # 空输入有兜底
    assert sanitize_filename("") == "untitled"
    # 长度截断
    assert len(sanitize_filename("x" * 300, maxlen=20)) == 20


def test_dedupe_lines_keeps_order():
    text = "技能\nPython\nPython\n项目\nPython"
    assert dedupe_lines(text) == "技能\nPython\n项目"


def test_truncate_middle_keeps_tail():
    text = "HEAD" + "x" * 500 + "TAIL"
    result = truncate_middle(text, 100)
    assert result.startswith("HEAD")
    assert result.endswith("TAIL")
    assert len(result) < len(text)
    assert "省略" in result


def test_coerce_int_handles_model_output():
    assert coerce_int("85") == 85
    assert coerce_int("85分") == 85
    assert coerce_int("85/100") == 85
    assert coerce_int(85.9) == 85
    assert coerce_int("abc", default=7) == 7
    assert coerce_int(None) == 0
    assert coerce_int(150, 0, 0, 100) == 100
    assert coerce_int(-5, 0, 0, 100) == 0


def test_clean_skills_filters_blacklist():
    skills = "Python, 五险一金, 本科, 团队合作能力, Redis, 周末双休"
    result = clean_skills(skills)
    assert "Python" in result
    assert "Redis" in result
    assert "本科" not in result
    assert "五险一金" not in result
    assert "周末双休" not in result


def test_clean_skills_accepts_list():
    assert clean_skills(["Python", "MySQL"]) == ["Python", "MySQL"]


def test_normalize_markdown_lists_adds_blank_line():
    text = "**打招呼话术:**\n- 简短版: 你好\n- 技能版: 我会 Python\n- 项目版: 做过 RAG"
    fixed = normalize_markdown_lists(text)
    assert "**打招呼话术:**\n\n- 简短版" in fixed
    # 连续列表项之间不应该被拆开
    assert "- 简短版: 你好\n- 技能版" in fixed


def test_normalize_markdown_lists_keeps_existing_blank_lines():
    text = "**标题**\n\n- 一\n- 二"
    assert normalize_markdown_lists(text) == text


def test_normalize_markdown_lists_splits_bold_heading_after_list():
    text = "- 薪资匹配：匹配度高\n**风险点:**\n- 候选人无正式经验\n- 技能需补充"
    fixed = normalize_markdown_lists(text)
    assert "- 薪资匹配：匹配度高\n\n**风险点:**\n\n- 候选人无正式经验\n- 技能需补充" == fixed
