# -*- coding: utf-8 -*-
"""AI 求职助手 — 命令行 / 网页面板共用的流程入口。

用法：
    python run.py parse  -f resume.pdf                 # ① 解析简历
    python run.py crawl  -k java -c 成都 -p 2 --slow   # ② 爬取职位
    python run.py match  [-n 50] [--latest|--file x]   # ③ 粗筛 + AI 精评
    python run.py report                                # ④ 生成市场分析报告
    python run.py top10  [-n 10]                        # ⑤ 推荐投递 + 话术
    python run.py all    -f resume.pdf -k java -c 成都 -p 2 --slow
    python run.py dashboard [-p 8000]                   # 可视化面板

网页面板通过 --events 读取进度（JSONL 事件流），CLI 用法保持不变。
"""

import argparse
import json
import os
import subprocess
import sys
import time
import traceback

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from common.console import setup_console
from common.events import EventEmitter
from common.history import record as record_history
from common.paths import AI_OUTPUT_DIR, CRAWLER_DIR

SPIDER = os.path.join(CRAWLER_DIR, "boss_cdp_spider.py")
STRUCTURED_RESUME = os.path.join(AI_OUTPUT_DIR, "resume_structured.json")
MATCHED_JOBS = os.path.join(AI_OUTPUT_DIR, "matched_jobs.json")
TOP10_MD = os.path.join(AI_OUTPUT_DIR, "top10.md")
REPORT_MD = os.path.join(AI_OUTPUT_DIR, "report.md")


# ============ 工具 ============

def _elapsed(started):
    return round(time.time() - started, 1)


def _load_resume(path=None):
    path = path or STRUCTURED_RESUME
    if not os.path.exists(path):
        print("❌ 未找到简历结构化数据，请先运行: python run.py parse -f 简历路径")
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception as exc:
        print("❌ 简历数据读取失败: %s" % exc)
        return None


