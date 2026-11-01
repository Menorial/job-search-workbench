# -*- coding: utf-8 -*-
"""配置：集中管理所有可调参数。

API Key 放在 .env（不提交 git）。本模块负责加载并校验。
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from common.paths import AI_OUTPUT_DIR, DATA_DIR, RESULTS_DIR, RESUME_DIR, ROOT_DIR, ensure_dir

AI_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = ROOT_DIR

_ENV_FILE = os.path.join(ROOT_DIR, ".env")
try:
    from dotenv import load_dotenv

    # 显式指定路径：不管从哪个目录启动都能读到项目根的 .env
    load_dotenv(_ENV_FILE, override=False)
except ImportError:
    pass

# ============ DeepSeek API ============
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

# 计价（元 / 百万 token），只用于本地成本估算，可按官方最新价格修改
PRICE_INPUT_PER_M = float(os.getenv("DEEPSEEK_PRICE_IN", "2.0"))
PRICE_OUTPUT_PER_M = float(os.getenv("DEEPSEEK_PRICE_OUT", "8.0"))

# ============ 匹配参数 ============
MATCH_THRESHOLD = int(os.getenv("MATCH_THRESHOLD", "70"))   # 低于此分不推荐投递
MAX_CANDIDATES = int(os.getenv("MAX_CANDIDATES", "50"))     # AI 精评的候选上限

# 维度权重（会归一化，改这里就同时改 prompt 和文档，不再有两套标准）
WEIGHTS = {
    "skill": float(os.getenv("SKILL_WEIGHT", "0.30")),
    "exp": float(os.getenv("EXP_WEIGHT", "0.20")),
    "city": float(os.getenv("CITY_WEIGHT", "0.15")),
    "salary": float(os.getenv("SALARY_WEIGHT", "0.15")),
    "edu": float(os.getenv("EDU_WEIGHT", "0.10")),
    "company": float(os.getenv("COMPANY_WEIGHT", "0.10")),
}
DIM_KEYS = ("skill", "exp", "edu", "city", "salary", "company")
DIM_NAMES = {"skill": "技能", "exp": "经验", "edu": "学历", "city": "城市",
             "salary": "薪资", "company": "公司"}

# 并发调用数：DeepSeek 接口能扛住 3-5，太高容易触发限流
CONCURRENCY = int(os.getenv("LLM_CONCURRENCY", "3"))

# 缓存版本：prompt/评分标准变化时 +1，旧缓存自动失效
CACHE_VERSION = 2
PARSER_VERSION = 2

# ============ 路径 ============
OUTPUT_DIR = AI_OUTPUT_DIR
RESULTS_DIR = RESULTS_DIR
RESUME_DIR = RESUME_DIR
CACHE_FILE = os.path.join(OUTPUT_DIR, "match_cache.json")
LETTER_CACHE_FILE = os.path.join(OUTPUT_DIR, "letter_cache.json")
USAGE_FILE = os.path.join(OUTPUT_DIR, "usage.jsonl")
STRUCTURED_RESUME = os.path.join(OUTPUT_DIR, "resume_structured.json")


def normalized_weights():
    """把权重归一化到和为 1（防止用户填的权重加起来不等于 1）。"""
    total = sum(max(0.0, value) for value in WEIGHTS.values())
    if total <= 0:
        return {key: 1.0 / len(DIM_KEYS) for key in DIM_KEYS}
    return {key: max(0.0, WEIGHTS[key]) / total for key in DIM_KEYS}


def weights_text():
    """给 prompt 用的权重描述，如 '技能30% 经验20% ...'。"""
    weights = normalized_weights()
    return " ".join("%s%d%%" % (DIM_NAMES[key], round(weights[key] * 100)) for key in DIM_KEYS)


def check_config():
    """检查配置是否就绪，缺失时给出明确提示。"""
    ensure_dir(OUTPUT_DIR)
    if not DEEPSEEK_API_KEY:
        print("❌ 未配置 DeepSeek API Key")
        print("   请复制 .env.example 为 .env，填入 DEEPSEEK_API_KEY=sk-xxxx")
        print("   获取地址: https://platform.deepseek.com")
        return False
    return True
