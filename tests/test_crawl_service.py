# -*- coding: utf-8 -*-
"""网页爬取服务测试：参数校验 / 命令行构建 / 进度推导 / 子进程生命周期。"""

import json
import time

import pytest

import crawl_service
from crawl_service import (
    CrawlError,
    CrawlRunner,
    build_command,
    derive_progress,
    read_history,
    resolve_result_file,
    validate_params,
)


# ============ 参数校验 ============

def test_validate_params_normalizes():
    params = validate_params({
        "keyword": "  python  ", "city": "成都市", "pages": "2",
        "detail": "3", "slow": True, "format": "CSV",
    })
    assert params == {
        "keyword": "python", "city": "成都", "pages": 2,
        "detail": 3, "format": "csv", "slow": True,
    }


@pytest.mark.parametrize("payload,fragment", [
    ({"keyword": ""}, "关键词"),
    ({"keyword": "x" * 60}, "过长"),
    ({"keyword": "python", "city": "火星基地"}, "未知城市"),
    ({"keyword": "python", "pages": 0}, "页数"),
    ({"keyword": "python", "pages": 99}, "页数"),
    ({"keyword": "python", "pages": "abc"}, "页数"),
    ({"keyword": "python", "detail": -1}, "详情"),
    ({"keyword": "python", "format": "xlsx"}, "格式"),
])
def test_validate_params_invalid(payload, fragment):
    with pytest.raises(CrawlError) as error:
        validate_params(payload)
    assert fragment in str(error.value)


def test_validate_params_defaults():
    params = validate_params({"keyword": "python"})
    assert params["city"] == "全国"
    assert params["pages"] == 3
    assert params["detail"] == 0
    assert params["format"] == "json"
    assert params["slow"] is False


# ============ 命令行 ============

def test_build_command_passes_keyword_as_single_argv(tmp_path):
    params = validate_params({"keyword": "AI&Python; rm -rf /", "city": "成都",
                              "pages": 2, "detail": 1, "slow": True})
    command = build_command(params, str(tmp_path / "e.jsonl"))
    assert isinstance(command, list)
    assert "AI&Python; rm -rf /" in command      # 原样传递，不经过 shell
    assert "--events" in command
    assert "-d" in command and "1" in command
    assert "--slow" in command


def test_build_command_without_detail_or_slow(tmp_path):
    params = validate_params({"keyword": "python", "pages": 1})
    command = build_command(params, None)
    assert "-d" not in command
    assert "--slow" not in command
    assert "--events" not in command


# ============ 进度推导 ============

def test_progress_search_page():
    events = [
        {"event": "start", "pages": 3, "detail": 0},
        {"event": "phase", "name": "search", "total": 3},
        {"event": "page", "page": 2, "pages": 3, "total": 60},
    ]
    progress = derive_progress(events, {"pages": 3, "detail": 0})
    assert progress["phase"] == "search"
    assert 60 <= progress["percent"] <= 70
    assert "2/3" in progress["text"]


def test_progress_detail_then_done():
    events = [
        {"event": "start", "pages": 1, "detail": 4},
        {"event": "list_done", "count": 30, "pages": 1},
        {"event": "phase", "name": "detail", "total": 4},
        {"event": "detail", "index": 2, "total": 4, "ok": True},
    ]
    progress = derive_progress(events, {"pages": 1, "detail": 4})
    assert progress["phase"] == "detail"
    assert 70 <= progress["percent"] <= 80

    events += [{"event": "saved", "jobs": 30}, {"event": "done", "status": "ok"}]
    progress = derive_progress(events, {"pages": 1, "detail": 4})
    assert progress["percent"] == 100


def test_progress_error_is_visible():
    progress = derive_progress([{"event": "error", "message": "未检测到 Chrome"}], {})
    assert "Chrome" in progress["text"]
    assert progress["percent"] == 0


def test_progress_setup_task_hint():
    progress = derive_progress([], {"kind": "setup"})
    assert "Chrome" in progress["text"]


# ============ 子进程生命周期 ============

FAKE_SPIDER = r'''
# -*- coding: utf-8 -*-
import json, sys, time

args = sys.argv[1:]
events = args[args.index("--events") + 1] if "--events" in args else None

def emit(**record):
    if events:
        with open(events, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

emit(event="start", pages=1, detail=0)
print("🔍 hello from fake spider", flush=True)
emit(event="page", page=1, pages=1, total=1)
time.sleep(0.2)
print("📊 done", flush=True)
emit(event="saved", jobs=1)
emit(event="done", status="ok")
'''

SLEEP_SPIDER = "import time\ntime.sleep(5)\n"


def _make_spider(tmp_dir, body):
    path = tmp_dir / "fake_spider.py"
    path.write_text(body, encoding="utf-8")
    return str(path)


def _wait(runner, timeout=10):
    status = {}
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = runner.status()
        if status["status"] != "running":
            return status
        time.sleep(0.05)
    return status


def test_runner_status_before_start(tmp_dir):
    runner = CrawlRunner(log_dir=str(tmp_dir))
    status = runner.status()
    assert status["status"] == "idle"
    assert status["progress"]["text"] == "等待开始"
    assert status["task"] is None
    assert runner.stop() is False


