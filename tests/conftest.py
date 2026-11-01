# -*- coding: utf-8 -*-
"""pytest 公共配置：路径解析 + 临时目录 fixture。

项目用的是「脚本目录即包目录」的扁平导入（from config import ...），
测试里保持同样的解析顺序即可。
"""

import os
import pathlib
import shutil
import sys
import uuid

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for path in (os.path.join(ROOT, "ai"), os.path.join(ROOT, "crawler"), ROOT):
    if path not in sys.path:
        sys.path.insert(0, path)


@pytest.fixture()
def tmp_dir():
    """项目内的临时目录。

    不用 pytest 的 tmp_path：它在部分受限环境里会用 0o700 建目录，
    导致目录不可读（Windows 上表现为 PermissionError）。
    """
    base = pathlib.Path(ROOT) / "_tmp_tests"
    base.mkdir(exist_ok=True)
    path = base / ("case-%s" % uuid.uuid4().hex[:8])
    path.mkdir()
    try:
        yield path
    finally:
        shutil.rmtree(str(path), ignore_errors=True)
