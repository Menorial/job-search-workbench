# -*- coding: utf-8 -*-
"""结果合并测试：多个 batch 去重、字段互补、缓存失效。"""

import json
import time

import data_store


def _write(path, jobs):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(jobs, handle, ensure_ascii=False)


def _reset(tmp_dir, monkeypatch):
    monkeypatch.setattr(data_store, "RESULTS_DIR", str(tmp_dir))
    monkeypatch.setattr(data_store, "_CACHE", {"signature": None, "jobs": []})


def test_merge_across_files_fills_missing_fields(tmp_dir, monkeypatch):
    _reset(tmp_dir, monkeypatch)
    old = tmp_dir / "boss_a.json"
    new = tmp_dir / "boss_b.json"
    _write(old, [{"job_id": "J1", "job_name": "Python", "company_name": "A",
                  "job_description": "", "salary": "15-25K"}])
    time.sleep(0.01)
    _write(new, [{"job_id": "J1", "job_name": "Python", "company_name": "A",
                  "job_description": "负责后端开发", "salary": "15-25K"}])

    jobs = data_store.load_all_jobs(refresh=True)
    assert len(jobs) == 1
    assert jobs[0]["job_description"] == "负责后端开发"
    assert jobs[0]["salary"] == "15-25K"


def test_duplicate_job_ids_are_collapsed(tmp_dir, monkeypatch):
    _reset(tmp_dir, monkeypatch)
    _write(tmp_dir / "a.json", [{"job_id": "J1", "job_name": "A"}])
    _write(tmp_dir / "b.json", [{"job_id": "J1", "job_name": "A"},
                                 {"job_id": "J2", "job_name": "B"}])
    jobs = data_store.load_all_jobs(refresh=True)
    assert {job["job_id"] for job in jobs} == {"J1", "J2"}


def test_ignores_internal_and_broken_files(tmp_dir, monkeypatch):
    _reset(tmp_dir, monkeypatch)
    _write(tmp_dir / "_detail_progress.json", {"x": 1})
    (tmp_dir / "broken.json").write_text("{oops", encoding="utf-8")
    _write(tmp_dir / "ok.json", [{"job_id": "J1", "job_name": "A"}])
    jobs = data_store.load_all_jobs(refresh=True)
    assert len(jobs) == 1


def test_job_key_falls_back_to_hash():
    key = data_store.job_key({"job_name": "A", "company_name": "B", "salary": "1K"})
    assert key.startswith("hash:")
    assert data_store.job_key({"job_id": "J9"}) == "J9"


def test_cache_reuses_when_files_unchanged(tmp_dir, monkeypatch):
    _reset(tmp_dir, monkeypatch)
    _write(tmp_dir / "a.json", [{"job_id": "J1", "job_name": "A"}])
    first = data_store.load_all_jobs(refresh=True)
    second = data_store.load_all_jobs(refresh=False)
    assert first is second  # 命中 mtime 缓存，没有重新解析


def test_merge_job_prefers_new_non_empty():
    merged = data_store.merge_job(
        {"job_id": "J1", "job_name": "A", "hr_name": "张三"},
        {"job_id": "J1", "job_name": "", "hr_name": "李四"},
    )
    assert merged["job_name"] == "A"
    assert merged["hr_name"] == "李四"
