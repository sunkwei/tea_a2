#!/usr/bin/env python3
"""A file cut in the middle of a character must not take the agent down.

toolkit_file reads at most `max_bytes` of a file. If the cut lands in the
middle of a multi-byte character, the tool result is not valid UTF-8 - and the
JSON layer refuses to serialise that, so the turn died with

    agent crashed: [json.exception.type_error.316] invalid UTF-8 byte at index
    131072: 0x0A

The read now stops at a character boundary.

Usage: python3 tests/smoke_utf8_truncation.py [path-to-tea_agent-binary]
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
CUT = 64  # the tool's max_bytes


class MockProvider(BaseHTTPRequestHandler):
    """Reads the file the user names, with a small byte limit."""

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
        after_tool = bool(messages) and messages[-1].get("role") == "tool"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        if after_tool:
            self.wfile.write(("data: " + json.dumps({
                "id": "c", "choices": [{"index": 0, "delta": {"content": "read"},
                                        "finish_reason": None}],
            }) + "\n\n").encode())
        else:
            prompt = ""
            for m in reversed(messages):
                if m.get("role") == "user":
                    prompt = m.get("content") or ""
                    break
            path = prompt.replace("read", "").strip()
            self.wfile.write(("data: " + json.dumps({
                "id": "c", "choices": [{"index": 0, "delta": {"tool_calls": [{
                    "index": 0, "id": "call_1", "type": "function",
                    "function": {"name": "toolkit_file", "arguments": json.dumps(
                        {"op": "read", "path": path, "max_bytes": CUT})},
                }]}, "finish_reason": None}],
            }) + "\n\n").encode())
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


def main():
    binary = (sys.argv[1] if len(sys.argv) > 1
              else os.path.join(REPO_ROOT, "build", "bin", "tea_agent"))
    if not os.path.exists(binary):
        print("binary not found:", binary)
        return 1

    mock = ThreadingHTTPServer(("127.0.0.1", 0), MockProvider)
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    mock_port = mock.server_address[1]

    tmp = tempfile.mkdtemp(prefix="tea_utf8_")
    port = free_port()
    ws_dir = os.path.join(tmp, "ws")
    os.makedirs(ws_dir)
    # The cut lands in the middle of a three-byte character.
    content = ("x" * (CUT - 1)) + "中" + "y" * 100
    with open(os.path.join(ws_dir, "cut.txt"), "w", encoding="utf-8") as f:
        f.write(content)
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
        path = os.path.join(ws_dir, "cut.txt")
        events = run_turn(port, wid, "read " + path)

        errors = [e for e in events if e.get("type") == "error"]
        check("the turn does not crash on a cut character", not errors,
              errors[0].get("message") if errors else "")
        results = [e for e in events if e.get("type") == "tool_result"]
        check("the tool answered", bool(results), events[-3:])
        out = results[0].get("output", "") if results else ""
        check("the result is valid UTF-8", isinstance(out, str), type(out))
        check("the cut stopped at a character boundary", "中" not in out, out[-40:])
        check("the file was reported as truncated", results and results[0].get("truncated") is True,
              results[0] if results else None)
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