def test_runner_success(tmp_dir):
    runner = CrawlRunner(spider_path=_make_spider(tmp_dir, FAKE_SPIDER), log_dir=str(tmp_dir))
    task = runner.start({"keyword": "python", "city": "成都", "pages": 1})
    assert task["keyword"] == "python"
    assert task["elapsed"] >= 0

    status = _wait(runner)
    assert status["status"] == "done", status
    assert status["returncode"] == 0
    text = "\n".join(status["lines"])
    assert "hello from fake spider" in text
    assert "done" in text
    assert status["progress"]["percent"] == 100
    assert status["events"][-1]["event"] == "done"


def test_runner_rejects_concurrent_task(tmp_dir):
    runner = CrawlRunner(spider_path=_make_spider(tmp_dir, SLEEP_SPIDER), log_dir=str(tmp_dir))
    runner.start({"keyword": "a", "city": "全国", "pages": 1})
    try:
        with pytest.raises(CrawlError):
            runner.start({"keyword": "b", "city": "全国", "pages": 1})
    finally:
        runner.stop()


def test_runner_stop_marks_cancelled(tmp_dir):
    runner = CrawlRunner(spider_path=_make_spider(tmp_dir, SLEEP_SPIDER), log_dir=str(tmp_dir))
    runner.start({"keyword": "a", "city": "全国", "pages": 1})
    time.sleep(0.2)
    assert runner.stop() is True
    status = _wait(runner)
    assert status["status"] == "cancelled"
    assert runner.stop() is False   # 已经不在运行


def test_runner_status_lines_incremental(tmp_dir):
    runner = CrawlRunner(spider_path=_make_spider(tmp_dir, FAKE_SPIDER), log_dir=str(tmp_dir))
    runner.start({"keyword": "a", "city": "全国", "pages": 1})
    status = _wait(runner)
    first = status["seq"]
    assert first >= 2
    # since 取当前最大序号后不应再返回旧日志
    assert runner.status(since=first)["lines"] == []


def test_runner_failure_exit_code(tmp_dir):
    runner = CrawlRunner(spider_path=_make_spider(tmp_dir, "import sys\nsys.exit(3)\n"),
                         log_dir=str(tmp_dir))
    runner.start({"keyword": "a", "city": "全国", "pages": 1})
    status = _wait(runner)
    assert status["status"] == "failed"
    assert status["returncode"] == 3


# ============ 历史与结果文件 ============

def test_read_history_newest_first(tmp_dir, monkeypatch):
    monkeypatch.setattr(crawl_service, "LOG_DIR", str(tmp_dir))
    path = tmp_dir / "run_history.jsonl"
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(json.dumps({"time": "2026-01-01 10:00:00", "keyword": "a"}, ensure_ascii=False) + "\n")
        handle.write(json.dumps({"time": "2026-01-01 11:00:00", "keyword": "b"}, ensure_ascii=False) + "\n")
    items = read_history(5)
    assert [item["keyword"] for item in items] == ["b", "a"]


def test_read_history_missing_file(tmp_dir, monkeypatch):
    monkeypatch.setattr(crawl_service, "LOG_DIR", str(tmp_dir))
    assert read_history() == []


def test_resolve_result_file_blocks_traversal(tmp_dir, monkeypatch):
    monkeypatch.setattr(crawl_service, "RESULTS_DIR", str(tmp_dir))
    (tmp_dir / "boss_x.json").write_text("[]", encoding="utf-8")
    assert resolve_result_file("boss_x.json").endswith("boss_x.json")
    # basename 化之后仍然只能取结果目录里的文件
    assert resolve_result_file("../../boss_x.json").endswith("boss_x.json")
    with pytest.raises(CrawlError):
        resolve_result_file("not_exists.json")
    with pytest.raises(CrawlError):
        resolve_result_file("cookies.txt")


# ============ HTTP 接口 ============

def test_crawl_endpoints(monkeypatch):
    import dashboard
    from fastapi.testclient import TestClient

    class StubRunner(object):
        def start(self, payload):
            return {"id": "t", **payload}

        def start_setup(self, wait=90):
            return {"id": "s", "login_wait": wait}

        def stop(self):
            return True

        def status(self, since=0):
            return {"status": "idle", "seq": 0, "lines": [], "task": None,
                    "progress": {"percent": 0}, "elapsed": 0}

        def env(self):
            return {"cdp_alive": False, "chrome_found": True, "cdp_port": 9222}

    monkeypatch.setattr(dashboard, "runner", StubRunner())
    client = TestClient(dashboard.app)

    assert client.get("/api/crawl/status").json()["status"] == "idle"

    response = client.post("/api/crawl/start",
                           json={"keyword": "python", "city": "成都", "pages": 1})
    assert response.status_code == 200
    assert response.json()["task"]["keyword"] == "python"

    assert client.post("/api/crawl/stop").json()["stopped"] is True
    assert client.post("/api/crawl/setup", json={"login_wait": 60}).status_code == 200

    cities = client.get("/api/cities").json()
    assert "全国" in cities["all"]
    assert "成都" in cities["popular"]

    env = client.get("/api/crawl/env").json()
    assert env["cdp_port"] == 9222
