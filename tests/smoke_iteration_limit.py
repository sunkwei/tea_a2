#!/usr/bin/env python3
"""Smoke test for the turn that runs out of iterations (max_iterations).

A turn is a loop: the model asks for a tool, the tool answers, the model looks
at the answer and asks for the next tool. The loop stops when the model answers
with text instead of a tool call - that text is the turn's **final answer**.

The loop also stops when the iteration budget is spent. Then there is no final
answer: the model was still calling tools. Before this test the turn simply
ended there, and the conversation looked like the agent had gone mute - the
user had to send another message to find out what happened (and to get an
answer at all).

What is pinned here:

  * the turn still ends (it does not hang) when the budget is spent;
  * the conversation ends with an **assistant message** that says the budget
    ran out and that the work is not finished - the message is in the session
    file, so it survives a reload;
  * the message is also pushed to the client while the turn ends (a
    content_delta), so the live bubble shows it right away;
  * the `done` event carries `hit_iteration_limit`, so a client that only sees
    the end of the stream can say what happened;
  * the trajectory of that turn records it too.

Usage: python3 tests/smoke_iteration_limit.py [path-to-tea_agent-binary]
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
    from socketsocketserver import ThreadingMixIn  # noqa: F401  (never reached)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

MAX_ITERATIONS = 3


class MockProvider(BaseHTTPRequestHandler):
    """Always asks for one more tool call: the loop never ends by itself."""

    calls = 0

    def log_message(self, *args):
        pass

    def _read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length) or b"{}")

    def _chunk(self, payload):
        try:
            self.wfile.write(("data: " + json.dumps(payload) + "\n\n").encode())
            self.wfile.flush()
        except OSError:
            pass

    def do_POST(self):
        if not self.path.endswith("/chat/completions"):
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        payload = self._read_json()
        MockProvider.calls += 1
        cid = "c%d" % MockProvider.calls
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        # 每次调不同的命令：这样停下来的原因只有迭代上限，不会先被
        # 「连续三次相同输出」的闸拦下（那是另一个测试的事）。
        self._chunk({
            "id": "c",
            "choices": [{
                "index": 0,
                "delta": {
                    "role": "assistant",
                    "tool_calls": [{
                        "index": 0, "id": cid, "type": "function",
                        "function": {"name": "toolkit_exec",
                                     "arguments": json.dumps({"command": "echo tick %s" % cid})},
                    }],
                },
                "finish_reason": None,
            }],
        })
        self._chunk({"id": "c", "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]})
        self.wfile.write(b"data: [DONE]\n\n")
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

    MockProvider.calls = 0
    mock = ThreadingHTTPServer(("127.0.0.1", 0), MockProvider)
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    mock_port = mock.server_address[1]

    tmp = tempfile.mkdtemp(prefix="tea_iter_limit_")
    port = free_port()
    ws_dir = os.path.join(tmp, "ws")
    run_dir = os.path.join(ws_dir, ".tea_agent_run")
    os.makedirs(run_dir)
    # The workspace pins its own budget: the turn must stop after 3 iterations.
    with open(os.path.join(run_dir, "settings.yaml"), "w", encoding="utf-8") as f:
        f.write("max_iterations: %d\n" % MAX_ITERATIONS)
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
        _, sess = call(port, "POST", "/api/workspaces/%s/sessions" % wid, {"title": "limit"})
        sid = sess["id"]
        session_file = os.path.join(run_dir, "sessions", sid + ".json")

        # ---- the turn ends, and it says why --------------------------------
        events = run_turn(port, wid, sid, "keep going")
        done = [e for e in events if e.get("type") == "done"]
        check("the turn ends", bool(done), [e.get("type") for e in events][-6:])
        check("the turn used exactly the iteration budget",
              done and done[0].get("iterations") == MAX_ITERATIONS,
              done[0].get("iterations") if done else None)
        check("the done event says the budget ran out",
              done and done[0].get("hit_iteration_limit") is True,
              done[0] if done else None)

        # ---- there is a final assistant message -----------------------------
        saved = json.load(open(session_file, encoding="utf-8"))
        messages = saved.get("messages") or []
        last = messages[-1] if messages else {}
        check("the conversation ends with an assistant message",
              last.get("role") == "assistant", last.get("role"))
        note = str(last.get("content") or "")
        check("the message says the budget ran out",
              "max_iterations" in note or "额度" in note, note[:160])
        check("the message says the work is not finished",
              "没" in note and "收尾" in note, note[:160])
        # 只有预算（max_iterations）没有「实际跑了多少轮」——写个数字出来只会
        # 被人当成「跑了 N 次工具调用」。所以这句话里一个数字都不该有。
        check("the message shows no round count",
              not any(ch.isdigit() for ch in note), note[:160])

        # ---- the client sees it while the turn ends -------------------------
        deltas = [e for e in events
                  if e.get("type") == "content_delta" and "额度" in str(e.get("text") or "")]
        check("the note is pushed to the client as it happens", bool(deltas),
              [e.get("type") for e in events][-6:])

        # ---- the API serves the same session -------------------------------
        _, detail = call(port, "GET", "/api/sessions/%s" % sid)
        api_last = (detail.get("messages") or [{}])[-1]
        check("GET /api/sessions ends with the same assistant message",
              api_last.get("role") == "assistant" and "额度" in str(api_last.get("content") or ""),
              str(api_last)[:160])

        # ---- the trajectory records it --------------------------------------
        _, turns = call(port, "GET", "/api/sessions/%s/trajectory" % sid)
        rows = turns if isinstance(turns, list) else []
        check("the trajectory records the cap",
              rows and rows[0].get("hit_iteration_limit") is True,
              rows[0] if rows else rows)
        check("the repetition guard did not fire (the calls differ)",
              rows and not rows[0].get("repeated_output"),
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
