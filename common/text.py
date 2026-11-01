# -*- coding: utf-8 -*-
"""文本处理小工具：文件名清洗、去重、截断、数值容错。"""

import re

# Windows 文件名非法字符 + 控制字符
_ILLEGAL_FILENAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL"}
_RESERVED_NAMES |= {"COM%d" % i for i in range(1, 10)}
_RESERVED_NAMES |= {"LPT%d" % i for i in range(1, 10)}
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")


def sanitize_filename(name, maxlen=80, fallback="untitled"):
    """把任意字符串变成合法的 Windows 文件名片段。

    例：'C/C++' -> 'C_C++'；'a:b*c?' -> 'a_b_c_'；超长截断。
    """
    s = _ILLEGAL_FILENAME.sub("_", str(name or ""))
    s = re.sub(r"\s+", " ", s).strip(" .")
    if s and s.upper().split(".")[0] in _RESERVED_NAMES:
        s = "_" + s
    s = s[:maxlen].strip(" .")
    return s or fallback


def dedupe_lines(text, keep_blank=False):
    """按出现顺序去重（文本框层叠会产出完全重复的行）。

    keep_blank=True 时保留空行（用于保持 Markdown 结构）。
    """
    seen = set()
    out = []
    for line in (text or "").splitlines():
        key = line.strip()
        if not key:
            if keep_blank:
                out.append("")
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append(key if key == line else line)
    return "\n".join(out)


def truncate_middle(text, limit, tail_ratio=0.35):
    """超长文本保留头尾（简历的「项目经历」常在末尾，不能直接截尾）。"""
    text = text or ""
    if limit <= 0 or len(text) <= limit:
        return text
    tail_len = int(limit * tail_ratio)
    head_len = max(0, limit - tail_len)
    omitted = len(text) - head_len - tail_len
    return "%s\n……（中间省略 %d 字符）……\n%s" % (text[:head_len], omitted, text[-tail_len:])


def coerce_int(value, default=0, lo=None, hi=None):
    """把 '85' / '85分' / 85.6 / None 统一转成 int，并夹在 [lo, hi] 内。"""
    n = default
    if isinstance(value, bool):
        n = int(value)
    elif isinstance(value, (int, float)):
        try:
            n = int(value)
        except (ValueError, OverflowError):
            n = default
    elif value is not None:
        m = _NUMBER.search(str(value))
        if m:
            try:
                n = int(float(m.group(0)))
            except ValueError:
                n = default
    if lo is not None:
        n = max(lo, n)
    if hi is not None:
        n = min(hi, n)
    return n


def first_line(text):
    """取第一行非空文本（地址等场景常用）。"""
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            return line
    return ""


_LIST_ITEM = re.compile(r"^\s*[-*]\s+")
_BOLD_LINE = re.compile(r"^\s*\*\*[^*]+\*\*\s*[:：]?\s*$")


def normalize_markdown_lists(text):
    """修补旧版生成的 Markdown 结构。

    旧版 top10.md 有两类问题：
    1. `**打招呼话术:**` 和 `- 简短版:` 之间没有空行 -> 列表变成普通段落；
    2. `**风险点:**` 紧跟在列表项后面 -> 被并进上一条列表项。
    这里补空行，让渲染结果和结构一致（不改变文字内容）。
    """
    out = []
    for line in (text or "").splitlines():
        is_item = bool(_LIST_ITEM.match(line))
        is_bold = bool(_BOLD_LINE.match(line))
        prev = out[-1] if out else ""
        prev_is_item = bool(_LIST_ITEM.match(prev))
        prev_blank = not prev.strip()
        if out and not prev_blank:
            if (is_item and not prev_is_item) or (is_bold and prev_is_item):
                out.append("")
        out.append(line)
    return "\n".join(out)
