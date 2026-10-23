#!/usr/bin/env python3
"""The tool-call structure of a request has to be one-to-one.

A provider maps every tool result back to the call it answers. MiniMax rejects
the *whole* request when the mapping is not one-to-one:

    HTTP 400 {"type":"error","error":{"type":"bad_request_error",
              "message":"invalid params, 400 (2013)","http_code":"400"}}

That is what a deployed 0.10.2 hit on the target device: the turn died with
"HTTP 400: invalid params, 400 (2013)" and the error said nothing about the
cause. Verified against api.minimax.cn on 2026-10-23 - the same request shape
answers 200 as soon as every tool result answers exactly one waiting call.

The mock below is MiniMax's rule set:

  * a tool result whose id is not waiting for an answer  -> 400
    ("invalid params, tool result's tool id(x) not found (2013)")
  * a second result for the same call                   -> 400
    ("invalid params, 400 (2013)")          <- the one from the device
  * a call that never got an answer                     -> 400
    ("invalid params, tool call and result not match (2013)")

What this pins:

  * a history with a duplicated tool result is repaired before it is sent, and
    the turn finishes;
  * an orphan tool result and an unanswered tool call are repaired too;
  * a provider that reuses a tool-call id does not put two answers to the same
    call into the session;
  * every request the agent sends has a one-to-one tool-call structure.

Usage: python3 tests/smoke_tool_call_structure.py [path-to-tea_agent-binary]
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

requests_seen = []          # every request body, in order
lock = threading.Lock()
# The tool call the mock answers with: the id is fixed so the test can check
# what the agent does with an id the history already used.
REPLY_CALL_ID = "c1"
REPLY_CALL_NAME = "toolkit_exec"


def check_structure(messages):
    """MiniMax's rules. Returns None when the request is fine, else the error."""
    declared = []
    answered = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        if m.get("role") == "assistant":
            for tc in (m.get("tool_calls") or []):
                cid = tc.get("id")
                if not cid:
                    return ("invalid params, 400 (2013)", 400)
                fn = tc.get("function") or {}
                if not fn.get("name"):
                    return ("invalid params, 400 (2013)", 400)
                if cid in declared:
                    return ("invalid params, duplicate tool_call id: %s (2013)" % cid, 400)
                declared.append(cid)
        elif m.get("role") == "tool":
            tid = m.get("tool_call_id")
            if not tid or tid not in declared:
                return ("invalid params, tool result's tool id(%s) not found (2013)" % tid, 400)
            if tid in answered:
                return ("invalid params, 400 (2013)", 400)
            answered.append(tid)
    for cid in declared:
        if cid not in answered:
            return ("invalid params, tool call and result not match (2013)", 400)
    return None


class Mock(BaseHTTPRequestHandler):
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
        with lock:
            requests_seen.append(body)

        problem = check_structure(body.get("messages") or [])
        if problem:
            message, code = problem
            payload = json.dumps({"type": "error", "error": {
                "type": "bad_request_error", "message": message, "http_code": str(code)}}).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        # The first answer asks for a tool; the second one ends the turn. The
        # tool call reuses the id the seeded history already used, so the agent
        # has to notice and rename it.
        turn = len([b for b in requests_seen])
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        def write(text):
            data = text.encode()
            self.wfile.write(("%x\r\n" % len(data)).encode() + data + b"\r\n")
            self.wfile.flush()

        def chunk(delta, finish=None):
            write("data: " + json.dumps({
                "id": "c1", "object": "chat.completion.chunk",
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }) + "\n\n")

        try:
            if turn == 1:
                chunk({"role": "assistant", "tool_calls": [{
                    "index": 0, "id": REPLY_CALL_ID, "type": "function",
                    "function": {"name": REPLY_CALL_NAME, "arguments": "{\"command\":\"echo hi\"}"}}]})
                chunk({}, "tool_calls")
            else:
                chunk({"content": "done "})
                chunk({}, "stop")
            write("data: " + json.dumps({
                "id": "c1", "object": "chat.completion.chunk", "choices": [],
                "usage": {"total_tokens": 10, "prompt_tokens": 8, "completion_tokens": 2},
            }) + "\n\n")
            write("data: [DONE]\n\n")
            write("")
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


