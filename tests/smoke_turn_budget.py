#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Smoke test for the **budget of one turn**.

A turn is a loop: the model asks for a tool, the tool answers, the model
looks at the answer and asks for the next one. It ends when the model answers
with text - or when it runs out of budget.

The budget is the machine's word, not a promise in a prompt:

  * the default ceiling is **300 iterations** (`GET /api/health` -> limits);
  * the settings can lower it (`max_iterations` in tea_agent.yaml, in the
    workspace's settings.yaml, or in provider.yaml's `settings:` block);
  * a request may **tighten** it for one turn (`limits` in the body of
    /api/chat) but never loosen it - the machine keeps the final word;
  * running out of room ends the turn **normally**: `stop_reason: "budget"`,
    not an error. The caller has to be able to tell "the work is done" from
    "we have no room left";
  * the trajectory records the task, the budget and how the turn ended.

Usage: python3 tests/smoke_turn_budget.py [path-to-tea_agent-binary]
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
    from socketserver import MixIn

    class ThreadingHTTPServer(HTTPServer):
        """The 3.6 spelling of http.server.ThreadingHTTPServer."""
        daemon_threads = True

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The default ceiling of one turn (iterations).
DEFAULT_MAX_ITERATIONS = 300


class MockProvider(BaseHTTPRequestHandler):
    """A model that calls a tool for ever (it never answers by itself)."""

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
        done_tools = len([m for m in msgs if m.get("role") == "tool"])
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        # A different command every time: three identical calls in a row would
        # trip the repetition guard, and this test is about the budget.
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


def run_turn(port, workspace_id, session_id, message="go", timeout=120, limits=None,
             task_id=None):
    body = {"workspace_id": workspace_id, "session_id": session_id, "message": message}
    if limits is not None:
        body["limits"] = limits
    if task_id:
        body["task_id"] = task_id
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


