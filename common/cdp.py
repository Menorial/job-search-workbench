# -*- coding: utf-8 -*-
"""极简 CDP 客户端（基于 websocket-client）。

相比原实现修正了 4 个问题：
1. 长连接复用（原来每次 evaluate 都新建/关闭 WebSocket）；
2. 设置读超时（原来 recv 可能永久阻塞）；
3. 检查 exceptionDetails（原来 JS 抛错被当成「没数据」静默吞掉）；
4. 统一处理连接断开，异常信息直接可读。
"""

import json
import time


class CDPError(RuntimeError):
    """CDP 调用失败（连接断开 / 超时 / JS 异常）。"""


def _short(text, limit=300):
    text = str(text or "").strip().replace("\n", " ")
    return text[:limit]


class CDPClient(object):
    def __init__(self, ws_url, connect_timeout=10.0, default_timeout=30.0):
        if not ws_url:
            raise CDPError("未获取到 CDP WebSocket 地址")
        self.ws_url = ws_url
        self.default_timeout = default_timeout
        self._ws = None
        self._id = 0
        self._connect(connect_timeout)

    # ---------- 连接 ----------
    def _connect(self, timeout):
        try:
            import websocket
        except ImportError:
            raise CDPError("缺少依赖 websocket-client，请先 pip install websocket-client")
        try:
            self._ws = websocket.create_connection(
                self.ws_url, timeout=timeout, enable_multithread=True
            )
        except Exception as exc:
            raise CDPError("连接 CDP 失败: %s" % exc)

    def close(self):
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:
                pass
            self._ws = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    # ---------- 底层收发 ----------
    def _recv_until(self, msg_id, timeout):
        import websocket

        deadline = time.time() + float(timeout)
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise CDPError("等待 CDP 响应超时（%.0fs），多半是页面卡住或标签页已关闭" % timeout)
            try:
                self._ws.settimeout(remaining)
                raw = self._ws.recv()
            except websocket.WebSocketTimeoutException:
                raise CDPError("等待 CDP 响应超时（%.0fs）" % timeout)
            except websocket.WebSocketConnectionClosedException:
                raise CDPError("CDP 连接已断开（页面被关闭或进程退出）")
            except Exception as exc:
                raise CDPError("读取 CDP 消息失败: %s" % exc)
            if not raw:
                raise CDPError("CDP 连接已断开")
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if msg.get("id") != msg_id:
                continue  # 事件通知或其他消息，丢弃
            if "error" in msg:
                raise CDPError("CDP 协议错误: %s" % _short(msg["error"].get("message")))
            return msg.get("result") or {}

    def call(self, method, params=None, timeout=None):
        if self._ws is None:
            raise CDPError("CDP 连接已关闭")
        self._id += 1
        msg_id = self._id
        payload = {"id": msg_id, "method": method, "params": params or {}}
        try:
            self._ws.send(json.dumps(payload))
        except Exception as exc:
            raise CDPError("发送 CDP 指令失败: %s" % exc)
        return self._recv_until(msg_id, timeout or self.default_timeout)

    # ---------- 常用封装 ----------
    def evaluate(self, expression, await_promise=True, timeout=None):
        """在页面上下文执行 JS，返回 byValue 结果。JS 抛错会抛 CDPError。"""
        params = {"expression": expression, "returnByValue": True}
        if await_promise:
            params["awaitPromise"] = True
        result = self.call("Runtime.evaluate", params, timeout)
        details = result.get("exceptionDetails")
        if details:
            desc = (details.get("exception") or {}).get("description") or details.get("text") or "JS 执行异常"
            raise CDPError(_short(desc))
        inner = result.get("result") or {}
        if inner.get("subtype") == "error":
            raise CDPError(_short(inner.get("description") or "JS error"))
        return inner.get("value")

    def navigate(self, url, wait_ready=True, timeout=20.0):
        """导航到指定 URL（用 Page.navigate，避免 location.href 造成的上下文销毁竞态）。"""
        result = self.call("Page.navigate", {"url": url}, timeout=10)
        error_text = result.get("errorText")
        if error_text:
            raise CDPError("页面导航失败: %s" % error_text)
        if wait_ready:
            return self.wait_for_js("document.readyState === 'complete'", timeout=timeout)
        return True

    def wait_for_js(self, expr, timeout=15.0, interval=0.3):
        """轮询直到 JS 表达式为真（导航过程中上下文销毁的报错会被忽略重试）。"""
        deadline = time.time() + float(timeout)
        wrapped = "(() => { try { return !!(%s); } catch (e) { return false; } })()" % expr
        while time.time() < deadline:
            try:
                if self.evaluate(wrapped, await_promise=False, timeout=5):
                    return True
            except CDPError:
                pass
            time.sleep(interval)
        return False

    def wait_for_selector(self, selector, timeout_ms=12000):
        """在页面内轮询等待元素出现（一次往返，比 Python 每次 sleep 更准更快）。"""
        js = """new Promise(resolve => {
            const sel = %s, started = Date.now();
            const tick = () => {
                if (document.querySelector(sel)) return resolve(true);
                if (Date.now() - started >= %d) return resolve(false);
                setTimeout(tick, 120);
            };
            tick();
        })""" % (json.dumps(selector), int(timeout_ms))
        try:
            return bool(self.evaluate(js, await_promise=True, timeout=timeout_ms / 1000.0 + 10))
        except CDPError:
            return False