def turn(port, wid, session_id, message):
    """One turn; returns the event types and the error message (if any)."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    payload = {"workspace_id": wid, "message": message}
    if session_id:
        payload["session_id"] = session_id
    conn.request("POST", "/api/chat", body=json.dumps(payload),
                 headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    types = []
    error = ""
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
        if ev.get("type") == "error":
            error = ev.get("message", "")
    conn.close()
    return types, error


def seed_session(ws_dir, session_id, messages):
    """Write a session file the agent will load as it is."""
    run_dir = os.path.join(ws_dir, ".tea_agent_run", "sessions")
    os.makedirs(run_dir, exist_ok=True)
    now = "2026-10-23T00:00:00Z"
    doc = {"id": session_id, "title": "seeded", "created_at": now, "updated_at": now,
           "messages": messages}
    with open(os.path.join(run_dir, session_id + ".json"), "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=2)


def assistant_call(cid, name=REPLY_CALL_NAME):
    return {"role": "assistant", "content": "", "tool_calls": [
        {"id": cid, "name": name, "arguments": {"command": "echo hi"},
         "raw_arguments": "{\"command\":\"echo hi\"}"}]}


def tool_result(cid, content="ok"):
    return {"role": "tool", "content": content, "tool_call_id": cid, "name": REPLY_CALL_NAME,
            "ok": True, "elapsed_ms": 1}


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

    tmp = tempfile.mkdtemp(prefix="tea_toolcalls_")
    ws_dir = os.path.join(tmp, "ws")
    os.makedirs(ws_dir)
    config_path = os.path.join(tmp, "provider.yaml")
    with open(config_path, "w", encoding="utf-8") as f:
        f.write("""version: 1
providers:
  picky:
    api_url: http://127.0.0.1:%d/v1
    api_key: sk-test
    models:
      m: {max_context_tokens: 32768, max_output_tokens: 256}
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

    try:
        for _ in range(100):
            try:
                if call(port, "GET", "/api/health", timeout=2)[0] == 200:
                    break
            except OSError:
                time.sleep(0.2)
        wid = call(port, "GET", "/api/workspaces")[1]["workspaces"][0]["id"]

        # ---- 1. a history with a duplicated tool result --------------------
        # This is the shape the target device hit: MiniMax answers
        # "invalid params, 400 (2013)" and the turn dies.
        seed_session(ws_dir, "s_dupe00000001", [
            {"role": "user", "content": "run it"},
            assistant_call("c1"),
            tool_result("c1"),
            tool_result("c1"),          # <- the second answer to the same call
            {"role": "user", "content": "again"},
        ])
        types, error = turn(port, wid, "s_dupe00000001", "carry on")
        check("a duplicated tool result does not kill the turn", "done" in types,
              (types, error))
        check("no provider error for the duplicated tool result", not error, error)
        first = requests_seen[0] if requests_seen else {}
        ids = [m.get("tool_call_id") for m in (first.get("messages") or [])
               if m.get("role") == "tool"]
        check("the request carries one answer per call", len(ids) == len(set(ids)), ids)

        # ---- 2. an orphan tool result and an unanswered call ---------------
        seed_session(ws_dir, "s_orphan0000001", [
            {"role": "user", "content": "run it"},
            tool_result("ghost"),        # <- answers a call that does not exist
            {"role": "user", "content": "again"},
        ])
        types, error = turn(port, wid, "s_orphan0000001", "carry on")
        check("an orphan tool result does not kill the turn", "done" in types, (types, error))

        seed_session(ws_dir, "s_unanswer00001", [
            {"role": "user", "content": "run it"},
            assistant_call("c9"),        # <- the turn was interrupted here
            {"role": "user", "content": "again"},
        ])
        types, error = turn(port, wid, "s_unanswer00001", "carry on")
        check("an unanswered tool call does not kill the turn", "done" in types, (types, error))

        # ---- 3. a provider that reuses a tool-call id ----------------------
        # The mock answers with the id "c1", which the seeded history already
        # used. The agent has to rename it, or the next request would carry two
        # answers to the same call.
        seed_session(ws_dir, "s_reuse00000001", [
            {"role": "user", "content": "run it"},
            assistant_call("c1"),
            tool_result("c1"),
            {"role": "user", "content": "again"},
        ])
        with lock:
            del requests_seen[:]
        types, error = turn(port, wid, "s_reuse00000001", "carry on")
        check("a reused tool-call id does not kill the turn", "done" in types, (types, error))
        with lock:
            bodies = list(requests_seen)
        check("the agent sent a second request", len(bodies) >= 2, len(bodies))
        for body in bodies:
            problem = check_structure(body.get("messages") or [])
            if problem:
                check("every request has a one-to-one tool-call structure", False,
                      (problem, body.get("messages")))
                break
        else:
            check("every request has a one-to-one tool-call structure", True)
        # The renamed id must not be the one the history already used.
        second = bodies[1] if len(bodies) > 1 else {}
        tool_ids = [m.get("tool_call_id") for m in (second.get("messages") or [])
                    if m.get("role") == "tool"]
        check("the reused id was renamed", REPLY_CALL_ID not in tool_ids[1:], tool_ids)

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            proc.kill()

    print("%d checks, %d failed" % (len(checks), len(failures)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
