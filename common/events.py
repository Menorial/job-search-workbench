# -*- coding: utf-8 -*-
"""进度事件流：把任务进度写成 JSONL，供网页面板轮询。

爬虫、简历解析、AI 匹配、报告、话术都用同一套事件协议：
    start / phase / progress / saved / error / done
没有传路径时所有 emit 都是空操作，CLI 行为完全不变。
"""

import json
import os
import threading
import time

from common.paths import ensure_dir


class EventEmitter(object):
    def __init__(self, path=None):
        self.path = path
        self._lock = threading.Lock()
        if path:
            ensure_dir(os.path.dirname(os.path.abspath(path)))

    def emit(self, event, **fields):
        if not self.path:
            return
        record = {"event": event, "ts": round(time.time(), 3)}
        record.update(fields)
        try:
            with self._lock, open(self.path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:
            pass  # 进度写不了不能影响主流程

    def __call__(self, event, **fields):
        """允许把 emitter 直接当回调传（on_progress=emitter）。"""
        self.emit(event, **fields)
