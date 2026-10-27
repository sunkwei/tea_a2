#!/usr/bin/env python3
"""Interrupting a turn ends it "as much as possible" - and leaves the session usable.

The user clicks 中断 while the agent is working. What has to happen:

  * a tool that can be stopped is stopped **right away** - a shell command that
    runs for a minute must not keep the turn alive for a minute;
  * a tool that was interrupted half-way leaves the workspace in whatever state
    it reached - that cannot be undone, but it must be *said*: the tool result
    says the command was interrupted, and the next turn must not assume the
    work was done;
  * the conversation stays usable: every tool call the model made has an
    answer. A call without an answer is a request no provider accepts
    ("invalid params, 400 (2013)" on MiniMax), so an interrupt that skipped the
    answers would break the session for good.

What this pins:
  * a long `toolkit_exec` is killed when the turn is stopped (the turn ends in
    seconds, not in the command's runtime);
  * the tool result says it was interrupted, and the transcript keeps that mark;
  * every tool call of an interrupted batch gets an answer;
  * the next turn still works - the provider sees a well-formed history.

Usage: python3 tests/smoke_interrupt.py [path-to-tea_agent-binary]
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

# A command that takes a minute. `ping` is the portable one (Windows has no
# `sleep`), and it does not care about the shell.
SLEEP_CMD = "ping -n 61 127.0.0.1 > nul" if os.name == "nt" else "sleep 60"

lock = threading.Lock()
requests_seen = []      # every request body the provider got
structure_ok = []       # per request: does every tool call have an answer?


def history_is_complete(messages):
    """Every assistant tool call must have a matching tool result."""
    declared = []
    answered = set()
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        if m.get("role") == "assistant":
            for call in m.get("tool_calls") or []:
                if isinstance(call, dict) and call.get("id"):
                    declared.append(call["id"])
        elif m.get("role") == "tool":
            if m.get("tool_call_id"):
                answered.add(m["tool_call_id"])
    return all(cid in answered for cid in declared)


class MockProvider(BaseHTTPRequestHandler):
    """Asks for the tools the prompt names, then answers normally."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            body = {}
        messages = body.get("messages") or []
        with lock:
            requests_seen.append(body)
            structure_ok.append(history_is_complete(messages))

        users = [str(m.get("content") or "") for m in messages if m.get("role") == "user"]
        prompt = users[-1] if users else ""
        # The first turn of a conversation asks for the tools; the follow-up
        # (the history now has the tool results) answers.
        already_ran = any(m.get("role") == "assistant" and m.get("tool_calls")
                           for m in messages if isinstance(m, dict))

        if not already_ran:
            calls = []
            count = 3 if prompt.startswith("tools3") else 1
            for i in range(count):
                calls.append({
                    "id": "call_%d" % (i + 1),
                    "type": "function",
                    "function": {"name": "toolkit_exec",
                                 "arguments": json.dumps({"command": SLEEP_CMD})},
                })
            self._sse([
                {"id": "c1", "object": "chat.completion.chunk",
                 "choices": [{"index": 0, "delta": {"tool_calls": calls}, "finish_reason": None}]},
                {"id": "c1", "object": "chat.completion.chunk",
                 "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
            ])
            return

        self._sse([
            {"id": "c1", "object": "chat.completion.chunk",
             "choices": [{"index": 0, "delta": {"content": "已经恢复，会话可以继续。"},
                          "finish_reason": None}]},
            {"id": "c1", "object": "chat.completion.chunk",
             "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        ])

    def _sse(self, chunks):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for chunk in chunks:
            data = ("data: " + json.dumps(chunk) + "\n\n").encode()
            try:
                self.wfile.write(("%x\r\n" % len(data)).encode() + data + b"\r\n")
                self.wfile.flush()
            except OSError:
                return
        try:
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except OSError:
            pass


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


def turn(port, wid, sid, message, events, timeout=180):
    """One turn in the background; the events land in `events`."""
    def worker():
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
            conn.request("POST", "/api/chat",
                         body=json.dumps({"workspace_id": wid, "session_id": sid,
                                          "provider": "mock", "message": message}),
                         headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
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
                    events.append(json.loads(payload))
                except ValueError:
                    continue
            conn.close()
        except Exception as exc:  # noqa: BLE001
            events.append({"type": "__exception__", "error": repr(exc)})

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    return thread


def main():
    binary = (sys.argv[1] if len(sys.argv) > 1
              else os.path.join(REPO_ROOT, "build", "bin", "tea_agent"))
    if not os.path.exists(binary):
        print("binary not found:", binary)
        return 1

    mock = ThreadingHTTPServer(("127.0.0.1", 0), MockProvider)
    mock.daemon_threads = True
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    mock_port = mock.server_address[1]

    tmp = tempfile.mkdtemp(prefix="tea_interrupt_")
    ws_dir = os.path.join(tmp, "ws")
    os.makedirs(ws_dir)
    config_path = os.path.join(tmp, "provider.yaml")
    with open(config_path, "w", encoding="utf-8") as f:
        f.write("""version: 1
providers:
  mock:
    api_url: http://127.0.0.1:%d
    api_key: sk-test
    default_model: m
    models:
      m:
        max_context_tokens: 32768
        max_output_tokens: 4096
        supports_tools: true
""" % mock_port)

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

    def wait_until(fn, timeout=30.0, step=0.1):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if fn():
                return True
            time.sleep(step)
        return False

    try:
        for _ in range(100):
            try:
                if call(port, "GET", "/api/health", timeout=2)[0] == 200:
                    break
            except OSError:
                time.sleep(0.2)
        wid = call(port, "GET", "/api/workspaces")[1]["workspaces"][0]["id"]

        # ---- 1. a long command is interrupted ----------------------------
        sid = call(port, "POST", "/api/workspaces/%s/sessions" % wid, {"title": "one"})[1]["id"]
        events = []
        turn(port, wid, sid, "tools1", events)
        # Wait until the tool is really running, then stop the turn.
        started = wait_until(lambda: any(e.get("type") == "tool_start" for e in events), 30)
        check("the tool call started", started, [e.get("type") for e in events])
        time.sleep(0.5)
        stopped_at = time.time()
        call(port, "POST", "/api/chat/stop", {"session_id": sid})
        ended = wait_until(lambda: any(e.get("type") == "stopped" for e in events), 30)
        elapsed = time.time() - stopped_at
        check("the turn ended after the interrupt", ended, [e.get("type") for e in events])
        check("the turn did not wait for the command to finish", elapsed < 15, "%.1fs" % elapsed)
        results = [e for e in events if e.get("type") == "tool_result"]
        check("the tool result says it was interrupted",
              bool(results) and results[0].get("cancelled") is True and
              results[0].get("ok") is False, results)
        check("the interrupted tool says what may be left behind",
              bool(results) and "interrupt" in (results[0].get("output") or "").lower(),
              results[0].get("output") if results else "")

        # The transcript keeps the mark, and every call has an answer.
        session = call(port, "GET", "/api/sessions/" + sid)[1]
        tool_messages = [m for m in session.get("messages") or [] if m.get("role") == "tool"]
        check("the transcript keeps the interrupted tool",
              len(tool_messages) == 1 and tool_messages[0].get("cancelled") is True,
              tool_messages)

        # ---- 2. a batch of tool calls: all of them get an answer ---------
        with lock:
            requests_seen.clear()
            structure_ok.clear()
        sid2 = call(port, "POST", "/api/workspaces/%s/sessions" % wid, {"title": "three"})[1]["id"]
        events2 = []
        turn(port, wid, sid2, "tools3", events2)
        started = wait_until(lambda: any(e.get("type") == "tool_start" for e in events2), 30)
        check("the batch of tool calls started", started, [e.get("type") for e in events2])
        time.sleep(0.5)
        call(port, "POST", "/api/chat/stop", {"session_id": sid2})
        ended = wait_until(lambda: any(e.get("type") == "stopped" for e in events2), 30)
        check("the interrupted batch ended", ended, [e.get("type") for e in events2])
        results2 = [e for e in events2 if e.get("type") == "tool_result"]
        check("every call of the batch got an answer", len(results2) == 3,
              [(e.get("id"), e.get("cancelled")) for e in results2])
        session2 = call(port, "GET", "/api/sessions/" + sid2)[1]
        tool_messages2 = [m for m in session2.get("messages") or [] if m.get("role") == "tool"]
        check("the transcript has an answer for every call", len(tool_messages2) == 3,
              [m.get("tool_call_id") for m in tool_messages2])

        # ---- 3. the next turn still works --------------------------------
        events3 = []
        turn(port, wid, sid, "继续", events3)
        done = wait_until(lambda: any(e.get("type") == "done" for e in events3), 60)
        check("the next turn finishes", done, [e.get("type") for e in events3])
        text = "".join(e.get("text") or "" for e in events3 if e.get("type") == "content_delta")
        check("the next turn got its answer", "已经恢复" in text, text)
        with lock:
            last_structure = structure_ok[-1] if structure_ok else None
        check("the provider saw a complete history", last_structure is True, last_structure)
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
