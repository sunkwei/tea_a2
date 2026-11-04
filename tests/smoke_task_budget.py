#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Smoke test for the queue's side of the mechanism layer.

The queue is the **strategy layer**: it decides what to work on, when, and
with which budget and permissions. The agent is the **mechanism layer**: it
enforces the budget, refuses the tools it was not given, and reports back
what it used.

What this pins:

  * a task may carry a budget (`limits`) and permissions (`policy`); a key
    nobody knows is refused at the door, not after a wait in the queue;
  * the queue hands the budget and the permissions to the agent with the turn;
  * the queue fills a task that has no budget of its own with the agent's
    budget (the mechanism layer's word), so the record says what the task ran
    under;
  * a turn that runs out of budget ends **normally**: the task is done, and
    the record says `stop_reason: "budget"` - not a failure;
  * a tool the task was not allowed to use does not run.

Usage: python3 tests/smoke_task_budget.py [path-to-tea_agent-binary]
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
SERVER = os.path.join(REPO_ROOT, "sched", "task_server.py")

# The machine's ceiling (provider.yaml's settings block).
MACHINE_MAX_ITERATIONS = 3


class Provider(BaseHTTPRequestHandler):
    """A model that calls a tool for ever, plus the quota endpoint."""

    def log_message(self, *args):
        pass

    def _json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802 (http.server API)
        if "/token_plan/remains" in self.path:
            row = {
                "model_name": "general",
                "current_interval_remaining_percent": 100.0,
                "current_weekly_remaining_percent": 100.0,
                "start_time": 0, "end_time": 0, "remains_time": 3600000,
                "weekly_start_time": 0, "weekly_end_time": 0,
                "weekly_remains_time": 86400000,
                "current_interval_status": 1, "current_weekly_status": 1,
            }
            self._json(200, {"base_resp": {"status_code": 0, "status_msg": "success"},
                             "model_remains": [row]})
            return
        self._json(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802 (http.server API)
        if not self.path.endswith("/chat/completions"):
            self._json(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            payload = {}
        msgs = payload.get("messages") or []
        done_tools = len([m for m in msgs if m.get("role") == "tool"])
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        # A tool call, every time: the turn only ends when the budget says so.
        cid = "c%d" % done_tools
        chunk = {
            "id": "c",
            "choices": [{
                "index": 0,
                "delta": {
                    "role": "assistant",
                    "tool_calls": [{
                        "index": 0, "id": cid, "type": "function",
                        "function": {"name": "toolkit_exec",
                                     "arguments": json.dumps({"command": "echo %d" % done_tools})},
                    }],
                },
                "finish_reason": None,
            }],
        }
        try:
            self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
            self.wfile.write(("data: " + json.dumps(
                {"id": "c", "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]}
            ) + "\n\n").encode())
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


def call(port, method, path, body=None, timeout=60, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    payload = None
    head = {}
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
        head["Content-Type"] = "application/json"
    head.update(headers or {})
    conn.request(method, path, body=payload, headers=head)
    resp = conn.getresponse()
    raw = resp.read()
    try:
        return resp.status, json.loads(raw or b"null")
    except ValueError:
        return resp.status, {"raw": raw.decode("utf-8", "replace")}


def wait_task_status(port, task_id, wanted, timeout=90.0):
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        status, body = call(port, "GET", "/api/tasks/%s" % task_id)
        if status == 200:
            data = body.get("data") or body
            task = data.get("task") or data
            last = task
            if task.get("status") in wanted:
                return task
        time.sleep(0.3)
    return last


def main():
    binary = (sys.argv[1] if len(sys.argv) > 1
              else os.path.join(REPO_ROOT, "build", "bin", "tea_agent"))
    if not os.path.exists(binary):
        print("binary not found:", binary)
        return 1
    if not os.path.exists(SERVER):
        print("task server not found:", SERVER)
        return 1

    provider = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    provider.daemon_threads = True
    threading.Thread(target=provider.serve_forever, daemon=True).start()
    provider_port = provider.server_address[1]

    tmp = tempfile.mkdtemp(prefix="tea_task_budget_")
    home = os.path.join(tmp, "home")
    ws_dir = os.path.join(tmp, "ws")
    data_dir = os.path.join(tmp, "tasks")
    os.makedirs(home)
    os.makedirs(ws_dir)
    config_path = os.path.join(home, "provider.yaml")
    with open(config_path, "w", encoding="utf-8") as f:
        f.write("""version: 1
providers:
  minimax:
    api_url: http://127.0.0.1:%d/v1
    api_key: sk-cp-test
    supports_usage: true
    usage_type: minimax_token_plan
    usage_url: http://127.0.0.1:%d/v1/token_plan/remains
    models:
      MiniMax-M3.1-Flash:
        max_context_tokens: 1000000
        max_output_tokens: 128000
        supports_tools: true
settings:
  max_iterations: %d
""" % (provider_port, provider_port, MACHINE_MAX_ITERATIONS))

    agent_port = free_port()
    task_port = free_port()
    agent = subprocess.Popen([
        binary, "--tea-home", home, "--host", "127.0.0.1", "--port", str(agent_port),
        "--no-console", "--log-file", os.path.join(home, "logs", "tea_agent.log"),
        "--workspace", ws_dir,
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    task_ws_path = os.path.join(tmp, "task-workspace")
    server_log = open(os.path.join(tmp, "server.log"), "w")
    task_server = subprocess.Popen([
        sys.executable, SERVER, "--agent", "http://127.0.0.1:%d" % agent_port,
        "--host", "127.0.0.1", "--port", str(task_port), "--data-dir", data_dir,
        "--provider", "minimax", "--interval", "2", "--max-per-round", "4",
        "--workspace-path", task_ws_path,
    ], stdout=server_log, stderr=subprocess.STDOUT, universal_newlines=True)

    failures = []
    checks = []

    def check(name, ok, detail=""):
        checks.append(name)
        print(("PASS " if ok else "FAIL ") + name + (("  " + str(detail)) if not ok else ""))
        if not ok:
            failures.append(name)

    try:
        if not wait_health(agent_port):
            print("agent did not become healthy")
            return 1
        if not wait_health(task_port):
            print("task server did not become healthy")
            return 1
        _, listing = call(agent_port, "GET", "/api/workspaces")
        ws_id = listing["workspaces"][0]["id"]

        # ---- the machine's budget ------------------------------------------
        _, health = call(agent_port, "GET", "/api/health")
        machine = health.get("limits") or {}
        check("the agent has a budget", machine.get("max_iterations") == MACHINE_MAX_ITERATIONS,
              machine)

        # ---- a task may carry a budget and permissions ----------------------
        status, body = call(task_port, "POST", "/api/tasks", {
            "workspace_id": ws_id, "prompt": "算到预算为止",
            "limits": {"max_iterations": 2},
            "policy": {"tools": ["toolkit_exec", "toolkit_task"]},
        })
        task = (body.get("data") or body).get("task") or body.get("data") or {}
        task_id = task.get("id") or ""
        check("a task with a budget is accepted", status == 200 and bool(task_id), (status, body))
        check("the task remembers its budget",
              (task.get("limits") or {}).get("max_iterations") == 2, task.get("limits"))
        check("the task remembers its permissions",
              "toolkit_exec" in ((task.get("policy") or {}).get("tools") or []),
              task.get("policy"))

        # ---- a budget nobody knows is refused ------------------------------
        status, body = call(task_port, "POST", "/api/tasks", {
            "workspace_id": ws_id, "prompt": "x", "limits": {"max_lunch": 2}})
        check("an unknown budget key is refused", status == 400, (status, body))
        status, body = call(task_port, "POST", "/api/tasks", {
            "workspace_id": ws_id, "prompt": "x", "policy": {"paths": ["/tmp"]}})
        check("an unknown permission key is refused", status == 400, (status, body))
        status, body = call(task_port, "POST", "/api/tasks", {
            "workspace_id": ws_id, "prompt": "x", "limits": {"max_iterations": "many"}})
        check("a budget that is not a number is refused", status == 400, (status, body))

        # ---- the turn runs with the budget and ends at it -------------------
        task = wait_task_status(task_port, task_id, ("done", "failed", "cancelled"))
        check("the task finished", task.get("status") == "done", task)
        check("the task says the budget stopped it",
              task.get("stop_reason") == "budget", task.get("stop_reason"))
        check("the task says which ceiling was hit",
              (task.get("budget_used") or {}).get("limit") == "iterations",
              task.get("budget_used"))
        check("the task ran with the budget it was given",
              (task.get("limits") or {}).get("max_iterations") == 2, task.get("limits"))
        check("the task is not a failure", not task.get("error"), task.get("error"))
        check("the note says the budget ran out",
              "预算" in str(task.get("note") or ""), task.get("note"))
        check("the task links to the turn's trajectory",
              bool(task.get("trajectory_id")), task.get("trajectory_id"))

        # ---- a task without a budget gets the machine's --------------------
        status, body = call(task_port, "POST", "/api/tasks", {
            "workspace_id": ws_id, "prompt": "用机器的预算"})
        plain = (body.get("data") or body).get("task") or body.get("data") or {}
        plain_id = plain.get("id") or ""
        plain = wait_task_status(task_port, plain_id, ("done", "failed", "cancelled"))
        check("the task ran with the machine's budget",
              (plain.get("limits") or {}).get("max_iterations") == MACHINE_MAX_ITERATIONS,
              plain.get("limits"))
        check("it ended at the machine's ceiling too",
              plain.get("stop_reason") == "budget", plain.get("stop_reason"))

        # ---- a tool the task may not use does not run ----------------------
        status, body = call(task_port, "POST", "/api/tasks", {
            "workspace_id": ws_id, "prompt": "只准用文件工具",
            "limits": {"max_iterations": 2},
            "policy": {"tools": ["toolkit_file"]},
        })
        guarded = (body.get("data") or body).get("task") or body.get("data") or {}
        guarded_id = guarded.get("id") or ""
        guarded = wait_task_status(task_port, guarded_id, ("done", "failed", "cancelled"))
        check("the guarded task finished", guarded.get("status") == "done", guarded)
        session_id = guarded.get("session_id") or ""
        refused = False
        if session_id:
            _, traj = call(agent_port, "GET",
                           "/api/sessions/%s/trajectory?full=1" % urllib_quote(session_id))
            for turn in (traj if isinstance(traj, list) else []):
                for step in (turn.get("steps") or []):
                    if "may not use the tool" in str(step.get("output") or ""):
                        refused = True
        check("the tool the task was not allowed to use did not run", refused,
              guarded.get("trajectory_id"))
    finally:
        task_server.terminate()
        agent.terminate()
        for proc in (task_server, agent):
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        provider.shutdown()

    print("")
    print("%d/%d checks passed" % (len(checks) - len(failures), len(checks)))
    return 1 if failures else 0


def urllib_quote(text):
    import urllib.parse
    return urllib.parse.quote(text)


if __name__ == "__main__":
    sys.exit(main())