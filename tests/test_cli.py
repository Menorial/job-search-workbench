# -*- coding: utf-8 -*-
"""CLI 回归测试：run.py all 曾经因为读 args.force / args.crawl / args.detail 直接崩。"""

import pytest

import ai.run as run_module


ALL_ATTRS = (
    "file", "keyword", "city", "pages", "slow", "detail",
    "max", "concurrency", "force", "no_crawl",
)


def test_all_subcommand_declares_every_attribute_it_uses():
    namespace = run_module.build_parser().parse_args(["all", "-f", "resume.docx", "-k", "python"])
    for attr in ALL_ATTRS:
        assert hasattr(namespace, attr), "run.py all 缺少参数: %s" % attr


def test_match_subcommand_attributes():
    namespace = run_module.build_parser().parse_args(["match"])
    for attr in ("resume", "max", "concurrency", "latest", "file"):
        assert hasattr(namespace, attr)


def _silence_history(monkeypatch):
    monkeypatch.setattr(run_module, "record_history", lambda **kwargs: None)


def test_cmd_all_with_no_crawl(monkeypatch):
    calls = []
    _silence_history(monkeypatch)
    monkeypatch.setattr(run_module, "_work_parse", lambda args, emit: calls.append("parse"))
    monkeypatch.setattr(run_module, "_work_match", lambda args, emit: calls.append("match"))
    monkeypatch.setattr(run_module, "_work_report", lambda emit: calls.append("report"))
    monkeypatch.setattr(run_module, "_work_top10", lambda args, emit: calls.append("top10"))

    namespace = run_module.build_parser().parse_args(
        ["all", "-f", "resume.docx", "-k", "python", "--no-crawl"])
    assert run_module.cmd_all(namespace) == 0
    assert calls == ["parse", "match", "report", "top10"]


def test_cmd_all_runs_crawl_by_default(monkeypatch):
    calls = []
    _silence_history(monkeypatch)
    monkeypatch.setattr(run_module, "_work_parse", lambda args, emit: calls.append("parse"))
    monkeypatch.setattr(run_module, "_work_match", lambda args, emit: calls.append("match"))
    monkeypatch.setattr(run_module, "_work_report", lambda emit: calls.append("report"))
    monkeypatch.setattr(run_module, "_work_top10", lambda args, emit: calls.append("top10"))
    monkeypatch.setattr(run_module.subprocess, "call",
                        lambda command: calls.append("crawl") or 1)  # 爬虫失败

    namespace = run_module.build_parser().parse_args(["all", "-f", "r.docx", "-k", "k"])
    # 爬虫失败不应中断后续匹配
    assert run_module.cmd_all(namespace) == 0
    assert calls == ["parse", "crawl", "match", "report", "top10"]


def test_cmd_all_stops_when_parse_fails(monkeypatch):
    calls = []
    _silence_history(monkeypatch)
    monkeypatch.setattr(run_module, "_work_parse", lambda args, emit: calls.append("parse") or "解析失败")
    monkeypatch.setattr(run_module, "_work_match", lambda args, emit: calls.append("match"))
    namespace = run_module.build_parser().parse_args(["all", "-f", "r.docx", "-k", "k"])
    assert run_module.cmd_all(namespace) == 1
    assert calls == ["parse"]


def test_parse_requires_file_argument():
    with pytest.raises(SystemExit):
        run_module.build_parser().parse_args(["parse"])


def test_no_subcommand_returns_help_code():
    assert run_module.main([]) == 1
