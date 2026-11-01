# -*- coding: utf-8 -*-
"""规则粗筛测试（覆盖旧版的城市判断优先级 bug 与薪资口径）。"""

from filter import (
    _degree_of,
    _match_city,
    _parse_years,
    coarse_filter,
    job_city_name,
    score_job,
)

RESUME = {
    "years_of_experience": 3,
    "education": {"degree": "本科"},
    "cities": ["成都"],
    "salary_expectation": {"min": 20000, "max": 30000},
    "skills": ["Python", "Django", "Redis"],
}


def test_job_city_name_prefers_city_field():
    assert job_city_name({"cityName": "成都", "location": ""}) == "成都"
    assert job_city_name({"location": "成都 武侯区 软件园"}) == "成都"
    assert job_city_name({}) == ""


def test_match_city_with_only_city_name():
    # 旧实现：只有 cityName、没有 location 时返回空串 -> 误判不匹配
    assert _match_city({"cityName": "成都"}, ["成都"]) is True
    assert _match_city({"location": "成都 武侯区"}, ["成都"]) is True
    assert _match_city({"location": "北京 朝阳区"}, ["成都"]) is False
    assert _match_city({"location": "北京"}, []) is True


def test_parse_years():
    assert _parse_years("3-5年") == (3, 5)
    assert _parse_years("5年") == (5, 5)
    assert _parse_years("在校/应届") == (0, 1)
    assert _parse_years("经验不限") == (0, 99)
    assert _parse_years("") is None


def test_degree_level():
    assert _degree_of({"education": "本科"}) == 4
    assert _degree_of({"education": "学历不限"}) == 0
    assert _degree_of({}) == 0


def test_score_prefers_matching_city_and_salary():
    good = {
        "job_name": "Python开发", "location": "成都 武侯区", "salary": "20K",
        "experience": "1-3年", "education": "本科", "skills": "Python, Redis",
    }
    bad = {
        "job_name": "Python开发", "location": "北京 朝阳区", "salary": "6-8K",
        "experience": "5-10年", "education": "硕士", "skills": "Python, Redis",
    }
    good_score, good_reasons = score_job(good, RESUME)
    bad_score, bad_reasons = score_job(bad, RESUME)
    assert good_score > bad_score
    assert any("城市" in reason for reason in bad_reasons)
    assert any("技能" in reason for reason in good_reasons)


def test_single_value_salary_counts():
    # '20K' 这种单值薪资旧的分析模块会漏掉，规则分这里应当识别
    with_salary = {"job_name": "Python", "location": "成都", "salary": "20K",
                   "experience": "3年", "education": "本科", "skills": "Python"}
    without = dict(with_salary, salary="面议")
    assert score_job(with_salary, RESUME)[0] > score_job(without, RESUME)[0]


def test_skill_match_avoids_java_javascript_confusion():
    resume = {"skills": ["Java"], "cities": [], "education": {}, "years_of_experience": 0}
    job = {"job_name": "JavaScript 前端", "skills": "JavaScript", "location": "成都"}
    score, reasons = score_job(job, resume)
    assert not any("命中" in reason for reason in reasons)
    assert score <= 70


def test_coarse_filter_sorts_and_limits():
    jobs = [
        {"job_name": "A", "location": "成都", "salary": "20K", "skills": "Python"},
        {"job_name": "B", "location": "北京", "salary": "5K", "skills": "Python"},
        {"job_name": "", "location": "成都"},  # 无职位名，应被跳过
    ]
    result = coarse_filter(jobs, RESUME, top_n=1)
    assert len(result) == 1
    assert result[0][0]["job_name"] == "A"
