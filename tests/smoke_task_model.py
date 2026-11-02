#!/usr/bin/env python3
"""Smoke test for `task_model:` in provider.yaml.

The operator wants one line in provider.yaml to say which provider and model
the **queued** work runs on:

    task_model: minimax+MiniMax-M3.1-Flash

What this pins:

  * the agent (which owns provider.yaml) parses the key and reports it in
    GET /api/health, next to its own default (the two are different things);
  * a broken value does not stop the agent: it warns and carries on;
  * the queue takes the provider and the model from it - the queue's own
    default (the first provider of the file) is NOT what the queued work
    uses;
  * the quota gate asks the provider that actually runs the work;
  * a task that names its own model still wins.

Usage: python3 tests/smoke_task_model.py [path-to-tea_agent-binary]
"""

import http.client
import json
import os
import random
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(REPO_ROOT, "sched", "task_server.py")

PROVIDER_YAML = """\
providers:
  mock:
    api_url: http://127.0.0.1:9/v1
    api_key: sk-test
    models:
      mock-fast:
        max_context_tokens: 128000
        supports_tools: true
      mock-slow:
        max_context_tokens: 128000
        supports_tools: true

task_model: mock+mock-slow
"""


class MockAgent(BaseHTTPRequestHandler):
    """A fake agent that answers the health check with a task_model."""

    task_model = "mock+mock-slow"
    records = {"chat": 0, "models": [], "usage": 0}
    ws_dir = ""

    def log_message(self, *args):
        pass

    def _json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path == "/api/health":
            out = {"ok": True, "version": "mock", "provider": "mock", "model": "mock-fast"}
            if MockAgent.task_model:
                provider, _, model = MockAgent.task_model.partition("+")
                out["task_model"] = MockAgent.task_model
                out["task_provider"] = provider
                out["task_model_name"] = model
            return self._json(out)
        if self.path == "/api/workspaces":
            return self._json({"workspaces": [{"id": "ws_mock", "path": MockAgent.ws_dir,
                                               "name": "mock"}]})
        if self.path.startswith("/api/providers/"):
            if self.path.endswith("/models"):
                return self._json({"provider": "mock",
                                   "current": [{"id": "mock-fast", "in_config": True}],
                                   "models": [{"id": "mock-fast"}]})
            MockAgent.records["usage"] += 1
            return self._json({"window": {"remaining_percent": 95.0}})
        return self._json({"error": "unknown path " + self.path}, 404)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length) if length else b""
        if self.path == "/api/chat":
            MockAgent.records["chat"] += 1
            try:
                body = json.loads(raw or b"{}")
            except ValueError:
                body = {}
            MockAgent.records["models"].append(body.get("model") or "")
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for frame in ({"type": "start", "session_id": "sess_mock"},
                          {"type": "done", "content": "做完了"}):
                try:
                    self.wfile.write(("data: " + json.dumps(frame) + "\n\n").encode())
                    self.wfile.flush()
                except OSError:
                    return
            return
        if self.path == "/api/llm/chat":
            return self._json({"ok": True, "content": "好", "reasoning": "",
                               "finish_reason": "stop"})
        return self._json({"error": "unknown path " + self.path}, 404)


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
    data = json.dumps(body).encode() if body is not None else None
    head = {"Content-Type": "application/json"} if data else {}
    conn.request(method, path, body=data, headers=head)
    resp = conn.getresponse()
    raw = resp.read().decode("utf-8", "replace")
    conn.close()
    try:
        return resp.status, json.loads(raw)
    except ValueError:
        return resp.status, {"raw": raw}


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


