# -*- coding: utf-8 -*-
"""CDP 客户端协议层测试（用假的 WebSocket，不需要真的开 Chrome）。

重点验证旧实现踩过的坑：
- 事件消息不能干扰响应匹配
- JS 抛错必须暴露出来，而不是返回 None 被当成「没数据」
- 读超时 / 连接断开要有明确报错，不能永久卡住
"""

import json

import pytest
import websocket

import common.cdp as cdp


class FakeWS(object):
    def __init__(self, messages):
        self.messages = list(messages)
        self.sent = []
        self.timeout = None
        self.closed = False

    def settimeout(self, value):
        self.timeout = value

    def send(self, payload):
        self.sent.append(json.loads(payload))

    def recv(self):
        if not self.messages:
            raise websocket.WebSocketTimeoutException("no more messages")
        item = self.messages.pop(0)
        if isinstance(item, Exception):
            raise item
        return json.dumps(item)

    def close(self):
        self.closed = True


@pytest.fixture()
def ws(monkeypatch):
    holder = {"ws": FakeWS([])}

    def fake_create_connection(url, timeout=None, enable_multithread=True):
        holder["url"] = url
        return holder["ws"]

    monkeypatch.setattr(websocket, "create_connection", fake_create_connection)
    return holder


def test_evaluate_ignores_events_and_matches_id(ws):
    ws["ws"] = FakeWS([
        {"method": "Runtime.consoleAPICalled", "params": {"type": "log"}},
        {"method": "Page.loadEventFired", "params": {}},
        {"id": 1, "result": {"result": {"type": "number", "value": 2}}},
    ])
    client = cdp.CDPClient("ws://fake")
    assert client.evaluate("1 + 1", await_promise=False) == 2
    assert ws["ws"].sent[0]["method"] == "Runtime.evaluate"
    assert ws["ws"].sent[0]["params"]["returnByValue"] is True


def test_js_exception_is_raised(ws):
    ws["ws"] = FakeWS([{
        "id": 1,
        "result": {
            "result": {"type": "object", "subtype": "error", "description": "Error: boom"},
            "exceptionDetails": {"text": "Uncaught", "exception": {"description": "Error: boom"}},
        },
    }])
    client = cdp.CDPClient("ws://fake")
    with pytest.raises(cdp.CDPError) as error:
        client.evaluate("throw new Error('boom')")
    assert "boom" in str(error.value)


def test_protocol_error_is_raised(ws):
    ws["ws"] = FakeWS([{"id": 1, "error": {"message": "Invalid parameters"}}])
    client = cdp.CDPClient("ws://fake")
    with pytest.raises(cdp.CDPError) as error:
        client.evaluate("x")
    assert "Invalid parameters" in str(error.value)


def test_read_timeout_does_not_hang(ws):
    ws["ws"] = FakeWS([websocket.WebSocketTimeoutException("timeout")])
    client = cdp.CDPClient("ws://fake")
    with pytest.raises(cdp.CDPError) as error:
        client.evaluate("x", timeout=0.2)
    assert "超时" in str(error.value)


def test_connection_closed_is_reported(ws):
    ws["ws"] = FakeWS([websocket.WebSocketConnectionClosedException("closed")])
    client = cdp.CDPClient("ws://fake")
    with pytest.raises(cdp.CDPError) as error:
        client.evaluate("x")
    assert "断开" in str(error.value)


def test_wait_for_selector_returns_bool(ws):
    ws["ws"] = FakeWS([{"id": 1, "result": {"result": {"type": "boolean", "value": True}}}])
    client = cdp.CDPClient("ws://fake")
    assert client.wait_for_selector(".job-sec-text", timeout_ms=1000) is True


def test_close_releases_socket(ws):
    ws["ws"] = FakeWS([])
    client = cdp.CDPClient("ws://fake")
    client.close()
    assert ws["ws"].closed is True


def test_empty_url_is_rejected():
    with pytest.raises(cdp.CDPError):
        cdp.CDPClient("")
