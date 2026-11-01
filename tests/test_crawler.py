# -*- coding: utf-8 -*-
"""爬虫纯逻辑测试：字段映射 / 城市解析 / 文件名清洗 / 断点。"""

import json
import logging
import os

import boss_cdp_spider as spider


def test_map_job_maps_list_fields():
    raw = {
        "encryptJobId": "J1",
        "jobName": "Python开发",
        "brandName": "某公司",
        "salaryDesc": "15-25K",
        "cityName": "成都",
        "areaDistrict": "武侯区",
        "businessDistrict": "软件园",
        "jobExperience": "3-5年",
        "jobDegree": "本科",
        "skills": ["Python", "Django"],
        "jobLabels": ["本科", "3-5年"],
        "welfareList": ["五险一金"],
        "securityId": "sec",
        "lid": "lid",
        "bossName": "张三",
        "bossTitle": "HR",
    }
    job = spider.map_job(raw)
    assert job["job_id"] == "J1"
    assert job["salary_min"] == 15.0
    assert job["salary_max"] == 25.0
    assert job["location"] == "成都 武侯区 软件园"
    assert job["skills"] == "Python, Django"
    assert job["welfare"] == "五险一金"
    assert job["job_url"].endswith("/job_detail/J1.html")
    # 详情字段预置空值，保证不同批次列一致
    assert job["job_description"] == ""
    assert job["job_address"] == ""


def test_map_job_tolerates_missing_fields():
    job = spider.map_job({})
    assert job["job_id"] == ""
    assert job["salary_min"] == ""
    assert job["job_url"] == ""
    assert job["location"] == ""


def test_resolve_city_supports_name_code_and_suffix():
    assert spider.resolve_city("成都")[0] == "成都"
    assert spider.resolve_city("成都市")[0] == "成都"
    assert spider.resolve_city("101270100") == ("成都", "101270100")
    assert spider.resolve_city("全国") == ("全国", "0")
    name, code = spider.resolve_city("1")  # 序号兼容
    assert name and code
    assert spider.resolve_city("不存在的城市") == (None, None)


def test_suggest_cities_gives_hint():
    assert "成都" in spider.suggest_cities("成都省")


def test_classify_info_lines():
    assert spider.classify_info_lines(["成都", "3-5年", "本科"]) == ("3-5年", "本科")
    assert spider.classify_info_lines(["成都", "学历不限"]) == ("", "学历不限")


def test_classify_info_lines_cleans_dirty_values():
    # 详情页把结算方式混进学历、把整行塞进经验，都需要清洗
    assert spider.classify_info_lines(["本科完工结"]) == ("", "本科")
    assert spider.classify_info_lines(["大专 月结"]) == ("", "大专")
    assert spider.classify_info_lines(["成都 1-3年 本科"]) == ("1-3年", "本科")


def test_detail_done_requires_real_description():
    assert spider.detail_done({"ok": True, "job_description": "x" * 25}) is True
    # 旧版会把「页面打开了但描述为空」当成功缓存，这里必须判为未完成
    assert spider.detail_done({"ok": True, "job_description": ""}) is False
    assert spider.detail_done({"ok": False, "job_description": "x" * 30}) is False
    assert spider.detail_done({}) is False


def test_merge_cached_details_does_not_overwrite_fresh_fields():
    jobs = [{"job_id": "J1", "company_name": "新公司", "job_description": "",
             "job_address": "", "experience": "", "education": ""}]
    progress = {"J1": {"ok": True, "job_description": "x" * 30, "job_address": "地址",
                       "experience": "3-5年", "education": "本科", "company_name": "旧公司"}}
    assert spider.merge_cached_details(jobs, progress) == 1
    assert jobs[0]["job_description"] == "x" * 30
    assert jobs[0]["company_name"] == "新公司"  # 列表里的新值不能被旧缓存覆盖


def test_save_results_sanitizes_filename(tmp_dir):
    spider.setup_logging(log_file=str(tmp_dir / "crawler.log"))
    job = spider.map_job({"encryptJobId": "J1", "jobName": "Python"})
    path = spider.save_results([job], "C/C++:入门", "成都市", "json", str(tmp_dir))
    name = os.path.basename(path)
    for char in '\\/:*?"<>|':
        assert char not in name
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    assert data[0]["job_id"] == "J1"
    assert set(data[0].keys()) == set(spider.FIELDS)


def test_save_results_csv(tmp_dir):
    spider.setup_logging(log_file=str(tmp_dir / "crawler.log"))
    job = spider.map_job({"encryptJobId": "J1", "jobName": "Python"})
    path = spider.save_results([job], "python", "成都", "csv", str(tmp_dir))
    assert path.endswith(".csv")
    with open(path, "r", encoding="utf-8-sig") as handle:
        header = handle.readline().strip()
    assert "job_id" in header and "job_url" in header


def test_progress_jsonl_roundtrip(tmp_dir, monkeypatch):
    jsonl = tmp_dir / "_detail_progress.jsonl"
    legacy = tmp_dir / "_detail_progress.json"
    monkeypatch.setattr(spider, "PROGRESS_JSONL", str(jsonl))
    monkeypatch.setattr(spider, "PROGRESS_LEGACY", str(legacy))

    spider.append_progress({"job_id": "J1", "ok": True, "job_description": "x" * 30})
    spider.append_progress({"job_id": "J2", "ok": False, "attempts": 1})
    # 模拟上次崩溃留下的半行
    with open(jsonl, "a", encoding="utf-8") as handle:
        handle.write('{"job_id": "J3"')

    progress = spider.load_progress()
    assert progress["J1"]["ok"] is True
    assert progress["J2"]["ok"] is False
    assert "J3" not in progress


