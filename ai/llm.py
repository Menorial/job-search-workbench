# -*- coding: utf-8 -*-
"""DeepSeek LLM 调用封装。

修正点：
- 复用同一个 OpenAI client（原来每次调用都新建，重复 TLS 握手）
- 重试不再在最后一次失败后还多睡几秒
- 记录 token 用量并估算成本（原来完全不知道花了多少钱）
- JSON 解析强制校验为 dict，避免返回 list 时下游崩
"""

import json
import os
import random
import threading
import time

from config import (
    DEEPSEEK_API_KEY,
    DEEPSEEK_BASE_URL,
    DEEPSEEK_MODEL,
    PRICE_INPUT_PER_M,
    PRICE_OUTPUT_PER_M,
    USAGE_FILE,
)
from common.text import coerce_int

_LOCK = threading.Lock()
_CLIENT = None
_USAGE = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "cost": 0.0}


class LLMError(Exception):
    pass


def _get_client(timeout=60):
    """懒加载单例 client（多线程共享，OpenAI SDK 线程安全）。"""
    global _CLIENT
    if _CLIENT is None:
        if not DEEPSEEK_API_KEY:
            raise LLMError("未配置 DEEPSEEK_API_KEY，请检查 .env 文件")
        try:
            from openai import OpenAI
        except ImportError:
            raise LLMError("缺少依赖 openai，请 pip install -r requirements.txt")
        with _LOCK:
            if _CLIENT is None:
                _CLIENT = OpenAI(
                    api_key=DEEPSEEK_API_KEY,
                    base_url=DEEPSEEK_BASE_URL,
                    timeout=timeout,
                    max_retries=0,  # 重试由本模块统一控制
                )
    return _CLIENT


def _record_usage(response):
    usage = getattr(response, "usage", None)
    prompt_tokens = coerce_int(getattr(usage, "prompt_tokens", 0))
    completion_tokens = coerce_int(getattr(usage, "completion_tokens", 0))
    cost = (prompt_tokens * PRICE_INPUT_PER_M + completion_tokens * PRICE_OUTPUT_PER_M) / 1e6
    with _LOCK:
        _USAGE["calls"] += 1
        _USAGE["prompt_tokens"] += prompt_tokens
        _USAGE["completion_tokens"] += completion_tokens
        _USAGE["cost"] += cost
    return prompt_tokens, completion_tokens


def usage_snapshot(reset=False):
    """当前累计用量（调用次数 / token / 估算成本）。"""
    with _LOCK:
        data = dict(_USAGE)
        if reset:
            _USAGE.update({"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "cost": 0.0})
    return data


def log_usage(phase, extra=None):
    """把本次用量追加到 ai_output/usage.jsonl，便于长期统计成本。"""
    usage = usage_snapshot()
    if not usage["calls"]:
        return
    record = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "phase": phase,
              "model": DEEPSEEK_MODEL, "calls": usage["calls"],
              "prompt_tokens": usage["prompt_tokens"],
              "completion_tokens": usage["completion_tokens"],
              "cost_cny": round(usage["cost"], 4)}
    if extra:
        record.update(extra)
    try:
        os.makedirs(os.path.dirname(USAGE_FILE), exist_ok=True)
        with open(USAGE_FILE, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


def chat(messages, temperature=0.2, max_tokens=2048, json_mode=False,
         max_retries=3, timeout=90):
    """调用 DeepSeek，返回文本内容。失败按指数退避重试。"""
    if not DEEPSEEK_API_KEY:
        raise LLMError("未配置 DEEPSEEK_API_KEY，请检查 .env 文件")

    client = _get_client(timeout=timeout)
    kwargs = {
        "model": DEEPSEEK_MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}

    last_error = None
    for attempt in range(max(1, max_retries)):
        try:
            response = client.chat.completions.create(**kwargs)
            _record_usage(response)
            return (response.choices[0].message.content or "")
        except Exception as exc:
            last_error = exc
            if attempt >= max(1, max_retries) - 1:
                break
            wait = min(30.0, (2 ** attempt) + random.uniform(0, 1.0))
            print("⚠️ LLM 调用失败（第 %d 次）: %s，%.1fs 后重试..." % (
                attempt + 1, str(exc)[:100], wait))
            time.sleep(wait)

    raise LLMError("LLM 调用连续失败: %s" % str(last_error)[:200])


def chat_json(messages, temperature=0.2, max_retries=3, timeout=90):
    """要求返回 JSON 对象，自动解析并校验为 dict。"""
    text = chat(messages, temperature=temperature, max_tokens=4096,
                json_mode=True, max_retries=max_retries, timeout=timeout)
    return parse_json(text, expect="object")


def parse_json(text, expect=None):
    """尽力从模型输出中解析 JSON（容忍 ```json 包裹、前后杂讯）。"""
    if not text:
        raise LLMError("模型返回为空")
    cleaned = text.strip().lstrip("\ufeff")
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()

    data = None
    try:
        data = json.loads(cleaned)
    except ValueError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start != -1 and end > start:
            try:
                data = json.loads(cleaned[start:end + 1])
            except ValueError:
                data = None
        if data is None:
            start = cleaned.find("[")
            end = cleaned.rfind("]")
            if start != -1 and end > start:
                try:
                    data = json.loads(cleaned[start:end + 1])
                except ValueError:
                    data = None

    if data is None:
        raise LLMError("无法解析模型 JSON 输出: %s" % cleaned[:200])
    if expect == "object" and not isinstance(data, dict):
        raise LLMError("模型返回的不是 JSON 对象: %s" % cleaned[:200])
    return data
