# -*- coding: utf-8 -*-
"""设置 / 缓存 / 用量：网页面板「设置」页需要的数据与服务。

- 读取当前配置（Key 只回传掩码）
- 把表单改动写回 .env（保留原有注释和其他行）
- 清理匹配缓存 / 话术缓存 / 详情断点
- 汇总 usage.jsonl 的调用次数、token 与估算成本
"""

import json
import os
import re

import config
from common.paths import AI_OUTPUT_DIR, LOG_DIR, RESULTS_DIR, RESUME_DIR, ROOT_DIR
from common.text import coerce_int

ENV_FILE = os.path.join(ROOT_DIR, ".env")

# 前端字段 -> .env 变量
_FIELD_MAP = (
    ("api_key", "DEEPSEEK_API_KEY", None),
    ("model", "DEEPSEEK_MODEL", None),
    ("base_url", "DEEPSEEK_BASE_URL", None),
    ("threshold", "MATCH_THRESHOLD", (0, 100)),
    ("max_candidates", "MAX_CANDIDATES", (1, 500)),
    ("concurrency", "LLM_CONCURRENCY", (1, 8)),
)

_CONFIG_ATTR = {
    "DEEPSEEK_API_KEY": "DEEPSEEK_API_KEY",
    "DEEPSEEK_MODEL": "DEEPSEEK_MODEL",
    "DEEPSEEK_BASE_URL": "DEEPSEEK_BASE_URL",
    "MATCH_THRESHOLD": "MATCH_THRESHOLD",
    "MAX_CANDIDATES": "MAX_CANDIDATES",
    "LLM_CONCURRENCY": "CONCURRENCY",
}


def _mask(secret):
    secret = str(secret or "")
    if not secret:
        return ""
    if len(secret) <= 8:
        return "***"
    return secret[:3] + "***" + secret[-4:]


def _count_json_entries(path, key="entries"):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        return 0
    if isinstance(data, dict):
        data = data.get(key) if key else data
    try:
        return len(data)
    except TypeError:
        return 0


def _count_lines(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return sum(1 for line in handle if line.strip())
    except OSError:
        return 0


def usage_summary():
    """汇总 data/ai_output/usage.jsonl。"""
    totals = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "cost": 0.0}
    phases = {}
    path = config.USAGE_FILE

    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    for key in ("calls", "prompt_tokens", "completion_tokens"):
                        totals[key] += float(record.get(key) or 0)
                    totals["cost"] += float(record.get("cost_cny") or 0)
                    name = record.get("phase") or "其他"
                    bucket = phases.setdefault(
                        name, {"phase": name, "calls": 0, "cost": 0.0, "prompt_tokens": 0})
                    bucket["calls"] += int(record.get("calls") or 0)
                    bucket["prompt_tokens"] += int(record.get("prompt_tokens") or 0)
                    bucket["cost"] += float(record.get("cost_cny") or 0)
        except OSError:
            pass

    totals["calls"] = int(totals["calls"])
    totals["prompt_tokens"] = int(totals["prompt_tokens"])
    totals["completion_tokens"] = int(totals["completion_tokens"])
    totals["cost"] = round(totals["cost"], 3)

    phase_list = sorted(phases.values(), key=lambda item: -item["cost"])
    for item in phase_list:
        item["cost"] = round(item["cost"], 3)

    return {
        "totals": totals,
        "by_phase": phase_list,
        "file": path,
        "exists": os.path.exists(path),
    }


