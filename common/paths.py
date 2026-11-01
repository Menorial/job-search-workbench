# -*- coding: utf-8 -*-
"""项目路径统一管理（原项目在 crawler/ 与 ai/ 里各算一份，容易漂移）。"""

import os

# common/ 的上级目录 = 项目根目录
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DATA_DIR = os.path.join(ROOT_DIR, "data")
RESULTS_DIR = os.path.join(DATA_DIR, "boss_results")      # 爬虫结果
AI_OUTPUT_DIR = os.path.join(DATA_DIR, "ai_output")       # AI 模块输出
RESUME_DIR = os.path.join(DATA_DIR, "resume")             # 简历存放
LOG_DIR = os.path.join(DATA_DIR, "logs")                  # 运行日志
CHROME_PROFILE_DIR = os.path.join(DATA_DIR, "boss_chrome_profile")  # Chrome 登录态

CRAWLER_DIR = os.path.join(ROOT_DIR, "crawler")
AI_DIR = os.path.join(ROOT_DIR, "ai")


def ensure_dir(path):
    """确保目录存在，返回该目录（方便链式使用）。"""
    if path and not os.path.isdir(path):
        os.makedirs(path, exist_ok=True)
    return path