def main():
    binary = (
        sys.argv[1] if len(sys.argv) > 1 else os.path.join(REPO_ROOT, "build", "bin", "tea_agent")
    )
    if not os.path.exists(binary):
        print("binary not found:", binary)
        return 1

    failures = []

    def check(name, ok, detail=""):
        print(("PASS " if ok else "FAIL ") + name + (("  " + str(detail)) if not ok else ""))
        if not ok:
            failures.append(name)

    tmp = tempfile.mkdtemp(prefix="tea_taskmodel_")
    try:
        # ---- the agent parses the key ------------------------------------
        home = os.path.join(tmp, "home")
        os.makedirs(home)
        with open(os.path.join(home, "provider.yaml"), "w", encoding="utf-8") as f:
            f.write(PROVIDER_YAML)
        port = free_port()
        agent = subprocess.Popen(
            [binary, "--port", str(port)],
            env=dict(os.environ, TEA_AGENT_HOME=home),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            if not wait_health(port):
                check("the agent starts with a task_model", False)
            else:
                status, body = call(port, "GET", "/api/health")
                check("the agent reports the task_model",
                      body.get("task_model") == "mock+mock-slow", body.get("task_model"))
                check("the agent reports both halves",
                      body.get("task_provider") == "mock"
                      and body.get("task_model_name") == "mock-slow", body)
                check("the agent's own default is a different thing",
                      body.get("provider") == "mock" and body.get("model") == "mock-fast", body)
        finally:
            agent.terminate()
            try:
                agent.wait(timeout=10)
            except subprocess.TimeoutExpired:
                agent.kill()

        # ---- a broken value does not stop the agent ----------------------
        with open(os.path.join(home, "provider.yaml"), "w", encoding="utf-8") as f:
            f.write(PROVIDER_YAML.replace("task_model: mock+mock-slow", "task_model: nonsense"))
        port2 = free_port()
        agent2 = subprocess.Popen(
            [binary, "--port", str(port2)],
            env=dict(os.environ, TEA_AGENT_HOME=home),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            up = wait_health(port2)
            check("a broken task_model does not stop the agent", up)
            if up:
                status, body = call(port2, "GET", "/api/health")
                check("a broken task_model is not used", not body.get("task_model"), body)
        finally:
            agent2.terminate()
            try:
                agent2.wait(timeout=10)
            except subprocess.TimeoutExpired:
                agent2.kill()

        # ---- the queue takes provider and model from it ------------------
        ws_dir = os.path.join(tmp, "ws")
        os.makedirs(ws_dir)
        MockAgent.ws_dir = ws_dir
        agent_port = free_port()
        queue_port = free_port()
        mock = ThreadingHTTPServer(("127.0.0.1", agent_port), MockAgent)
        threading.Thread(target=mock.serve_forever, daemon=True).start()

        queue = subprocess.Popen(
            [sys.executable, SERVER,
             "--agent", "http://127.0.0.1:%d" % agent_port,
             "--host", "127.0.0.1", "--port", str(queue_port),
             "--data-dir", os.path.join(tmp, "tasks"),
             "--workspace", "ws_mock", "--interval", "1"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            if not wait_health(queue_port):
                print("the queue did not become healthy")
                return 1
            status, body = call(queue_port, "GET", "/api/health")
            data = (body or {}).get("data") or {}
            check("the queue runs on the provider of the task_model",
                  data.get("provider") == "mock", data.get("provider"))
            check("the queue runs on the model of the task_model",
                  data.get("model") == "mock-slow", data.get("model"))
            check("the quota gate asks that provider",
                  data.get("quota_percent") == 95.0, data.get("quota_percent"))

            # ---- a task without a model uses the default ------------------
            status, body = call(queue_port, "POST", "/api/tasks", {"prompt": "第一步"})
            task_id = ((body or {}).get("data") or {}).get("id")
            deadline = time.time() + 60
            while time.time() < deadline:
                status, body = call(queue_port, "GET", "/api/tasks/" + str(task_id))
                task = (body or {}).get("data") or {}
                if task.get("status") in ("done", "failed", "cancelled"):
                    break
                time.sleep(0.3)
            check("a task without a model runs on the default model",
                  "mock-slow" in MockAgent.records["models"],
                  MockAgent.records["models"][-3:])

            # ---- a task with its own model wins --------------------------
            status, body = call(queue_port, "POST", "/api/tasks",
                                 {"prompt": "第二步", "model": "mock-fast"})
            task_id = ((body or {}).get("data") or {}).get("id")
            deadline = time.time() + 60
            while time.time() < deadline:
                status, body = call(queue_port, "GET", "/api/tasks/" + str(task_id))
                task = (body or {}).get("data") or {}
                if task.get("status") in ("done", "failed", "cancelled"):
                    break
                time.sleep(0.3)
            check("a task with its own model wins",
                  MockAgent.records["models"][-1] == "mock-fast",
                  MockAgent.records["models"][-3:])
        finally:
            queue.terminate()
            try:
                queue.wait(timeout=10)
            except subprocess.TimeoutExpired:
                queue.kill()
            try:
                mock.shutdown()
            except Exception:  # noqa: BLE001
                pass
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if failures:
        print("FAIL")
        for f in failures:
            print(" -", f)
        return 1
    print("PASS: task_model says which provider and model the queued work runs on")
    return 0


if __name__ == "__main__":
    sys.exit(main())