def settings_snapshot():
    cache = {
        "match_entries": _count_json_entries(config.CACHE_FILE),
        "letter_entries": _count_json_entries(config.LETTER_CACHE_FILE),
        "detail_progress": _count_lines(os.path.join(RESULTS_DIR, "_detail_progress.jsonl")),
    }
    return {
        "api_key_configured": bool(config.DEEPSEEK_API_KEY),
        "api_key_masked": _mask(config.DEEPSEEK_API_KEY),
        "base_url": config.DEEPSEEK_BASE_URL,
        "model": config.DEEPSEEK_MODEL,
        "threshold": config.MATCH_THRESHOLD,
        "max_candidates": config.MAX_CANDIDATES,
        "concurrency": config.CONCURRENCY,
        "weights": {key: round(value, 3) for key, value in config.normalized_weights().items()},
        "prices": {"input": config.PRICE_INPUT_PER_M, "output": config.PRICE_OUTPUT_PER_M},
        "paths": {
            "root": ROOT_DIR,
            "results": RESULTS_DIR,
            "ai_output": AI_OUTPUT_DIR,
            "resume": RESUME_DIR,
            "logs": LOG_DIR,
        },
        "cache": cache,
        "usage": usage_summary(),
        "env_file": ENV_FILE,
    }


def _write_env(updates):
    """按行更新 .env：已有的键替换，没有的追加；注释和顺序保持不变。"""
    lines = []
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE, "r", encoding="utf-8") as handle:
            lines = handle.read().splitlines()

    index = {}
    for position, line in enumerate(lines):
        match = re.match(r"\s*([A-Za-z0-9_]+)\s*=", line)
        if match:
            index[match.group(1)] = position

    for key, value in updates.items():
        entry = "%s=%s" % (key, value)
        if key in index:
            lines[index[key]] = entry
        else:
            lines.append(entry)

    text = "\n".join(lines).rstrip() + "\n"
    tmp = ENV_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.replace(tmp, ENV_FILE)


def _apply_to_config(updates):
    for env_key, attr in _CONFIG_ATTR.items():
        if env_key in updates:
            setattr(config, attr, updates[env_key])
    os.environ.update({key: str(value) for key, value in updates.items()})


def update_settings(payload):
    """校验并保存设置；非法值抛 ValueError。"""
    payload = payload or {}
    updates = {}

    if "api_key" in payload:
        key = str(payload.get("api_key") or "").strip()
        if key and not key.startswith("sk-"):
            raise ValueError("API Key 应以 sk- 开头")
        if len(key) > 300:
            raise ValueError("API Key 过长")
        updates["DEEPSEEK_API_KEY"] = key

    if "model" in payload:
        model = str(payload.get("model") or "").strip()
        if not model:
            raise ValueError("模型名不能为空")
        if len(model) > 60:
            raise ValueError("模型名过长")
        updates["DEEPSEEK_MODEL"] = model

    if "base_url" in payload:
        base = str(payload.get("base_url") or "").strip().rstrip("/")
        if not base.startswith("http"):
            raise ValueError("Base URL 需以 http:// 或 https:// 开头")
        updates["DEEPSEEK_BASE_URL"] = base

    for field, env_key, limits in _FIELD_MAP:
        if limits is None or field not in payload:
            continue
        value = coerce_int(payload.get(field), -1)
        low, high = limits
        if not low <= value <= high:
            raise ValueError("%s 需在 %d-%d 之间" % (field, low, high))
        updates[env_key] = value

    if not updates:
        return settings_snapshot()

    _write_env(updates)
    _apply_to_config(updates)
    return settings_snapshot()


def clear_cache(kind):
    """清理缓存文件，返回删掉了什么。"""
    kind = str(kind or "").lower()
    if kind == "match":
        targets = [config.CACHE_FILE]
    elif kind == "letter":
        targets = [config.LETTER_CACHE_FILE]
    elif kind == "progress":
        targets = [
            os.path.join(RESULTS_DIR, "_detail_progress.jsonl"),
            os.path.join(RESULTS_DIR, "_detail_progress.json"),
        ]
    else:
        raise ValueError("未知缓存类型: %s" % kind)

    removed = []
    for path in targets:
        if os.path.exists(path):
            try:
                os.remove(path)
                removed.append(os.path.basename(path))
            except OSError:
                pass
    return {"kind": kind, "removed": removed}
