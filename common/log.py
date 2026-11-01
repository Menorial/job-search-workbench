# -*- coding: utf-8 -*-
"""日志：控制台保持原样（只有正文，不破坏现有交互体验）+ 滚动文件落盘。

原来全程 print，长任务一旦关窗口/断线就完全无法回溯。
"""

import logging
import os
import sys
from logging.handlers import RotatingFileHandler

from common.paths import LOG_DIR, ensure_dir

_LOGGER_NAME = "boss"
_logger = None


def setup_logging(log_file=None, verbose=False):
    """初始化日志。console 只输出正文，文件里带时间戳和级别。"""
    global _logger
    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(logging.DEBUG)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(console)

    try:
        path = log_file or os.path.join(ensure_dir(LOG_DIR), "crawler.log")
        file_handler = RotatingFileHandler(path, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S"))
        logger.addHandler(file_handler)
    except Exception:
        pass  # 日志写不了也不该影响主流程

    logger.propagate = False
    _logger = logger
    return logger


def get_logger():
    if _logger is None:
        return setup_logging()
    return _logger
