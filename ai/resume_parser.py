# -*- coding: utf-8 -*-
"""简历解析：PDF / Word / Markdown / 文本 -> 结构化 JSON。

流程：提取文本 -> DeepSeek 结构化 -> resume_structured.json
缓存：文件内容 hash + 解析器版本都没变时直接复用，避免重复花钱。

用法：
    python resume_parser.py -f <简历路径>
"""

import argparse
import hashlib
import json
import os
import re
import sys
import zipfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from common.console import setup_console
from common.paths import ensure_dir
from common.text import coerce_int, dedupe_lines, truncate_middle

from config import OUTPUT_DIR, PARSER_VERSION, STRUCTURED_RESUME, check_config
from llm import LLMError, chat_json, log_usage

_CACHE_META = os.path.join(OUTPUT_DIR, "resume_cache_meta.json")
MAX_RESUME_CHARS = 9000

SYSTEM_PROMPT = """你是一位专业的简历解析引擎。用户会给你一份简历的纯文本内容，请从中提取结构化信息，只输出 JSON，不要输出任何其他文字。

JSON 结构（所有字段必须存在，缺失的填空字符串/空列表）：
{
  "name": "姓名",
  "years_of_experience": 工作年限数字（如 3；应届生填 0）,
  "education": {"degree": "最高学历（本科/硕士/博士/大专等）", "school": "学校", "major": "专业"},
  "cities": ["期望工作城市，可多个"],
  "salary_expectation": {"min": 最低期望月薪数字（元）, "max": 最高期望月薪数字（元）},
  "skills": ["技能列表，如 Python、Java、Django"],
  "job_intent": ["期望岗位方向，如 Java后端开发"],
  "highlights": ["2-4 条亮点总结，如 3年电商后端开发经验"],
  "projects": [{"name": "项目名", "tech": ["技术栈"], "desc": "一句话描述"}],
  "company_pref": {"scale": "期望公司规模偏好（如 不限/大厂/中小），未知填 不限", "industry": "期望行业，未知填空", "work_style": "是否接受大小周/加班（接受/不接受/未知）"}
}

注意：
- 城市、技能、岗位方向从简历原文提取，不要编造
- 简历没有的信息填空值，不要猜测
- 工作经验年限：优先看"工作经验 X 年"，其次按时间推算，应届生为 0"""

EXAMPLE_USER = """姓名：张三
求职意向：Java后端开发工程师
工作年限：3年
教育背景：XX大学 计算机科学与技术 本科
期望城市：成都、北京
期望薪资：15000-25000
技能：Java、Spring Boot、MySQL、Redis、Docker
工作经历：3年电商平台后端开发，负责订单系统微服务化
项目：秒杀系统（Java、Redis）—— 支撑日活10W用户"""

EXAMPLE_ASSIST = (
    '{"name": "张三", "years_of_experience": 3, "education": {"degree": "本科", '
    '"school": "XX大学", "major": "计算机科学与技术"}, "cities": ["成都", "北京"], '
    '"salary_expectation": {"min": 15000, "max": 25000}, "skills": ["Java", "Spring Boot", '
    '"MySQL", "Redis", "Docker"], "job_intent": ["Java后端开发"], "highlights": '
    '["3年电商平台后端开发经验", "负责订单系统微服务化"], "projects": [{"name": "秒杀系统", '
    '"tech": ["Java", "Redis"], "desc": "支撑日活10W用户"}], "company_pref": '
    '{"scale": "不限", "industry": "", "work_style": "未知"}}'
)

# 字段默认值（模型偶尔漏字段时兜底）
_FIELD_DEFAULTS = {
    "name": "",
    "years_of_experience": 0,
    "education": {},
    "cities": [],
    "salary_expectation": {},
    "skills": [],
    "job_intent": [],
    "highlights": [],
    "projects": [],
    "company_pref": {},
}


def _extract_docx(file_path):
    try:
        from docx import Document
    except ImportError:
        print("❌ 请先安装: pip install python-docx")
        sys.exit(1)

    doc = Document(file_path)
    parts = [para.text.strip() for para in doc.paragraphs if para.text.strip()]
    for table in doc.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
            if cells:
                parts.append(" | ".join(cells))
    text = "\n".join(parts)

    # WPS/Word 的文本框内容 python-docx 读不到，直接从 XML 补提
    if len(text.strip()) < 200:
        try:
            with zipfile.ZipFile(file_path) as archive:
                xml = archive.read("word/document.xml").decode("utf-8", errors="replace")
            lines = []
            for segment in re.split(r"</w:p>", xml):
                line = "".join(re.findall(r"<w:t[^>]*>([^<]*)</w:t>", segment)).strip()
                if line:
                    lines.append(line)
            if lines:
                text = "\n".join(lines)
        except Exception as exc:
            print("    ⚠️ XML 补提失败: %s" % exc)

    # 文本框层叠会产生大量完全重复的行，统一去重
    return dedupe_lines(text)