def _load_matched():
    if not os.path.exists(MATCHED_JOBS):
        print("❌ 还没有匹配结果，请先运行: python run.py match")
        return None
    try:
        with open(MATCHED_JOBS, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception as exc:
        print("❌ 匹配结果读取失败: %s" % exc)
        return None


def _load_jobs(args):
    from data_store import load_all_jobs, load_latest_file_jobs, summary

    if getattr(args, "file", None):
        if not os.path.exists(args.file):
            print("❌ 指定的职位文件不存在: %s" % args.file)
            return None
        with open(args.file, "r", encoding="utf-8") as handle:
            jobs = json.load(handle)
        if not isinstance(jobs, list):
            print("❌ 职位文件格式不正确（应为 JSON 数组）")
            return None
        print("📂 使用指定文件: %s" % args.file)
        return jobs

    if getattr(args, "latest", False):
        jobs = load_latest_file_jobs()
        print("📂 只使用最新一次爬取结果")
    else:
        jobs = load_all_jobs()
    info = summary(jobs)
    print("📊 职位 %d 个（其中带职位描述 %d 个）" % (info.get("jobs", 0), info.get("with_detail", 0)))
    return jobs


def _fail(emit, started, kind, message, **extra):
    print("❌ " + message)
    elapsed = _elapsed(started)
    emit("error", message=message, **extra)
    emit("done", status="failed", elapsed=elapsed)
    record_history(kind=kind, status="failed", error=message[:200], elapsed=elapsed, **extra)
    return 1


def _finish(emit, started, kind, **extra):
    elapsed = _elapsed(started)
    emit("done", status="ok", elapsed=elapsed, **extra)
    record_history(kind=kind, status="ok", elapsed=elapsed, **extra)
    return 0


def _run_work(work, kind):
    """执行一步工作：未预期异常转成可读错误，traceback 仍然写进任务日志。"""
    try:
        return work()
    except Exception as exc:
        traceback.print_exc()
        return "%s异常: %s" % (kind, str(exc)[:160])


# ============ 各步骤（供单命令与 all 复用） ============

def _work_parse(args, emit):
    from resume_parser import parse_resume

    emit("phase", name="parse", total=1)
    result = parse_resume(args.file, force=getattr(args, "force", False))
    if not result:
        return "简历解析失败（文件不存在、格式不支持，或 AI 调用失败）"
    emit("progress", current=1, total=1, phase="parse", label="简历结构化完成")
    emit("saved", path=STRUCTURED_RESUME)
    return None


def _work_match(args, emit):
    from matcher import match_all

    resume = _load_resume(getattr(args, "resume", None))
    if resume is None:
        return "未找到简历结构化数据，请先解析简历"
    jobs = _load_jobs(args)
    if jobs is None:
        return "职位数据读取失败"
    if not jobs:
        return "没有职位数据，请先爬取职位"

    results = match_all(
        resume,
        jobs=jobs,
        max_candidates=getattr(args, "max", None),
        concurrency=getattr(args, "concurrency", None),
        on_progress=emit,
    )
    if not results:
        return "匹配没有产生结果（检查 API Key 是否配置、日志里是否有报错）"
    return None


def _work_report(emit):
    from analyzer import generate_report

    emit("phase", name="report", total=1)
    report = generate_report()
    if not report:
        return "报告生成失败（检查 API Key 或日志）"
    emit("progress", current=1, total=1, phase="report", label="报告生成完成")
    emit("saved", path=REPORT_MD)
    return None


def _work_top10(args, emit):
    from cover_letter import generate_top10

    resume = _load_resume()
    if resume is None:
        return "未找到简历结构化数据，请先解析简历"
    matched = _load_matched()
    if matched is None:
        return "还没有匹配结果，请先运行匹配"

    out = generate_top10(resume, matched, top_n=getattr(args, "top", 10), on_progress=emit)
    if not out:
        return "推荐话术生成失败（检查 API Key 或日志）"
    return None


# ============ 子命令 ============

def cmd_parse(args):
    emit = EventEmitter(getattr(args, "events", None))
    started = time.time()
    emit("start", task="parse", file=args.file)
    error = _run_work(lambda: _work_parse(args, emit), "简历解析")
    if error:
        return _fail(emit, started, "parse", error, file=os.path.basename(args.file or ""))
    return _finish(emit, started, "parse", file=os.path.basename(args.file or ""))


def _crawl_command(args):
    command = [sys.executable, SPIDER, "-k", args.keyword, "-c", args.city,
               "-p", str(args.pages)]
    if getattr(args, "slow", False):
        command.append("--slow")
    detail = getattr(args, "detail", 0)
    if detail:
        command += ["-d", str(detail)]
    return command


def cmd_crawl(args):
    if not os.path.exists(SPIDER):
        print("❌ 找不到爬虫脚本: %s" % SPIDER)
        return 1
    command = _crawl_command(args)
    print("🕷️ " + " ".join(command))
    try:
        return subprocess.call(command)
    except KeyboardInterrupt:
        print("\n⏹️ 爬虫已被中断")
        return 130


def cmd_match(args):
    emit = EventEmitter(getattr(args, "events", None))
    started = time.time()
    emit("start", task="match", max=getattr(args, "max", None),
         concurrency=getattr(args, "concurrency", None))
    error = _run_work(lambda: _work_match(args, emit), "匹配")
    if error:
        return _fail(emit, started, "match", error)
    matched_count = 0
    try:
        with open(MATCHED_JOBS, "r", encoding="utf-8") as handle:
            matched_count = len(json.load(handle))
    except Exception:
        pass
    return _finish(emit, started, "match", jobs=matched_count)


def cmd_report(args):
    emit = EventEmitter(getattr(args, "events", None))
    started = time.time()
    emit("start", task="report")
    error = _run_work(lambda: _work_report(emit), "报告生成")
    if error:
        return _fail(emit, started, "report", error)
    return _finish(emit, started, "report")


def cmd_top10(args):
    emit = EventEmitter(getattr(args, "events", None))
    started = time.time()
    emit("start", task="top10", top=getattr(args, "top", 10))
    error = _run_work(lambda: _work_top10(args, emit), "推荐话术")
    if error:
        return _fail(emit, started, "top10", error)
    return _finish(emit, started, "top10")


def cmd_all(args):
    """一键全流程：解析 -> 爬取（可选）-> 匹配 -> 报告 -> 推荐。"""
    emit = EventEmitter(getattr(args, "events", None))
    started = time.time()
    emit("start", task="all")

    error = _run_work(lambda: _work_parse(args, emit), "简历解析")
    if error:
        return _fail(emit, started, "all", error)

    if not getattr(args, "no_crawl", False):
        emit("phase", name="crawl", total=0)
        print("🕷️ 开始爬取...")
        rc = subprocess.call(_crawl_command(args))
        if rc != 0:
            print("⚠️ 爬虫执行未成功（返回码 %s），继续用已有数据匹配..." % rc)
    else:
        print("⏭️ 已跳过爬取（--no-crawl）")

    error = _run_work(lambda: _work_match(args, emit), "匹配")
    if error:
        return _fail(emit, started, "all", error)

    if _run_work(lambda: _work_report(emit), "报告生成"):
        print("⚠️ 报告生成失败，已跳过")
    if _run_work(lambda: _work_top10(args, emit), "推荐话术"):
        print("⚠️ 话术生成失败，已跳过")

    print("")
    print("🎉 全流程完成！")
    print("   结果目录: %s" % AI_OUTPUT_DIR)
    return _finish(emit, started, "all")


def cmd_dashboard(args):
    from dashboard import run_server

    return run_server(port=args.port, open_browser=not args.no_browser)


# ============ 参数解析 ============

def _add_events(parser):
    parser.add_argument("--events", default=None, help=argparse.SUPPRESS)


def build_parser():
    parser = argparse.ArgumentParser(description="AI 求职助手")
    sub = parser.add_subparsers(dest="command")

    p_parse = sub.add_parser("parse", help="解析简历")
    p_parse.add_argument("-f", "--file", required=True, help="简历文件路径")
    p_parse.add_argument("--force", action="store_true", help="忽略缓存强制重新解析")
    _add_events(p_parse)
    p_parse.set_defaults(func=cmd_parse)

    p_crawl = sub.add_parser("crawl", help="爬取职位（包装 boss_cdp_spider.py）")
    p_crawl.add_argument("-k", "--keyword", required=True, help="搜索关键词")
    p_crawl.add_argument("-c", "--city", default="全国", help="城市")
    p_crawl.add_argument("-p", "--pages", type=int, default=2, help="页数（每页30条）")
    p_crawl.add_argument("--slow", action="store_true", help="慢速模式")
    p_crawl.add_argument("-d", "--detail", type=int, default=0, help="详情数量")
    p_crawl.set_defaults(func=cmd_crawl)

    p_match = sub.add_parser("match", help="粗筛 + AI 精评")
    p_match.add_argument("-r", "--resume", help="简历结构化 JSON（默认 ai_output/resume_structured.json）")
    p_match.add_argument("-n", "--max", type=int, default=None, help="AI 精评候选上限")
    p_match.add_argument("-j", "--concurrency", type=int, default=None, help="并发数（默认 3）")
    p_match.add_argument("--latest", action="store_true", help="只匹配最新一次爬取结果")
    p_match.add_argument("--file", help="指定职位 JSON 文件")
    _add_events(p_match)
    p_match.set_defaults(func=cmd_match)

    p_report = sub.add_parser("report", help="生成市场分析报告")
    _add_events(p_report)
    p_report.set_defaults(func=cmd_report)

    p_top10 = sub.add_parser("top10", help="生成推荐投递 + 话术")
    p_top10.add_argument("-n", "--top", type=int, default=10)
    _add_events(p_top10)
    p_top10.set_defaults(func=cmd_top10)

    p_dash = sub.add_parser("dashboard", help="启动可视化面板（FastAPI + ECharts）")
    p_dash.add_argument("-p", "--port", type=int, default=8000, help="监听端口（默认 8000）")
    p_dash.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    p_dash.set_defaults(func=cmd_dashboard)

    p_all = sub.add_parser("all", help="一键全流程")
    p_all.add_argument("-f", "--file", required=True, help="简历文件路径")
    p_all.add_argument("-k", "--keyword", required=True, help="搜索关键词")
    p_all.add_argument("-c", "--city", default="全国", help="城市")
    p_all.add_argument("-p", "--pages", type=int, default=2, help="页数")
    p_all.add_argument("--slow", action="store_true", help="慢速模式")
    p_all.add_argument("-d", "--detail", type=int, default=0, help="详情数量")
    p_all.add_argument("-n", "--max", type=int, default=None, help="AI 精评候选上限")
    p_all.add_argument("-j", "--concurrency", type=int, default=None, help="并发数（默认 3）")
    p_all.add_argument("-t", "--top", type=int, default=10, help="推荐投递条数")
    p_all.add_argument("--force", action="store_true", help="强制重新解析简历")
    p_all.add_argument("--no-crawl", action="store_true", help="跳过爬取，用已有数据")
    _add_events(p_all)
    p_all.set_defaults(func=cmd_all)

    return parser


def main(argv=None):
    setup_console()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 1
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
