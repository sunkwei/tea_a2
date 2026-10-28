#!/usr/bin/env python3
"""Smoke test for the two ways a turn can end without a final answer.

A turn is a loop: the model asks for a tool, the tool answers, the model looks
at the answer and asks for the next tool. The loop ends when the model answers
with text - that text is the turn's **final answer**.

Two things can cut the loop short, and both of them have to leave a final
message behind (otherwise the conversation looks like the agent went mute and
the user has to send another message to find out what happened):

  * the model gives the **same output three times in a row** - it is going in
    circles, not working. The turn stops and says so;
  * `max_iterations` is set (it is **not** by default any more: 0 means "run
    until the conversation is done"), and the budget runs out. The turn stops
    and says so.

What is pinned here:

  * three identical outputs stop the turn - and the third one is the last;
  * the conversation ends with an assistant message that says the model was
    repeating itself, and that message is in the session file;
  * the `done` event carries `repeated_output`, and the trajectory records it;
  * with no limit configured, a turn runs past the old 50-iteration cap (61
    iterations here) and ends with a normal final answer;
  * a turn with an explicit `max_iterations` still stops there, with a note.

Usage: python3 tests/smoke_repeat_guard.py [path-to-tea_agent-binary]
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

# How many iterations the "varying" mock does before it finally answers.
VARY_ITERATIONS = 61


class MockProvider(BaseHTTPRequestHandler):
    """`repeat`: always the same tool call. `vary`: a different one each time."""

    mode = "repeat"

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
        if MockProvider.mode == "vary":
            if done_tools >= VARY_ITERATIONS:
                self._content("做完了。")
                return
            self._tool_call("c%d" % done_tools, "echo %d" % done_tools)
            return
        # repeat: the same call, over and over
        self._tool_call("same", "echo tick")


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


def run_turn(port, workspace_id, session_id, message="go", timeout=300):
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
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    mock_port = mock.server_address[1]

    tmp = tempfile.mkdtemp(prefix="tea_repeat_guard_")
    port = free_port()
    ws_dir = os.path.join(tmp, "ws")
    os.makedirs(ws_dir)
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

        # ---- the default has no iteration cap ------------------------------
        _, providers = call(port, "GET", "/api/providers")
        cap = (providers.get("settings") or {}).get("max_iterations")
        check("max_iterations defaults to 0 (no limit)", cap == 0, cap)

        # ---- the same output three times in a row stops the turn ----------
        MockProvider.mode = "repeat"
        _, sess = call(port, "POST", "/api/workspaces/%s/sessions" % wid, {"title": "repeat"})
        sid = sess["id"]
        events = run_turn(port, wid, sid, "go")
        done = [e for e in events if e.get("type") == "done"]
        check("the turn ends", bool(done), [e.get("type") for e in events][-6:])
        check("the turn stopped after the third identical output",
              done and done[0].get("iterations") == 3, done[0].get("iterations") if done else None)
        check("the done event says the model repeated itself",
              done and done[0].get("repeated_output") is True, done[0] if done else None)
        check("the done event says what repeated",
              done and "toolkit_exec" in str(done[0].get("repeated_detail") or ""),
              done[0].get("repeated_detail") if done else None)
        repeated = [e for e in events if e.get("type") == "repeated_output"]
        check("the client is told about the repetition", bool(repeated), repeated[:1])

        _, detail = call(port, "GET", "/api/sessions/%s" % sid)
        messages = detail.get("messages") or []
        last = messages[-1] if messages else {}
        check("the conversation ends with an assistant message",
              last.get("role") == "assistant", last.get("role"))
        note = str(last.get("content") or "")
        check("the message says the model was repeating itself",
              "相同的结果" in note or "原地打转" in note, note[:200])
        check("the message says the turn stopped there",
              "到此为止" in note, note[:200])

        _, turns = call(port, "GET", "/api/sessions/%s/trajectory" % sid)
        rows = turns if isinstance(turns, list) else []
        check("the trajectory records the repetition",
              rows and rows[0].get("repeated_output") is True, rows[0] if rows else rows)
        check("the iteration cap did not fire (there is no cap)",
              rows and not rows[0].get("hit_iteration_limit"),
              rows[0] if rows else rows)

        # ---- with no limit, a turn runs past the old cap --------------------
        MockProvider.mode = "vary"
        _, sess2 = call(port, "POST", "/api/workspaces/%s/sessions" % wid, {"title": "long"})
        sid2 = sess2["id"]
        events = run_turn(port, wid, sid2, "keep going")
        done = [e for e in events if e.get("type") == "done"]
        check("a long turn ends", bool(done), [e.get("type") for e in events][-4:])
        # VARY_ITERATIONS tool calls, then one more request that answers with
        # text: the turn counts every model request.
        check("it ran past the old 50-iteration cap",
              done and done[0].get("iterations") == VARY_ITERATIONS + 1,
              done[0].get("iterations") if done else None)
        check("it was not cut short by the repetition guard",
              done and not done[0].get("repeated_output"), done[0] if done else None)
        _, detail = call(port, "GET", "/api/sessions/%s" % sid2)
        last = (detail.get("messages") or [{}])[-1]
        check("it ends with the model's own answer",
              last.get("role") == "assistant" and "做完了" in str(last.get("content") or ""),
              str(last)[:160])
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