def start_agent(binary, tmp, mock_port, port, extra_settings=""):
    """Start the agent against the mock provider.

    `extra_settings` goes into provider.yaml's `settings:` block (the one
    place a task's operator looks first).
    """
    ws_dir = os.path.join(tmp, "ws")
    run_dir = os.path.join(ws_dir, ".tea_agent_run")
    os.makedirs(run_dir)
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
%s""" % (mock_port, extra_settings))
    log_path = os.path.join(tmp, "tea.log")
    proc = subprocess.Popen([
        binary, "--config", config, "--tea-home", os.path.join(tmp, "home"),
        "--host", "127.0.0.1", "--port", str(port), "--no-console",
        "--log-file", log_path, "--workspace", ws_dir,
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return proc, ws_dir, run_dir, log_path


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

    tmp = tempfile.mkdtemp(prefix="tea_budget_")
    port = free_port()
    proc, ws_dir, run_dir, log_path = start_agent(binary, tmp, mock_port, port)

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

        def new_session(title):
            _, sess = call(port, "POST", "/api/workspaces/%s/sessions" % wid, {"title": title})
            return sess["id"]

        # ---- the default ceiling ------------------------------------------
        _, health = call(port, "GET", "/api/health")
        limits = health.get("limits") or {}
        check("the agent reports its budget",
              limits.get("max_iterations") == DEFAULT_MAX_ITERATIONS, limits)
        _, providers = call(port, "GET", "/api/providers")
        settings = providers.get("settings") or {}
        check("the settings report the budget too",
              (settings.get("budget") or {}).get("max_iterations") == DEFAULT_MAX_ITERATIONS,
              settings.get("budget"))

        # ---- a request may tighten the budget ------------------------------
        sid = new_session("tightened")
        events = run_turn(port, wid, sid, "go", limits={"max_iterations": 2})
        done = [e for e in events if e.get("type") == "done"]
        check("the turn ends", bool(done), [e.get("type") for e in events][-6:])
        check("it stopped at the requested number of iterations",
              done and done[0].get("iterations") == 2, done[0].get("iterations") if done else None)
        check("the done event says the budget stopped it",
              done and done[0].get("stop_reason") == "budget", done[0] if done else None)
        check("it says which ceiling was hit",
              done and (done[0].get("budget_used") or {}).get("limit") == "iterations",
              done[0].get("budget_used") if done else None)
        check("the turn is not an error",
              not [e for e in events if e.get("type") == "error"],
              [e.get("type") for e in events][-6:])
        check("there is a budget event",
              bool([e for e in events if e.get("type") == "budget"]),
              [e.get("type") for e in events][-6:])
        check("the answer says the budget ran out",
              done and "预算" in str(done[0].get("content") or ""),
              str(done[0].get("content"))[:160] if done else None)

        # ---- the tool-call budget -----------------------------------------
        sid = new_session("tools")
        events = run_turn(port, wid, sid, "go", limits={"max_tool_calls": 1})
        done = [e for e in events if e.get("type") == "done"]
        results = [e for e in events if e.get("type") == "tool_result"]
        check("the tool budget stops the turn",
              done and done[0].get("stop_reason") == "budget", done[0] if done else None)
        check("it says which ceiling was hit",
              done and (done[0].get("budget_used") or {}).get("limit") == "tool_calls",
              done[0].get("budget_used") if done else None)
        # Every announced call gets an answer (a provider rejects a tool call
        # without a result), so there are two results - but only one of them
        # is a tool that actually ran.
        ran = [r for r in results if "budget" not in str(r.get("output") or "")]
        check("only one tool ran", len(ran) == 1, len(ran))
        check("the call that did not run says so",
              results and "budget" in str(results[-1].get("output") or ""),
              str(results[-1].get("output"))[:160] if results else None)

        # ---- a request may not loosen the ceiling -------------------------
        sid = new_session("loosen")
        events = run_turn(port, wid, sid, "go", limits={"max_iterations": 100000})
        done = [e for e in events if e.get("type") == "done"]
        check("a request cannot raise the machine's ceiling",
              done and done[0].get("iterations") == DEFAULT_MAX_ITERATIONS,
              done[0].get("iterations") if done else None)

        # ---- a budget nobody knows is an error ----------------------------
        sid = new_session("unknown")
        status, body = call(port, "POST", "/api/chat",
                            {"workspace_id": wid, "session_id": sid, "message": "go",
                             "limits": {"max_lunch": 3}})
        check("an unknown budget key is refused", status == 400, (status, body))
        check("the error names the key",
              status == 400 and "max_lunch" in json.dumps(body, ensure_ascii=False),
              body)
        status, body = call(port, "POST", "/api/chat",
                            {"workspace_id": wid, "session_id": sid, "message": "go",
                             "limits": {"max_iterations": "many"}})
        check("a budget that is not a number is refused", status == 400, (status, body))

        # ---- the trajectory knows the task and the budget ------------------
        sid = new_session("trajectory")
        events = run_turn(port, wid, sid, "go", limits={"max_iterations": 2},
                          task_id="t_deadbeef")
        _, turns = call(port, "GET", "/api/sessions/%s/trajectory" % sid)
        rows = turns if isinstance(turns, list) else []
        check("the trajectory records the turn", bool(rows), rows)
        check("the trajectory knows the task",
              rows and rows[0].get("task_id") == "t_deadbeef", rows[0] if rows else None)
        check("the trajectory says how the turn ended",
              rows and rows[0].get("stop_reason") == "budget", rows[0] if rows else None)
        _, full = call(port, "GET", "/api/sessions/%s/trajectory?full=1" % sid)
        rows_full = full if isinstance(full, list) else []
        check("the full record carries the budget",
              rows_full and (rows_full[0].get("budget") or {}).get("max_iterations") == 2,
              rows_full[0].get("budget") if rows_full else None)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    # ---- the settings can lower the ceiling -------------------------------
    tmp2 = tempfile.mkdtemp(prefix="tea_budget_cfg_")
    port2 = free_port()
    proc2, ws2, run2, log2 = start_agent(binary, tmp2, mock_port, port2,
                                          extra_settings="  max_iterations: 2\n")
    try:
        if not wait_health(port2):
            print("the second server did not become healthy")
            return 1
        _, health = call(port2, "GET", "/api/health")
        limits = health.get("limits") or {}
        check("provider.yaml's settings block can lower the ceiling",
              limits.get("max_iterations") == 2, limits)
        _, listing = call(port2, "GET", "/api/workspaces")
        wid2 = listing["workspaces"][0]["id"]
        _, sess = call(port2, "POST", "/api/workspaces/%s/sessions" % wid2, {"title": "cfg"})
        events = run_turn(port2, wid2, sess["id"], "go")
        done = [e for e in events if e.get("type") == "done"]
        check("the turn runs with the configured ceiling",
              done and done[0].get("iterations") == 2 and
              done[0].get("stop_reason") == "budget",
              done[0] if done else None)
    finally:
        proc2.terminate()
        try:
            proc2.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc2.kill()
        mock.shutdown()

    print("")
    print("%d/%d checks passed" % (len(checks) - len(failures), len(checks)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())