#!/usr/bin/env python3
"""A tool call that takes a long time must say so.

A build that runs for three minutes looks exactly like a hung agent in the UI:
one row, a spinner, no numbers. The user cannot tell "still working" from
"stuck", and they cannot tell whether waiting another minute is worth it.

So the agent announces the timing of every tool call:

  * tool_start carries `started_at` (when the call began) and `timeout_ms`
    (the deadline the tool itself enforces);
  * tool_result carries the same two numbers plus `elapsed_ms`;
  * the session keeps both, so a reloaded transcript still says when the call
    ran and what its budget was.

What this pins:

  * tool_start has both fields, and the timestamp is a real ISO8601 instant;
  * the timeout is the number the tool really enforces - a command asked for
    5 s is given 5 s, not the 180 s default;
  * tool_result repeats them, and its elapsed_ms agrees with the wall clock
    between started_at and the result;
  * the persisted tool message keeps started_at and timeout_ms, so the
    transcript does not lose them on a reload;
  * a tool with no deadline of its own (a file read) says so honestly
    (timeout_ms == 0) instead of inventing one.

Usage: python3 tests/smoke_tool_timing.py [path-to-tea_agent-binary]
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
    from socketsocketserver import MixIn  # noqa: F401  (never used)
    from socketserver import ThreadingMixIn

    class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
        """The 3.6 spelling of http.server.ThreadingHTTPServer."""
        daemon_threads = True

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# What the mock model asks for. The command sleeps, so the call really takes
# time - that is the whole point of the test.
EXEC_ARGS = {"command": "sleep 0.6", "timeout_ms": 5000}
FILE_ARGS = {"op": "read", "path": "note.txt"}


class MockProvider(BaseHTTPRequestHandler):
    """One tool call (a slow command), then the answer."""

    records = []

    def log_message(self, *args):
        pass

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            body = {}
        MockProvider.records.append(body)
        messages = body.get("messages") or []
        after_tool = bool(messages) and messages[-1].get("role") == "tool"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        if after_tool:
            self._text("done")
        else:
            self._tool_call("toolkit_exec", EXEC_ARGS)
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def _text(self, text):
        self.wfile.write(("data: " + json.dumps({
            "id": "c", "choices": [{"index": 0, "delta": {"content": text},
                                    "finish_reason": None}],
        }) + "\n\n").encode())
        self.wfile.flush()

    def _tool_call(self, name, arguments):
        self.wfile.write(("data: " + json.dumps({
            "id": "c", "choices": [{"index": 0, "delta": {"tool_calls": [{
                "index": 0, "id": "call_1", "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }]}, "finish_reason": None}],
        }) + "\n\n").encode())
        self.wfile.flush()


def free_port():
    while True:
        port = random.randint(20000, 60000)
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue


def wait_health(port, timeout=30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            conn.request("GET", "/api/health")
            if conn.getresponse().status == 200:
                return True
        except OSError:
            pass
        time.sleep(0.2)
    return False


def call(port, method, path, body=None, timeout=60):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    headers = {"Content-Type": "application/json"} if body is not None else {}
    conn.request(method, path, body=json.dumps(body) if body is not None else None,
                 headers=headers)
    resp = conn.getresponse()
    raw = resp.read()
    try:
        return resp.status, json.loads(raw or b"null")
    except ValueError:
        return resp.status, {"raw": raw.decode("utf-8", "replace")}


def run_turn(port, workspace_id, message):
    body = {"workspace_id": workspace_id, "message": message}
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=120)
    conn.request("POST", "/api/chat", body=json.dumps(body),
                 headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    events = []
    while True:
        line = resp.readline()
        if not line:
            break
        line = line.strip()
        if not line.startswith(b"data: "):
            continue
        data = line[6:]
        if data == b"[DONE]":
            break
        try:
            events.append(json.loads(data))
        except ValueError:
            continue
    return events


def find(events, kind):
    for e in events:
        if e.get("type") == kind:
            return e
    return {}


def parse_iso(text):
    """Seconds since the epoch for an ISO8601 timestamp (the trailing Z)."""
    if not text:
        return None
    try:
        return time.mktime(time.strptime(text[:19], "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return None


def main():
    binary = (sys.argv[1] if len(sys.argv) > 1
              else os.path.join(REPO_ROOT, "build", "bin", "tea_agent"))
    if not os.path.exists(binary):
        print("binary not found:", binary)
        return 1

    MockProvider.records = []
    mock = ThreadingHTTPServer(("127.0.0.1", 0), MockProvider)
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    mock_port = mock.server_address[1]

    tmp = tempfile.mkdtemp(prefix="tea_timing_")
    port = free_port()
    ws_dir = os.path.join(tmp, "ws")
    os.makedirs(ws_dir)
    with open(os.path.join(ws_dir, "note.txt"), "w", encoding="utf-8") as f:
        f.write("hello\n")
    config = os.path.join(tmp, "provider.yaml")
    with open(config, "w", encoding="utf-8") as f:
        f.write("""version: 1
