#!/usr/bin/env python3
"""The file tools' boundary, and the per-turn grant that opens it.

By default the file tools (toolkit_file, toolkit_load_image) refuse any path
outside the workspace root: "path escapes the workspace". A caller that drives
its own policy - the task queue does - can say "this turn may also work in
these directories" with `allow_paths` in POST /api/chat. The grant lasts that
turn only.

What this pins:

  * without the grant, a path outside the workspace is refused;
  * with it, the same path is read;
  * the grant does not leak into the next turn;
  * the system prompt tells the model what it may reach (a model that does not
    know refuses, or wastes turns finding out).

Usage: python3 tests/smoke_allow_paths.py [path-to-tea_agent-binary]
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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class MockProvider(BaseHTTPRequestHandler):
    """Reads one file with toolkit_file - whatever path the request names."""

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
            # The path to read is in the user message ("read <path>").
            prompt = ""
            for m in reversed(messages):
                if m.get("role") == "user":
                    prompt = m.get("content") or ""
                    break
            path = prompt.replace("read", "").strip()
            self._tool_call("toolkit_file", {"op": "read", "path": path})
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def _text(self, text):
        self.wfile.write(("data: " + json.dumps({
            "id": "c", "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
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


def run_turn(port, workspace_id, message, allow_paths=None):
    body = {"workspace_id": workspace_id, "message": message}
    if allow_paths is not None:
        body["allow_paths"] = allow_paths
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


def tool_output(events, name="toolkit_file"):
    for e in events:
        if e.get("type") == "tool_result" and e.get("name") == name:
            return e.get("output", "")
    return ""


def system_prompt(request):
    for m in request.get("messages") or []:
        if m.get("role") == "system":
            return m.get("content") or ""
    return ""


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

    tmp = tempfile.mkdtemp(prefix="tea_allow_")
    port = free_port()
    ws_dir = os.path.join(tmp, "ws")
    outside_dir = os.path.join(tmp, "outside")
    os.makedirs(ws_dir)
    os.makedirs(outside_dir)
    secret = os.path.join(outside_dir, "secret.txt")
    with open(secret, "w", encoding="utf-8") as f:
        f.write("the file outside the workspace\n")
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

        # ---- without the grant: refused -------------------------------------
        events = run_turn(port, wid, "read " + secret)
        out = tool_output(events)
        check("a path outside the workspace is refused",
              "escapes the workspace" in out, out[:200])
        check("the refusal names the workspace root", ws_dir in out, out[:200])
        prompt = system_prompt(MockProvider.records[-1])
        check("the prompt says the file tools are fenced",
              "restricted to the workspace root" in prompt, prompt[-300:])

        # ---- with the grant: it works ---------------------------------------
        events = run_turn(port, wid, "read " + secret, allow_paths=[outside_dir])
        out = tool_output(events)
        check("the same path is read when the turn is granted it",
              "the file outside the workspace" in out, out[:200])
        prompt = system_prompt(MockProvider.records[-1])
        check("the prompt tells the model about the extra directory",
              outside_dir in prompt and "may also access" in prompt, prompt[-400:])

        # ---- the grant does not leak into the next turn ---------------------
        events = run_turn(port, wid, "read " + secret)
        out = tool_output(events)
        check("the next turn is fenced again", "escapes the workspace" in out, out[:200])

        # ---- a granted directory does not open the whole disk ---------------
        events = run_turn(port, wid, "read " + os.path.join(tmp, "nope.txt"),
                          allow_paths=[outside_dir])
        out = tool_output(events)
        check("a path outside the granted directory is still refused",
              "escapes the workspace" in out, out[:200])
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
