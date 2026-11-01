# -*- coding: utf-8 -*-
"""AI 匹配测试：分数归一 / 缓存键 / 缓存版本。"""

import json

import matcher
from matcher import (
    build_system_prompt,
    cache_key,
    load_cache,
    normalize_result,
    resume_fingerprint,
    save_cache,
)


def test_normalize_result_coerces_string_score():
    # 旧实现 setdefault("score", 0) 后直接排序，模型返回 "85" 会让 sorted 抛 TypeError
    result = normalize_result({"score": "85分", "dims": {"skill": "90", "exp": 70},
                               "reasons": ["a", "", "b"], "risks": None})
    assert result["score"] == 85
    assert isinstance(result["score"], int)
    assert result["dims"]["skill"] == 90
    assert result["dims"]["exp"] == 70
    assert result["dims"]["edu"] == 0
    assert result["reasons"] == ["a", "b"]
    assert result["risks"] == []
    assert result["cover_tips"] == ""


def test_normalize_result_clamps_range():
    assert normalize_result({"score": 150})["score"] == 100
    assert normalize_result({"score": -10})["score"] == 0
    assert normalize_result(None)["score"] == 0


def test_resume_fingerprint_changes_with_resume():
    first = resume_fingerprint({"skills": ["Python"], "years_of_experience": 3})
    second = resume_fingerprint({"skills": ["Python"], "years_of_experience": 4})
    assert first != second
    assert first == resume_fingerprint({"years_of_experience": 3, "skills": ["Python"]})


def test_cache_key_includes_resume_hash():
    assert cache_key("job1", "aaa") != cache_key("job1", "bbb")


def test_system_prompt_uses_config_weights():
    prompt = build_system_prompt()
    assert "30%" in prompt  # 技能权重来自 config
    assert "技能" in prompt


def test_cache_roundtrip(tmp_dir, monkeypatch):
    cache_file = tmp_dir / "match_cache.json"
    monkeypatch.setattr(matcher, "CACHE_FILE", str(cache_file))
    entries = {"job1|abc|v2": {"score": 88, "dims": {}, "reasons": [], "risks": [],
                               "cover_tips": "hi"}}
    save_cache(entries)
    loaded, usable = load_cache()
    assert usable is True
    assert loaded["job1|abc|v2"]["score"] == 88


def test_old_cache_format_is_invalidated(tmp_dir, monkeypatch):
    # 旧格式：{job_id: {...}}，没有版本也没有绑定简历 -> 必须失效，否则简历改了就出错分
    cache_file = tmp_dir / "match_cache.json"
    with open(cache_file, "w", encoding="utf-8") as handle:
        json.dump({"job1": {"score": 90}}, handle)
    monkeypatch.setattr(matcher, "CACHE_FILE", str(cache_file))
    entries, usable = load_cache()
    assert entries == {}
    assert usable is False


def test_corrupt_cache_is_invalidated(tmp_dir, monkeypatch):
    cache_file = tmp_dir / "match_cache.json"
    cache_file.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(matcher, "CACHE_FILE", str(cache_file))
    entries, usable = load_cache()
    assert entries == {}
    assert usable is False


def test_match_all_handles_coarse_filter_tuples(tmp_dir, monkeypatch):
    """回归：coarse_filter 返回 (job, score, reasons) 元组，不能直接当 job 用。"""
    monkeypatch.setattr(matcher, "OUTPUT_DIR", str(tmp_dir))
    monkeypatch.setattr(matcher, "check_config", lambda: True)
    monkeypatch.setattr(matcher, "load_cache", lambda: ({}, True))
    monkeypatch.setattr(matcher, "save_cache", lambda entries: None)
    monkeypatch.setattr(matcher, "log_usage", lambda *args, **kwargs: None)
    monkeypatch.setattr(matcher, "usage_snapshot", lambda: {
        "calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "cost": 0.0})
    monkeypatch.setattr(matcher, "_score_one", lambda resume, job, prompt: (
        {"score": 80, "dims": {key: 80 for key in matcher.DIM_KEYS},
         "reasons": ["技能匹配"], "risks": [], "cover_tips": "你好"}, None))

    resume = {"skills": ["Python"], "cities": [], "education": {},
              "years_of_experience": 1}
    jobs = [{"job_id": "J1", "job_name": "Python 开发", "location": "成都",
             "skills": "Python"}]

    results = matcher.match_all(resume, jobs=jobs, max_candidates=10, concurrency=1)

    assert len(results) == 1
    assert results[0]["job"]["job_id"] == "J1"
    assert results[0]["score"] == 80
    assert (tmp_dir / "matched_jobs.json").exists()


def test_match_all_keeps_previous_results_when_all_fail(tmp_dir, monkeypatch):
    """回归：全部调用失败时，不能把上一次的 matched_jobs.json 覆盖成空。"""
    previous = tmp_dir / "matched_jobs.json"
    previous.write_text(json.dumps([{"job": {"job_id": "OLD"}, "score": 88}]),
                        encoding="utf-8")

    monkeypatch.setattr(matcher, "OUTPUT_DIR", str(tmp_dir))
    monkeypatch.setattr(matcher, "check_config", lambda: True)
    monkeypatch.setattr(matcher, "load_cache", lambda: ({}, True))
    saved = []
    monkeypatch.setattr(matcher, "save_cache", lambda entries: saved.append(entries))
    monkeypatch.setattr(matcher, "log_usage", lambda *args, **kwargs: None)
    monkeypatch.setattr(matcher, "usage_snapshot", lambda: {
        "calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "cost": 0.0})
    monkeypatch.setattr(matcher, "_score_one",
                        lambda resume, job, prompt: (None, "401 invalid key"))

    resume = {"skills": ["Python"], "cities": [], "education": {},
              "years_of_experience": 1}
    jobs = [{"job_id": "J1", "job_name": "Python", "location": "成都", "skills": "Python"}]

    results = matcher.match_all(resume, jobs=jobs, max_candidates=5, concurrency=1)

    assert results == []
    assert json.loads(previous.read_text(encoding="utf-8"))[0]["job"]["job_id"] == "OLD"
    assert saved == []          # 也不应该写空缓存
