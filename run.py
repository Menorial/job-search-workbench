# -*- coding: utf-8 -*-
"""项目根目录入口：转发到 ai/run.py。

用法保持不变：
    python run.py parse -f 简历.docx
    python run.py match
    python run.py dashboard

实际逻辑在 ai/run.py（AI 求职助手一键流程）。
"""

import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# ai/ 内部的模块用扁平导入（from config import ...），
# 因此把 ai/ 也加进 sys.path，保证 `python run.py` 与 `python ai\run.py` 行为一致
AI_DIR = os.path.join(ROOT, "ai")
if AI_DIR not in sys.path:
    sys.path.insert(0, AI_DIR)

from ai.run import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
