#!/usr/bin/env python3
"""Bytes that are not UTF-8 must not take the agent down.

Everything the agent hands to the JSON layer - a file it read, the output of a
command, the text the model streamed - ends up in a JSON document. nlohmann's
parser accepts such bytes inside a string, but its serialiser refuses them:

    agent crashed: [json.exception.type_error.316] invalid UTF-8 byte at
    index 4368: 0x20

(the reported byte is the one *after* the broken sequence: a lead byte that is
followed by a space is what a cut or a foreign encoding leaves behind).

The bytes are now replaced with U+FFFD where they enter the program, and every
JSON document is serialised with an error handler that replaces instead of
throwing - so the turn survives and the model gets readable text.

Usage: python3 tests/smoke_invalid_utf8.py [path-to-tea_agent-binary]
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
REPLACEMENT = "�"


class MockProvider(BaseHTTPRequestHandler):
    """Calls one tool, then answers."""

    def log_message(self, *args):
        pass

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw or b"{}")
        except (ValueError, UnicodeDecodeError):
            # The agent sent a request that is not valid UTF-8. Remember it:
            # the provider would have answered with a 400.
            self.server.bad_requests.append(raw[:200])
            body = {}
        messages = body.get("messages") or []
        after_tool = bool(messages) and messages[-1].get("role") == "tool"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        if after_tool:
            self.wfile.write(("data: " + json.dumps({
                "id": "c", "choices": [{"index": 0, "delta": {"content": "done"},
                                        "finish_reason": None}],
            }) + "\n\n").encode())
        else:
            prompt = ""
            for m in reversed(messages):
                if m.get("role") == "user":
                    prompt = m.get("content") or ""
                    break
            if "exec" in prompt:
                call = {"name": "toolkit_exec",
                        "arguments": {"command": "cat " + prompt.split()[-1]}}
            else:
                call = {"name": "toolkit_file",
                        "arguments": {"op": "read", "path": prompt.split()[-1]}}
            self.wfile.write(("data: " + json.dumps({
                "id": "c", "choices": [{"index": 0, "delta": {"tool_calls": [{
                    "index": 0, "id": "call_1", "type": "function",
                    "function": {"name": call["name"],
                                 "arguments": json.dumps(call["arguments"])},
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
    """Returns (events, broken_frames).

    A frame that is not valid JSON is a frame the agent could not serialise -
    the very failure this test is about.
    """
    body = {"workspace_id": workspace_id, "message": message}
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=120)
    conn.request("POST", "/api/chat", body=json.dumps(body),
                 headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    events = []
    broken = []
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
            events.append(json.loads(data.decode("utf-8")))
        except (ValueError, UnicodeDecodeError):
            broken.append(data[:200])
    return events, broken


def main():
    binary = (sys.argv[1] if len(sys.argv) > 1
              else os.path.join(REPO_ROOT, "build", "bin", "tea_agent"))
    if not os.path.exists(binary):
        print("binary not found:", binary)
        return 1

    mock = ThreadingHTTPServer(("127.0.0.1", 0), MockProvider)
    mock.bad_requests = []
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    mock_port = mock.server_address[1]

    tmp = tempfile.mkdtemp(prefix="tea_badutf8_")
    port = free_port()
    ws_dir = os.path.join(tmp, "ws")
    os.makedirs(ws_dir)
    # A lead byte followed by a space: exactly what the crash report showed
    # (the reported byte is the one after the broken sequence).
    bad_path = os.path.join(ws_dir, "bad.bin")
    with open(bad_path, "wb") as f:
        f.write(b"plain ascii\n")
        f.write(b"broken \xe4 byte\n")
        f.write(b"more ascii\n")

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

        # ---- a file whose content is not UTF-8 ---------------------------
        events, broken = run_turn(port, wid, "read " + bad_path)
        errors = [e for e in events if e.get("type") == "error"]
        check("reading a file with a broken byte does not crash the turn", not errors,
              errors[0].get("message") if errors else "")
        check("every event frame is valid JSON", not broken, broken[:1])
        results = [e for e in events if e.get("type") == "tool_result"]
        check("the tool answered", bool(results), events[-3:])
        out = results[0].get("output", "") if results else ""
        check("the result is a string", isinstance(out, str), type(out))
        check("the result is valid UTF-8", isinstance(out, str) and
              out.encode("utf-8", "replace").decode("utf-8") == out, out[:80])
        check("the turn finished", any(e.get("type") == "done" for e in events),
              [e.get("type") for e in events])
        if sys.platform != "win32":
            # On Windows the console code page transcodes the byte instead;
            # everywhere else the byte stays as it is and has to be replaced.
            check("the broken byte became the replacement character",
                  REPLACEMENT in out, repr(out[:80]))
        check("the readable text survived", "plain ascii" in out and
              "more ascii" in out, repr(out[:120]))

        # ---- a command that prints raw bytes ------------------------------
        events, broken = run_turn(port, wid, "exec " + bad_path)
        errors = [e for e in events if e.get("type") == "error"]
        check("a command with raw bytes does not crash the turn", not errors,
              errors[0].get("message") if errors else "")
        check("every event frame is valid JSON", not broken, broken[:1])
        results = [e for e in events if e.get("type") == "tool_result"]
        out = results[0].get("output", "") if results else ""
        check("the command output is valid UTF-8", isinstance(out, str), type(out))
        if sys.platform != "win32":
            check("the raw byte became the replacement character",
                  REPLACEMENT in out, repr(out[:120]))

        # ---- what the provider received ----------------------------------
        check("every request to the provider was valid UTF-8",
              not mock.bad_requests, mock.bad_requests[:1])

        # ---- what was written to disk -------------------------------------
        sessions = os.path.join(ws_dir, ".tea_agent_run", "sessions")
        broken_files = []
        for name in sorted(os.listdir(sessions)) if os.path.isdir(sessions) else []:
            if not name.endswith(".json"):
                continue
            with open(os.path.join(sessions, name), "rb") as f:
                blob = f.read()
            try:
                blob.decode("utf-8")
            except UnicodeDecodeError:
                broken_files.append(name)
        check("the session file on disk is valid UTF-8", not broken_files, broken_files)
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        mock.shutdown()

    print("")
    print("%d/%d checks passed" % (len(checks) - len(failures), len(checks)))
    if failures:
        print("FAILED: " + ", ".join(failures))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
