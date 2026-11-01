# -*- coding: utf-8 -*-
"""AI 任务（解析/匹配/报告/话术）的参数校验、命令构造与进度推导。"""

import json
import sys
import time

import pytest

import crawl_service
from crawl_service import (
    CrawlError,
    TaskRunner,
    build_ai_command,
    derive_progress,
    validate_match,
    validate_parse,
    validate_top10,
)


# ============ 参数校验 ============

def test_validate_parse_accepts_resume_dir(tmp_dir, monkeypatch):
    monkeypatch.setattr(crawl_service, "RESUME_DIR", str(tmp_dir))
    path = tmp_dir / "我的简历.docx"
    path.write_bytes(b"x")
    params = validate_parse({"path": str(path)})
    assert params["path"].endswith("我的简历.docx")
    assert params["force"] is False


def test_validate_parse_rejects_outside_dir(tmp_dir, monkeypatch):
    monkeypatch.setattr(crawl_service, "RESUME_DIR", str(tmp_dir / "resume"))
    (tmp_dir / "resume").mkdir()
    outside = tmp_dir / "other.docx"
    outside.write_bytes(b"x")
    with pytest.raises(CrawlError):
        validate_parse({"path": str(outside)})


def test_validate_parse_rejects_bad_extension(tmp_dir, monkeypatch):
    monkeypatch.setattr(crawl_service, "RESUME_DIR", str(tmp_dir))
    path = tmp_dir / "virus.exe"
    path.write_bytes(b"x")
    with pytest.raises(CrawlError):
        validate_parse({"path": str(path)})


def test_validate_match_defaults_and_limits():
    expected = {"max": crawl_service.MAX_CANDIDATES,
                "concurrency": crawl_service.CONCURRENCY, "latest": False}
    assert validate_match({}) == expected
    assert validate_match({"max": 20, "concurrency": 5})["max"] == 20
    for payload in ({"max": 0}, {"max": 999}, {"concurrency": 0}, {"concurrency": 9},
                    {"max": "abc"}):
        with pytest.raises(CrawlError):
            validate_match(payload)


def test_validate_top10():
    assert validate_top10({})["top"] == 10
    assert validate_top10({"top": 5})["top"] == 5
    for payload in ({"top": 0}, {"top": 99}, {"top": "x"}):
        with pytest.raises(CrawlError):
            validate_top10(payload)


# ============ 命令构造 ============

def test_build_ai_command_match(tmp_path):
    params = validate_match({"max": 30, "concurrency": 4})
    command = build_ai_command("match", params, str(tmp_path / "e.jsonl"))
    assert command[0] == sys.executable
    assert command[1] == "-u"
    assert command[2].endswith("run.py")
    assert "match" in command
    assert "-n" in command and "30" in command
    assert "-j" in command and "4" in command
    assert "--events" in command
    assert command[-1].endswith("e.jsonl")


def test_build_ai_command_parse_and_top10(tmp_path):
    parse_command = build_ai_command("parse", {"path": "C:/r/cv.docx", "force": True},
                                     str(tmp_path / "a.jsonl"))
    assert "parse" in parse_command and "--force" in parse_command
    top_command = build_ai_command("top10", {"top": 8}, str(tmp_path / "b.jsonl"))
    assert "top10" in top_command and "8" in top_command
    report_command = build_ai_command("report", {}, None)
    assert "report" in report_command
    assert "--events" not in report_command


def test_build_ai_command_rejects_unknown_kind(tmp_path):
    with pytest.raises(CrawlError):
        build_ai_command("rm -rf", {}, str(tmp_path / "x"))


# ============ 进度推导 ============

def test_progress_for_match_task():
    events = [
        {"event": "start", "task": "match"},
        {"event": "phase", "name": "match", "total": 10},
        {"event": "progress", "phase": "match", "current": 5, "total": 10, "label": "Python"},
    ]
    progress = derive_progress(events, {"kind": "match"})
    assert progress["percent"] == 50
    assert "5/10" in progress["text"]
    assert "Python" in progress["text"]


def test_progress_for_parse_task_is_full_when_done():
    events = [
        {"event": "phase", "name": "parse", "total": 1},
        {"event": "progress", "phase": "parse", "current": 1, "total": 1},
        {"event": "saved", "path": "x"},
    ]
    progress = derive_progress(events, {"kind": "parse"})
    assert progress["percent"] == 100


def test_progress_reports_error_text():
    progress = derive_progress([{"event": "error", "message": "API Key 未配置"}], {"kind": "match"})
    assert "API Key" in progress["text"]


# ============ 任务启动 ============

FAKE_AI = r'''
# -*- coding: utf-8 -*-
import json, sys, time

args = sys.argv[1:]
events = args[args.index("--events") + 1] if "--events" in args else None

def emit(**record):
    if events:
        with open(events, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

emit(event="start", task="report")
emit(event="phase", name="report", total=1)
time.sleep(0.1)
emit(event="progress", phase="report", current=1, total=1, label="报告")
emit(event="saved", path="report.md")
emit(event="done", status="ok")
'''


def _wait(runner, timeout=10):
    status = {}
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = runner.status()
        if status["status"] != "running":
            return status
        time.sleep(0.05)
    return status


def test_runner_start_ai_task(tmp_dir, monkeypatch):
    fake = tmp_dir / "fake_ai.py"
    fake.write_text(FAKE_AI, encoding="utf-8")
    monkeypatch.setattr(crawl_service, "AI_RUN", str(fake))

    runner = TaskRunner(log_dir=str(tmp_dir))
    task = runner.start_task("report", {})
    assert task["kind"] == "report"

    status = _wait(runner)
    assert status["status"] == "done", status
    assert status["progress"]["percent"] == 100
    assert any(event["event"] == "saved" for event in status["events"])


def test_runner_rejects_unknown_task_kind(tmp_dir):
    runner = TaskRunner(log_dir=str(tmp_dir))
    with pytest.raises(CrawlError):
        runner.start_task("drop-database", {})


def test_runner_cities_task(tmp_dir, monkeypatch):
    fake = tmp_dir / "fake_cities.py"
    fake.write_text("print('cities updated')", encoding="utf-8")
    monkeypatch.setattr(crawl_service, "FETCH_CITIES", str(fake))

    runner = TaskRunner(log_dir=str(tmp_dir))
    task = runner.start_task("cities", {})
    assert task["kind"] == "cities"

    status = _wait(runner)
    assert status["status"] == "done"
    assert "cities updated" in "\n".join(status["lines"])