def _extract_pdf(file_path):
    try:
        import pdfplumber
    except ImportError:
        print("❌ 请先安装: pip install pdfplumber")
        sys.exit(1)
    parts = []
    with pdfplumber.open(file_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if text:
                parts.append(text)
    return "\n".join(parts)


def extract_text(file_path):
    """按扩展名提取简历纯文本。"""
    ext = os.path.splitext(file_path)[1].lower()
    if ext == ".docx":
        return _extract_docx(file_path)
    if ext == ".doc":
        print("❌ 不支持旧版 .doc，请另存为 .docx 或导出 PDF")
        return ""
    if ext == ".pdf":
        return _extract_pdf(file_path)
    with open(file_path, "r", encoding="utf-8", errors="replace") as handle:
        return handle.read()


def file_signature(file_path):
    """简历文件签名：路径 + 大小 + 修改时间 + 内容 hash。"""
    with open(file_path, "rb") as handle:
        content_hash = hashlib.md5(handle.read()).hexdigest()
    return {
        "path": os.path.abspath(file_path),
        "mtime": os.path.getmtime(file_path),
        "size": os.path.getsize(file_path),
        "hash": content_hash,
    }


def load_cache_meta():
    if not os.path.exists(_CACHE_META):
        return {}
    try:
        with open(_CACHE_META, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return {}


def save_cache_meta(meta):
    ensure_dir(OUTPUT_DIR)
    tmp = _CACHE_META + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(meta, handle, ensure_ascii=False, indent=2)
    os.replace(tmp, _CACHE_META)


def _normalize_resume(raw):
    """保证字段齐全且类型正确，避免下游 KeyError/TypeError。"""
    data = raw if isinstance(raw, dict) else {}
    result = {}
    for key, default in _FIELD_DEFAULTS.items():
        value = data.get(key, default)
        if isinstance(default, list) and not isinstance(value, list):
            value = [] if value in (None, "") else [value]
        if isinstance(default, dict) and not isinstance(value, dict):
            value = {}
        result[key] = value if value is not None else default
    result["years_of_experience"] = coerce_int(result.get("years_of_experience"), 0, 0, 60)
    result["name"] = str(result.get("name") or "").strip()

    salary = result.get("salary_expectation") or {}
    result["salary_expectation"] = {
        "min": coerce_int(salary.get("min"), 0, 0, 1000000),
        "max": coerce_int(salary.get("max"), 0, 0, 1000000),
    }
    education = result.get("education") or {}
    result["education"] = {
        "degree": str(education.get("degree") or "").strip(),
        "school": str(education.get("school") or "").strip(),
        "major": str(education.get("major") or "").strip(),
    }
    prefs = result.get("company_pref") or {}
    result["company_pref"] = {
        "scale": str(prefs.get("scale") or "不限"),
        "industry": str(prefs.get("industry") or ""),
        "work_style": str(prefs.get("work_style") or "未知"),
    }
    return result


def parse_resume(file_path, force=False):
    """解析简历，返回结构化 dict。文件没变且非 force 时走缓存。"""
    if not os.path.exists(file_path):
        print("❌ 简历文件不存在: %s" % file_path)
        return None

    signature = file_signature(file_path)
    meta = load_cache_meta()
    cached = meta.get("resume") or {}

    if (not force and meta.get("parser_version") == PARSER_VERSION
            and cached.get("hash") == signature["hash"]
            and cached.get("path") == signature["path"]
            and os.path.exists(STRUCTURED_RESUME)):
        print("✅ 简历未变化，使用缓存: %s" % STRUCTURED_RESUME)
        with open(STRUCTURED_RESUME, "r", encoding="utf-8") as handle:
            return json.load(handle)

    print("📄 解析简历: %s" % file_path)
    text = extract_text(file_path)
    if not text or len(text.strip()) < 50:
        print("❌ 简历文本提取失败或内容过少（可能是扫描版 PDF / 纯图片简历）")
        print("   请提供 Word 版或文本版简历")
        return None

    char_count = len(text)
    text = truncate_middle(text, MAX_RESUME_CHARS)
    print("   提取文本 %d 字符%s，调用 DeepSeek 结构化..." % (
        char_count, "（超长已保留头尾）" if char_count > MAX_RESUME_CHARS else ""))

    try:
        raw = chat_json([
            {"role": "system", "content": SYSTEM_PROMPT},
            # few-shot：给一组「简历文本 -> 结构化 JSON」示例，减少字段漂移
            {"role": "user", "content": "简历文本：\n%s" % EXAMPLE_USER},
            {"role": "assistant", "content": EXAMPLE_ASSIST},
            {"role": "user", "content": "简历文本：\n%s" % text},
        ], temperature=0.1)
    except LLMError as exc:
        print("❌ 结构化失败: %s" % exc)
        return None

    result = _normalize_resume(raw)

    ensure_dir(OUTPUT_DIR)
    tmp = STRUCTURED_RESUME + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
    os.replace(tmp, STRUCTURED_RESUME)

    meta["parser_version"] = PARSER_VERSION
    meta["resume"] = signature
    save_cache_meta(meta)
    log_usage("parse")

    print("✅ 结构化完成 -> %s" % STRUCTURED_RESUME)
    return result


def main(argv=None):
    setup_console()
    parser = argparse.ArgumentParser(description="简历解析工具")
    parser.add_argument("-f", "--file", required=True, help="简历文件（.docx/.pdf/.md/.txt）")
    parser.add_argument("--force", action="store_true", help="忽略缓存强制重新解析")
    args = parser.parse_args(argv)

    if not check_config():
        return 1

    result = parse_resume(args.file, force=args.force)
    if not result:
        return 1
    print("")
    print("===== 解析结果 =====")
    print("姓名: %s" % result.get("name"))
    print("年限: %s 年" % result.get("years_of_experience"))
    print("学历: %s" % (result.get("education") or {}).get("degree"))
    print("城市: %s" % result.get("cities"))
    print("期望薪资: %s" % result.get("salary_expectation"))
    print("技能: %s" % result.get("skills"))
    print("岗位方向: %s" % result.get("job_intent"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
