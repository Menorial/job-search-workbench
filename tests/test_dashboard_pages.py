# -*- coding: utf-8 -*-
"""网页面板新增接口：简历上传/列表、推荐话术、设置、任务分发。"""

import json

import pytest


@pytest.fixture()
def client(tmp_dir, monkeypatch):
    import dashboard
    from fastapi.testclient import TestClient

    output = tmp_dir / "ai"
    output.mkdir()
    monkeypatch.setattr(dashboard, "RESUME_DIR", str(tmp_dir))
    monkeypatch.setattr(dashboard, "OUTPUT_DIR", str(output))
    return TestClient(dashboard.app), tmp_dir, output


def test_resume_upload_and_list(client):
    http, resume_dir, _output = client

    response = http.post("/api/resume/upload",
                         params={"name": "我的简历.pdf"}, content=b"%PDF-1.4 test")
    assert response.status_code == 200
    assert (resume_dir / "我的简历.pdf").exists()

    data = http.get("/api/resume/files").json()
    assert [item["name"] for item in data["files"]] == ["我的简历.pdf"]
    assert data["current_exists"] is False

    bad = http.post("/api/resume/upload", params={"name": "virus.exe"}, content=b"1")
    assert bad.status_code == 400


def test_resume_current_file_missing(client):
    http, resume_dir, output = client
    (output / "resume_cache_meta.json").write_text(
        json.dumps({"resume": {"path": str(resume_dir / "gone.docx")}}), encoding="utf-8")
    (resume_dir / "new.docx").write_bytes(b"x")

    data = http.get("/api/resume/files").json()
    assert data["current_exists"] is False
    assert len(data["files"]) == 1


def test_top10_endpoint(client):
    http, _resume_dir, output = client
    (output / "top10.md").write_text("# 推荐投递 TOP10\n\n## 1. Python 开发\n", encoding="utf-8")

    data = http.get("/api/top10").json()
    assert "推荐投递" in data["markdown"]
    assert "<h1>" in data["html"]
    assert data["updated_ts"] > 0


def test_settings_endpoints(client):
    http, _resume_dir, _output = client

    settings = http.get("/api/settings").json()
    assert "model" in settings
    assert "cache" in settings
    assert "usage" in settings

    assert http.post("/api/settings", json={"threshold": 999}).status_code == 400
    assert http.post("/api/cache/clear", json={"kind": "unknown"}).status_code == 400


def test_tasks_dispatch(monkeypatch):
    import dashboard
    from fastapi.testclient import TestClient

    class StubRunner(object):
        def start_task(self, kind, params):
            return {"id": "t", "kind": kind, **params}

        def status(self, since=0):
            return {"status": "idle", "seq": 0, "lines": [], "task": None,
                    "progress": {"percent": 0}, "elapsed": 0}

        def stop(self):
            return True

    monkeypatch.setattr(dashboard, "runner", StubRunner())
    http = TestClient(dashboard.app)

    response = http.post("/api/tasks/start", json={"kind": "match", "max": 20})
    assert response.status_code == 200
    assert response.json()["task"]["kind"] == "match"

    assert http.get("/api/tasks/status").json()["status"] == "idle"
    assert http.post("/api/tasks/stop").json()["stopped"] is True
