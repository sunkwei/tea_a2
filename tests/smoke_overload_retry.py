#!/usr/bin/env python3
"""A busy provider (HTTP 529) is waited out, not given up on.

MiniMax answers a request at the top of the hour with:

    HTTP 529: 当前为整点高峰时段，服务器短暂繁忙，通常 1-5 分钟内恢复。请稍后重试 (2064)
    {"type":"error","error":{"type":"overloaded_error","message":"...","http_code":"529"}}

That is not a broken request - the same request succeeds a minute later. So the
agent has to *wait* (the provider says 1-5 minutes) and try again, instead of
failing the turn and throwing the answer away.

What this pins:
  * a 529 is classified as "overloaded", not as a fatal error;
  * the wait is minutes long by default and configurable (`overload_backoff_ms`);
  * every wait is announced to the user (a `retry` event with the countdown);
  * the retry budget is its own (`overload_retries`), and `overload_retries: 0`
    turns the waiting off;
  * the same holds when the gateway reports the failure *inside* the stream
    (HTTP 200 + an error event carrying "http_code": "529").

Usage: python3 tests/smoke_overload_retry.py [path-to-tea_agent-binary]
"""

import http.client
import json
import os
import random
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler

try:
    # Python 3.7+
    from http.server import ThreadingHTTPServer
except ImportError:  # Python 3.6
    from http.server import HTTPServer
    from socketserver import ThreadingMixIn

    class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
        """The 3.6 spelling of http.server.ThreadingHTTPServer."""
        daemon_threads = True

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The provider's own words, verbatim (2026-10-27, MiniMax at the top of the hour).
OVERLOAD_BODY = {
    "type": "error",
    "error": {
        "type": "overloaded_error",
        "message": "当前为整点高峰时段，服务器短暂繁忙，通常 1-5 分钟内恢复。请稍后重试 (2064)",
        "http_code": "529",
    },
    "request_id": "072f39a987269aa44e442b8926a15cdd",
}

ANSWER = "回答正常：等待之后拿到了答案。"

# How many times each provider path fails before it answers.
FAILS_BEFORE_ANSWER = 2

seen = {}          # provider path -> number of requests
lock = threading.Lock()


def bump(path):
    with lock:
        seen[path] = seen.get(path, 0) + 1
        return seen[path]