def test_load_progress_reads_legacy_json(tmp_dir, monkeypatch):
    jsonl = tmp_dir / "_detail_progress.jsonl"
    legacy = tmp_dir / "_detail_progress.json"
    legacy.write_text(json.dumps({"J1": {"job_description": "x" * 30}}), encoding="utf-8")
    monkeypatch.setattr(spider, "PROGRESS_JSONL", str(jsonl))
    monkeypatch.setattr(spider, "PROGRESS_LEGACY", str(legacy))

    progress = spider.load_progress()
    assert progress["J1"]["ok"] is True  # 旧记录自动补 ok 标记
    assert spider.detail_done(progress["J1"]) is True


# ============ 列表抓取与进度事件 ============

class FakeCDP(object):
    """假的 CDP 客户端：按顺序吐出预设的接口响应。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.expressions = []
        self.navigated = []

    def navigate(self, url, **kwargs):
        self.navigated.append(url)
        return True

    def evaluate(self, expression, **kwargs):
        self.expressions.append(expression)
        if not self.responses:
            return {"code": 0, "message": "", "jobs": []}
        return self.responses.pop(0)


def _read_events(path):
    with open(str(path), "r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def test_fetch_job_page_escapes_keyword():
    client = FakeCDP([{"code": 0, "message": "", "jobs": []}])
    keyword = 'a`b${c}&d#e"f'
    spider.fetch_job_page(client, keyword, "101270100", 2)
    expression = client.expressions[0]
    # 关键词必须以 JSON 字面量形式嵌入，并交给 encodeURIComponent 编码
    assert "encodeURIComponent" in expression
    assert json.dumps(keyword, ensure_ascii=False) in expression


def test_search_jobs_dedupes_and_emits(tmp_dir, monkeypatch):
    monkeypatch.setattr(spider.time, "sleep", lambda *args: None)

    job = {"encryptJobId": "J1", "jobName": "Python", "salaryDesc": "10-15K"}
    client = FakeCDP([
        {"code": 0, "message": "", "jobs": [dict(job) for _ in range(30)]},  # 满页但全是同一个 id
        {"code": 0, "message": "", "jobs": [dict(job, encryptJobId="J2")]},  # 不满 30 -> 最后一页
    ])
    emitter = spider.EventEmitter(str(tmp_dir / "ev.jsonl"))
    jobs = spider.search_jobs(client, "python", "101270100", 3, False,
                              logging.getLogger("test"), emit=emitter.emit)

    assert [item["job_id"] for item in jobs] == ["J1", "J2"]
    events = _read_events(tmp_dir / "ev.jsonl")
    assert events[0]["event"] == "phase" and events[0]["name"] == "search"
    assert any(event["event"] == "page" and event["page"] == 1 for event in events)
    assert events[-1]["event"] == "list_done"
    assert events[-1]["count"] == 2


def test_search_jobs_stops_on_anti_crawl(tmp_dir):
    client = FakeCDP([{"code": 37, "message": "环境异常", "jobs": []}])
    emitter = spider.EventEmitter(str(tmp_dir / "ev.jsonl"))
    jobs = spider.search_jobs(client, "python", "101270100", 2, False,
                              logging.getLogger("test"), emit=emitter.emit)
    assert jobs == []
    events = _read_events(tmp_dir / "ev.jsonl")
    error_events = [event for event in events if event["event"] == "error"]
    assert error_events and error_events[0]["code"] == 37


def test_collect_details_emits_progress(tmp_dir, monkeypatch):
    monkeypatch.setattr(spider, "PROGRESS_JSONL", str(tmp_dir / "progress.jsonl"))

    def fake_detail(client, job_id, log):
        return {
            "ok": True,
            "source": "page",
            "job_description": "x" * 40,
            "job_address": "某某大厦",
            "experience": "3-5年",
            "education": "本科",
        }

    monkeypatch.setattr(spider, "fetch_detail_page", fake_detail)

    emitter = spider.EventEmitter(str(tmp_dir / "ev.jsonl"))
    jobs = [{"job_id": "J1", "job_name": "Python开发"}]
    stats = spider.collect_details(None, jobs, 5, False, {},
                                   logging.getLogger("test"), emit=emitter.emit)

    assert stats["success"] == 1
    assert jobs[0]["job_description"] == "x" * 40

    events = _read_events(tmp_dir / "ev.jsonl")
    assert events[0]["event"] == "phase" and events[0]["name"] == "detail"
    detail_events = [event for event in events if event["event"] == "detail"]
    assert detail_events and detail_events[0]["ok"] is True
    assert detail_events[0]["index"] == 1
    assert events[-1]["event"] == "detail_done"


def test_record_history_appends_jsonl(tmp_dir, monkeypatch):
    import common.history as history

    path = tmp_dir / "run_history.jsonl"
    monkeypatch.setattr(history, "HISTORY_FILE", str(path))
    spider.record_history(status="ok", keyword="python", jobs=3)
    record = json.loads(path.read_text(encoding="utf-8").strip())
    assert record["keyword"] == "python"
    assert record["status"] == "ok"
    assert record["time"]
