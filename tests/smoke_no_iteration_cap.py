#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Smoke test for the turn that has **no** iteration cap.

A turn is a loop: the model asks for a tool, the tool answers, the model looks
at the answer and asks for the next tool. The loop ends when the model answers
with text instead of a tool call - that text is the turn's **final answer**.

There used to be a budget: `max_iterations` in the config. It is gone - not
"off by default", gone: the code does not read it any more, so a file that
still carries the key must not change anything.

What is pinned here:

  * the settings have no iteration knob (the API does not report one);
  * a `max_iterations:` key in provider.yaml's `settings:` block is ignored -
    and the config says so in the log, so nobody wonders why it does nothing;
  * the same key in the workspace's `settings.yaml` is ignored as well;
  * a turn that needs more rounds than that key allows runs to the end and
    ends with the model's own answer - not with a note about a budget;
  * nothing in the done event, the trajectory or the session says that a cap
    was hit.

Usage: python3 tests/smoke_no_iteration_cap.py [path-to-tea_agent-binary]
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

# How many tool calls the mock makes before it finally answers. The point is
# that this is MORE than the budget the config asks for - the budget is
# ignored, so the turn runs all the way.
TOOL_CALLS = 6

# The budget the config asks for. It has to be ignored.
CONFIGURED_BUDGET = 2


class MockProvider(BaseHTTPRequestHandler):
    """A model that calls a tool a few times and then answers."""

    def log_message(self, *args):
        pass

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"{}")

    def _chunk(self, payload):
        try:
            self.wfile.write(("data: " + json.dumps(payload) + "\n\n").encode())
            self.wfile.flush()
        except OSError:
            pass

    def _content(self, text):
        self._chunk({"id": "c", "choices": [{"index": 0, "delta": {"content": text},
                                             "finish_reason": None}]})
        self._chunk({"id": "c", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})

    def _tool_call(self, cid, command):
        self._chunk({
            "id": "c",
            "choices": [{
                "index": 0,
                "delta": {
                    "role": "assistant",
                    "tool_calls": [{
                        "index": 0, "id": cid, "type": "function",
                        "function": {"name": "toolkit_exec",
                                     "arguments": json.dumps({"command": command})},
                    }],
                },
                "finish_reason": None,
            }],
        })
        self._chunk({"id": "c", "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]})

    def do_POST(self):
        if not self.path.endswith("/chat/completions"):
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        payload = self._read_json()
        msgs = payload.get("messages") or []
        # How many tools have already answered in this turn: that is the
        # iteration number (the first request has none).
        done_tools = len([m for m in msgs if m.get("role") == "tool"])

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        if done_tools >= TOOL_CALLS:
            self._content("做完了。")
            return
        # A different command every time: three identical calls in a row would
        # trip the repetition guard, and this test is about the cap, not that.
        self._tool_call("c%d" % done_tools, "echo %d" % done_tools)


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
    payload = None
    headers = {}
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    conn.request(method, path, body=payload, headers=headers)
    resp = conn.getresponse()
    raw = resp.read()
    try:
        return resp.status, json.loads(raw or b"null")
    except ValueError:
        return resp.status, {"raw": raw.decode("utf-8", "replace")}


def run_turn(port, workspace_id, session_id, message="go", timeout=120):
    body = {"workspace_id": workspace_id, "session_id": session_id, "message": message}
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
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

    tmp = tempfile.mkdtemp(prefix="tea_no_cap_")
    port = free_port()
    ws_dir = os.path.join(tmp, "ws")
    run_dir = os.path.join(ws_dir, ".tea_agent_run")
    os.makedirs(run_dir)
    # The budget in the workspace's own settings file: ignored as well.
    with open(os.path.join(run_dir, "settings.yaml"), "w", encoding="utf-8") as f:
        f.write("max_iterations: %d\n" % CONFIGURED_BUDGET)
    # And in provider.yaml - where the whole `settings:` block is not read any
    # more. The config warns about it, so the log has to say so.
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
settings:
  max_iterations: %d
""" % (mock_port, CONFIGURED_BUDGET))

    log_path = os.path.join(tmp, "tea.log")
    proc = subprocess.Popen([
        binary, "--config", config, "--tea-home", os.path.join(tmp, "home"),
        "--host", "127.0.0.1", "--port", str(port), "--no-console",
        "--log-file", log_path, "--workspace", ws_dir,
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

        # ---- the settings have no iteration knob ---------------------------
        _, providers = call(port, "GET", "/api/providers")
        settings = providers.get("settings") or {}
        check("the settings have no iteration knob",
              "max_iterations" not in settings, settings)

        # ---- the config says the settings block is not read ----------------
        log = ""
        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                log = f.read()
        except (IOError, OSError):
            pass
        check("the config says provider.yaml's settings block is not read",
              "not read any more" in log, log[-400:])

        # ---- a turn that needs more rounds than the budget runs to the end --
        _, listing = call(port, "GET", "/api/workspaces")
        wid = listing["workspaces"][0]["id"]
        _, sess = call(port, "POST", "/api/workspaces/%s/sessions" % wid, {"title": "long"})
        sid = sess["id"]
        events = run_turn(port, wid, sid, "keep going")
        done = [e for e in events if e.get("type") == "done"]
        check("the turn ends", bool(done), [e.get("type") for e in events][-6:])
        check("it ran past the budget the config asked for",
              done and done[0].get("iterations") == TOOL_CALLS + 1,
              done[0].get("iterations") if done else None)
        check("the done event does not say that a cap was hit",
              done and "hit_iteration_limit" not in done[0],
              done[0] if done else None)
        check("the turn was not cut short by the repetition guard",
              done and not done[0].get("repeated_output"), done[0] if done else None)

        # ---- the conversation ends with the model's own answer --------------
        session_file = os.path.join(run_dir, "sessions", sid + ".json")
        saved = json.load(open(session_file, encoding="utf-8"))
        messages = saved.get("messages") or []
        last = messages[-1] if messages else {}
        check("the conversation ends with the model's own answer",
              last.get("role") == "assistant" and "做完了" in str(last.get("content") or ""),
              str(last)[:160])
        check("no note about a budget was left behind",
              "额度" not in str(last.get("content") or ""), str(last)[:160])

        # ---- the API serves the same session --------------------------------
        _, detail = call(port, "GET", "/api/sessions/%s" % sid)
        api_last = (detail.get("messages") or [{}])[-1]
        check("GET /api/sessions ends with the same answer",
              api_last.get("role") == "assistant" and "做完了" in str(api_last.get("content") or ""),
              str(api_last)[:160])

        # ---- the trajectory has no cap flag ----------------------------------
        _, turns = call(port, "GET", "/api/sessions/%s/trajectory" % sid)
        rows = turns if isinstance(turns, list) else []
        check("the trajectory records the turn", bool(rows), rows)
        check("the trajectory has no iteration-cap flag",
              rows and "hit_iteration_limit" not in rows[0],
              rows[0] if rows else rows)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        mock.shutdown()

    print("")
    print("%d/%d checks passed" % (len(checks) - len(failures), len(checks)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
