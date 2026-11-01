# -*- coding: utf-8 -*-
"""运行历史（爬取 / 简历解析 / 匹配 / 报告 / 话术共用一份 JSONL）。

面板的「最近运行」、任务排查、成本回顾都读这个文件。
"""

import json
import os
import threading
from datetime import datetime

from common.paths import LOG_DIR, ensure_dir

HISTORY_FILE = os.path.join(LOG_DIR, "run_history.jsonl")

_LOCK = threading.Lock()


def record(**fields):
    """追加一条运行记录；失败也不影响主流程。"""
    item = {"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    item.update(fields)
    try:
        ensure_dir(LOG_DIR)
        with _LOCK, open(HISTORY_FILE, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(item, ensure_ascii=False) + "\n")
    except Exception:
        pass


def read(limit=20):
    """读取最近的运行记录（新的在前）。"""
    if not os.path.exists(HISTORY_FILE):
        return []
    items = []
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    items.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return []
    return list(reversed(items[-max(1, int(limit)):]))