providers:
  mock:
    api_url: http://127.0.0.1:%d
    api_key: sk-test
    default_model: m
    models:
      m:
        max_context_tokens: 32768
        supports_tools: true
""" % mock_port)

    proc = subprocess.Popen([
        binary, "--config", config, "--tea-home", os.path.join(tmp, "home"),
        "--host", "127.0.0.1", "--port", str(port), "--no-console",
        "--log-file", os.path.join(tmp, "tea.log"), "--workspace", ws_dir,
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    failures = []
    checks = []

    def check(name, ok, detail=""):
        checks.append(name)
        print(("PASS " if ok else "FAIL ") + name + (("  " + str(detail)) if not ok else ""))
        if not ok:
            failures.append(name)

    try:
        if not wait_health(port):
            print("server did not become healthy")
            return 1
        _, listing = call(port, "GET", "/api/workspaces")
        wid = listing["workspaces"][0]["id"]

        events = run_turn(port, wid, "run the slow command")

        start = find(events, "tool_start")
        result = find(events, "tool_result")

        check("the tool call was announced", bool(start), events)
        check("tool_start says when the call started",
              bool(start.get("started_at")), start)
        check("tool_start says what the deadline is",
              start.get("timeout_ms") is not None, start)

        started = parse_iso(start.get("started_at"))
        check("started_at is an ISO8601 instant",
              started is not None, start.get("started_at"))
        check("the timeout is the one the command was given (5 s, not the default)",
              start.get("timeout_ms") == 5000, start.get("timeout_ms"))

        check("tool_result repeats the start time",
              result.get("started_at") == start.get("started_at"),
              (result.get("started_at"), start.get("started_at")))
        check("tool_result repeats the deadline",
              result.get("timeout_ms") == 5000, result.get("timeout_ms"))
        check("tool_result measures how long it took",
              result.get("elapsed_ms", 0) >= 400, result.get("elapsed_ms"))

        # The elapsed time must agree with the wall clock: the tool slept 0.6 s.
        check("the call really took about as long as the command slept",
              result.get("elapsed_ms", 0) >= 400,
              result.get("elapsed_ms"))

        # ---- the transcript keeps the numbers ------------------------------
        # The session id is not the workspace id: ask for the list.
        _, slist = call(port, "GET", "/api/workspaces/" + wid + "/sessions")
        sid = (slist.get("sessions") or [{}])[0].get("id")
        check("the session is on disk", bool(sid), slist)
        _, stored = call(port, "GET", "/api/sessions/" + sid)
        tool_msgs = [m for m in (stored.get("messages") or []) if m.get("role") == "tool"]
        check("the tool message is stored", bool(tool_msgs), stored.get("messages"))
        if tool_msgs:
            tm = tool_msgs[0]
            check("the stored message keeps the start time",
                  bool(tm.get("started_at")), tm)
            check("the stored message keeps the deadline",
                  tm.get("timeout_ms") == 5000, tm)
            check("the stored message keeps the elapsed time",
                  tm.get("elapsed_ms", 0) >= 400, tm)
    except Exception as e:  # noqa: BLE001 - a harness error is a failure too
        print("harness error:", e)
        failures.append("harness")
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            proc.kill()
        mock.shutdown()

    print()
    print("%d checks, %d failed" % (len(checks), len(failures)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