class Mock(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        path = self.path
        count = bump(path)

        # ---- the gateway reports the failure inside the stream -----------
        if "/streamerr" in path:
            if count <= FAILS_BEFORE_ANSWER:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                self._chunk("data: " + json.dumps(OVERLOAD_BODY) + "\n\n")
                self._chunk("")
                return
            self._answer()
            return

        # ---- the busy provider -------------------------------------------
        if "/always" in path or count <= FAILS_BEFORE_ANSWER:
            payload = json.dumps(OVERLOAD_BODY).encode()
            self.send_response(529)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        self._answer()

    def _chunk(self, text):
        data = text.encode()
        try:
            self.wfile.write(("%x\r\n" % len(data)).encode() + data + b"\r\n")
            self.wfile.flush()
        except OSError:
            pass

    def _answer(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        self._chunk("data: " + json.dumps({
            "id": "c1", "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": {"content": ANSWER},
                         "finish_reason": None}],
        }) + "\n\n")
        self._chunk("data: " + json.dumps({
            "id": "c1", "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }) + "\n\n")
        self._chunk("data: [DONE]\n\n")
        self._chunk("")


def free_port():
    while True:
        port = random.randint(20000, 60000)
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue


def call(port, method, path, body=None, timeout=30):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request(method, path, body=json.dumps(body) if body is not None else None,
                     headers={"Content-Type": "application/json"} if body is not None else {})
        resp = conn.getresponse()
        raw = resp.read()
        try:
            return resp.status, json.loads(raw or b"null")
        except ValueError:
            return resp.status, {"raw": raw.decode("utf-8", "replace")}
    finally:
        conn.close()


def turn(port, wid, provider, message="hello", timeout=120):
    """One turn; returns the event types and the interesting events."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    conn.request("POST", "/api/chat",
                 body=json.dumps({"workspace_id": wid, "provider": provider,
                                  "message": message}),
                 headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    types = []
    events = []
    while True:
        line = resp.readline()
        if not line:
            break
        line = line.strip()
        if not line.startswith(b"data: "):
            continue
        payload = line[6:]
        if payload == b"[DONE]":
            break
        try:
            ev = json.loads(payload)
        except ValueError:
            continue
        types.append(ev.get("type"))
        if ev.get("type") in ("retry", "error", "done", "content_delta"):
            events.append(ev)
    conn.close()
    return types, events


def write_settings(ws_dir, text):
    run_dir = os.path.join(ws_dir, ".tea_agent_run")
    if not os.path.isdir(run_dir):
        os.makedirs(run_dir)
    with open(os.path.join(run_dir, "settings.yaml"), "w", encoding="utf-8") as f:
        f.write(text)


def main():
    binary = (sys.argv[1] if len(sys.argv) > 1
              else os.path.join(REPO_ROOT, "build", "bin", "tea_agent"))
    if not os.path.exists(binary):
        print("binary not found:", binary)
        return 1

    mock = ThreadingHTTPServer(("127.0.0.1", 0), Mock)
    mock.daemon_threads = True
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    mock_port = mock.server_address[1]

    tmp = tempfile.mkdtemp(prefix="tea_overload_")
    ws_dir = os.path.join(tmp, "ws")
    os.makedirs(ws_dir)
    # The wait is a minute by default - far too long for a test, and the point
    # of the setting is that it can be tuned.
    write_settings(ws_dir, "settings:\n"
                         "  overload_retries: 3\n"
                         "  overload_backoff_ms: 300\n"
                         "  overload_max_backoff_ms: 2000\n")
    config_path = os.path.join(tmp, "provider.yaml")
    with open(config_path, "w", encoding="utf-8") as f:
        f.write("""version: 1
providers:
  busy:
    api_url: http://127.0.0.1:%d/busy/v1
    api_key: sk-test
    models:
      m: {max_context_tokens: 32768, max_output_tokens: 256}
  always:
    api_url: http://127.0.0.1:%d/always/v1
    api_key: sk-test
    models:
      m: {max_context_tokens: 32768, max_output_tokens: 256}
  streamerr:
    api_url: http://127.0.0.1:%d/streamerr/v1
    api_key: sk-test
    models:
      m: {max_context_tokens: 32768, max_output_tokens: 256}
""" % (mock_port, mock_port, mock_port))

    port = free_port()
    proc = subprocess.Popen([
        binary, "--config", config_path, "--tea-home", os.path.join(tmp, "home"),
        "--host", "127.0.0.1", "--port", str(port), "--no-console",
        "--log-file", os.path.join(tmp, "tea_agent.log"), "--workspace", ws_dir,
    ])

    failures = []
    checks = []

    def check(name, ok, detail=""):
        checks.append(name)
        print(("PASS " if ok else "FAIL ") + name + ((("  [" + str(detail) + "]")) if not ok else ""))
        if not ok:
            failures.append(name)

    try:
        for _ in range(100):
            try:
                if call(port, "GET", "/api/health", timeout=2)[0] == 200:
                    break
            except OSError:
                time.sleep(0.2)
        wid = call(port, "GET", "/api/workspaces")[1]["workspaces"][0]["id"]

        # ---- 1. a busy provider is waited out ----------------------------
        seen.clear()
        types, events = turn(port, wid, "busy")
        retries = [e for e in events if e.get("type") == "retry"]
        check("the turn finishes after the provider got busy",
              "done" in types, types)
        check("the request was sent again after the 529",
              seen.get("/busy/v1/chat/completions") == FAILS_BEFORE_ANSWER + 1, seen)
        check("the answer is the normal one",
              any(e.get("type") == "content_delta" and ANSWER in (e.get("text") or "")
                  for e in events), events)
        check("every wait was announced to the user",
              len(retries) == FAILS_BEFORE_ANSWER, retries)
        check("the notice says the provider was busy",
              all(r.get("kind") == "overloaded" for r in retries) and
              all(r.get("http_status") == 529 for r in retries), retries)
        check("the notice counts the retries",
              [r.get("attempt") for r in retries] == [1, 2] and
              all(r.get("max_attempts") == 3 for r in retries), retries)
        check("the wait grows (300ms, then 600ms)",
              [r.get("delay_ms") for r in retries] == [300, 600],
              [r.get("delay_ms") for r in retries])

        # ---- 2. the same when the failure arrives inside the stream -----
        seen.clear()
        types, events = turn(port, wid, "streamerr")
        retries = [e for e in events if e.get("type") == "retry"]
        check("a stream error with http_code 529 is waited out too",
              "done" in types and len(retries) == FAILS_BEFORE_ANSWER, (types, retries))
        check("the stream error is seen as a busy provider",
              all(r.get("http_status") == 529 for r in retries), retries)

        # ---- 3. when the provider stays busy, the turn fails cleanly ----
        seen.clear()
        types, events = turn(port, wid, "always")
        errors = [e for e in events if e.get("type") == "error"]
        check("the turn gives up after the retry budget",
              seen.get("/always/v1/chat/completions") == 1 + 3, seen)
        check("the failure says what the provider said",
              bool(errors) and "529" in (errors[0].get("message") or "") and
              "繁忙" in (errors[0].get("message") or ""), errors)
        check("no answer is invented", "done" not in types, types)

        # ---- 4. the waiting can be switched off -------------------------
        ws2 = os.path.join(tmp, "ws2")
        os.makedirs(ws2)
        status, entry = call(port, "POST", "/api/workspaces", {"path": ws2})
        if status != 200:
            check("the second workspace could be created", False, entry)
        else:
            write_settings(ws2, "settings:\n  overload_retries: 0\n")
            seen.clear()
            types, events = turn(port, entry["id"], "busy")
            check("overload_retries: 0 means no waiting at all",
                  seen.get("/busy/v1/chat/completions") == 1, seen)
            check("the turn fails right away",
                  any(e.get("type") == "error" for e in events), types)
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        mock.shutdown()

    print()
    print("%d/%d checks passed" % (len(checks) - len(failures), len(checks)))
    if failures:
        print("FAILED:", ", ".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
