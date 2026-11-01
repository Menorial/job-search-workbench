# -*- coding: utf-8 -*-
"""设置服务测试：.env 读写、校验、缓存清理、用量汇总。"""

import json

import pytest

import config
import settings_service as svc


@pytest.fixture()
def env_file(tmp_dir, monkeypatch):
    path = tmp_dir / ".env"
    path.write_text("# 注释保留\nMATCH_THRESHOLD=70\nDEEPSEEK_MODEL=deepseek-chat\n",
                    encoding="utf-8")
    monkeypatch.setattr(svc, "ENV_FILE", str(path))
    for attr in ("MATCH_THRESHOLD", "MAX_CANDIDATES", "CONCURRENCY",
                 "DEEPSEEK_MODEL", "DEEPSEEK_BASE_URL", "DEEPSEEK_API_KEY"):
        monkeypatch.setattr(config, attr, getattr(config, attr))
    return path


def test_mask_hides_secret():
    assert svc._mask("") == ""
    assert svc._mask("short") == "***"
    masked = svc._mask("sk-1234567890abcdef")
    assert masked.startswith("sk-")
    assert masked.endswith("cdef")
    assert "1234567890" not in masked


def test_update_settings_writes_env(env_file):
    snapshot = svc.update_settings({
        "threshold": 80, "max_candidates": 30, "concurrency": 4,
    })
    text = env_file.read_text(encoding="utf-8")
    assert "MATCH_THRESHOLD=80" in text
    assert "MAX_CANDIDATES=30" in text
    assert "LLM_CONCURRENCY=4" in text
    assert "# 注释保留" in text
    assert snapshot["threshold"] == 80
    assert snapshot["max_candidates"] == 30
    assert snapshot["concurrency"] == 4
    # 内存配置也要同步，否则面板显示旧值
    assert config.MATCH_THRESHOLD == 80


def test_update_api_key_is_masked(env_file):
    snapshot = svc.update_settings({"api_key": "sk-test-abcdef123456"})
    assert snapshot["api_key_configured"] is True
    assert "sk-test-abcdef123456" not in json.dumps(snapshot)
    assert "DEEPSEEK_API_KEY=sk-test-abcdef123456" in env_file.read_text(encoding="utf-8")


def test_update_rejects_invalid_values(env_file):
    original = env_file.read_text(encoding="utf-8")
    cases = [
        {"api_key": "not-a-key"},
        {"threshold": 101},
        {"threshold": -1},
        {"max_candidates": 0},
        {"concurrency": 9},
        {"base_url": "ftp://example.com"},
        {"model": ""},
    ]
    for payload in cases:
        with pytest.raises(ValueError):
            svc.update_settings(payload)
    assert env_file.read_text(encoding="utf-8") == original


def test_clear_cache(tmp_dir, monkeypatch):
    path = tmp_dir / "match_cache.json"
    path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(config, "CACHE_FILE", str(path))
    result = svc.clear_cache("match")
    assert result["removed"] == ["match_cache.json"]
    assert not path.exists()
    with pytest.raises(ValueError):
        svc.clear_cache("unknown")


def test_usage_summary(tmp_dir, monkeypatch):
    path = tmp_dir / "usage.jsonl"
    records = [
        {"phase": "match", "calls": 2, "prompt_tokens": 1000,
         "completion_tokens": 200, "cost_cny": 0.005},
        {"phase": "report", "calls": 1, "prompt_tokens": 500,
         "completion_tokens": 100, "cost_cny": 0.002},
    ]
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    monkeypatch.setattr(config, "USAGE_FILE", str(path))

    summary = svc.usage_summary()
    assert summary["totals"]["calls"] == 3
    assert summary["totals"]["prompt_tokens"] == 1500
    assert summary["totals"]["cost"] == pytest.approx(0.007)
    assert summary["by_phase"][0]["phase"] == "match"


def test_usage_summary_without_file(tmp_dir, monkeypatch):
    monkeypatch.setattr(config, "USAGE_FILE", str(tmp_dir / "missing.jsonl"))
    summary = svc.usage_summary()
    assert summary["exists"] is False
    assert summary["totals"]["calls"] == 0
