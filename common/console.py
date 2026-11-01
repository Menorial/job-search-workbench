# -*- coding: utf-8 -*-
"""Windows 控制台编码兼容。

原项目在 10 个文件里各自复制了同一段 reconfigure 补丁，统一到这里。
"""

import sys

_NEED_FIX = ("gbk", "cp936", "cp1252", "gb2312", "big5")


def setup_console():
    """把 GBK 控制台的 stdout/stderr 切到 UTF-8，避免 emoji/中文报 UnicodeEncodeError。

    只是尽力而为：重定向到文件、IDE 捕获输出等场景失败也不影响主流程。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            enc = (getattr(stream, "encoding", "") or "").lower()
            if enc in _NEED_FIX and hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
