#!/usr/bin/env python3
"""Smoke test for the fleet target: a task that runs on the devices.

What this pins:

  * a task with target=fleet does NOT go to the local agent - the devices'
    own agents answer, and the queue records their answers as the result;
  * the selection (which devices) and the prompt reach the fleet service;
  * a device that failed is in the result too (silently dropping it would
    hide a broken device);
  * a task without the target still runs locally, as before.

Usage: python3 tests/smoke_fleet_tasks.py [path-to-tea_agent-binary]
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
SERVER = os.path.join(REPO_ROOT, "sched", "task_server.py")


class MockAgent(BaseHTTPRequestHandler):
    """A fake agent: it answers the health check and finishes a turn."""

    records = {"chat": 0}
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

    def do_GET(self):  # noqa: N802 (http.server API)
        if self.path == "/api/health":
            return self._json({"ok": True, "version": "mock"})
        if self.path == "/api/workspaces":
            return self._json({"workspaces": [{"id": "ws_mock", "path": MockAgent.ws_dir,
                                               "name": "mock"}]})
        if self.path.startswith("/api/providers/"):
            return self._json({"window": {"remaining_percent": 95.0}})
        return self._json({"error": "unknown path " + self.path}, 404)

    def do_POST(self):  # noqa: N802 (http.server API)
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length) if length else b""
        if self.path == "/api/chat":
            MockAgent.records["chat"] += 1
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for frame in ({"type": "start", "session_id": "sess_mock"},
                          {"type": "done", "content": "本机做完了"}):
                try:
                    self.wfile.write(("data: " + json.dumps(frame) + "\n\n").encode())
                    self.wfile.flush()
                except OSError:
                    return
            return
        if self.path == "/api/workspaces":
            return self._json({"id": "ws_mock"})
        return self._json({"error": "unknown path " + self.path}, 404)


class MockFleet(BaseHTTPRequestHandler):
    """A fake fleet service: the devices' own agents answer."""

    records = []

    def log_message(self, *args):
        pass

    def _json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802 (http.server API)
        return self._json({"code": 0, "message": "ok", "data": {"devices": []}})

    def do_POST(self):  # noqa: N802 (http.server API)
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            body = {}
        MockFleet.records.append({"body": body, "auth": self.headers.get("Authorization")})
        if self.path == "/api/fleet/ask":
            return self._json({"code": 0, "message": "ok", "data": {
                "devices": [
                    {"device": "class-a-01", "ok": True,
                     "content": "我这边重启过了，服务正常。"},
                    {"device": "class-a-02", "ok": False,
                     "error": "the device agent did not answer"},
                ],
                "answered": 1,
            }})
        return self._json({"code": 40401, "message": "no such route", "data": {}}, 404)


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
    headers = {"Content-Type": "application/json"} if data else {}
    conn.request(method, path, body=data, headers=headers)
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


def wait_until(predicate, timeout=40.0, interval=0.4):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if predicate():
                return True
        except Exception:  # noqa: BLE001 - a polling helper must not explode
            pass
        time.sleep(interval)
    return False


def task_of(port, task_id):
    status, body = call(port, "GET", "/api/tasks/" + task_id)
    if status != 200:
        return {}
    return (body or {}).get("data") or {}


def main():
    binary = (
        sys.argv[1] if len(sys.argv) > 1 else os.path.join(REPO_ROOT, "build", "bin", "tea_agent")
    )
    if not os.path.exists(binary):
        print("binary not found:", binary)
        return 1

    tmp = tempfile.mkdtemp(prefix="tea_fleet_tasks_")
    ws_dir = os.path.join(tmp, "ws")
    os.makedirs(ws_dir)
    MockAgent.ws_dir = ws_dir

    agent_port = free_port()
    fleet_port = free_port()
    queue_port = free_port()
    agent = ThreadingHTTPServer(("127.0.0.1", agent_port), MockAgent)
    threading.Thread(target=agent.serve_forever, daemon=True).start()
    fleet = ThreadingHTTPServer(("127.0.0.1", fleet_port), MockFleet)
    threading.Thread(target=fleet.serve_forever, daemon=True).start()

    queue = subprocess.Popen(
        [
            sys.executable,
            SERVER,
            "--agent",
            "http://127.0.0.1:%d" % agent_port,
            "--host",
            "127.0.0.1",
            "--port",
            str(queue_port),
            "--provider",
            "mock",
            "--data-dir",
            os.path.join(tmp, "tasks"),
            "--workspace",
            "ws_mock",
            "--fleet-url",
            "http://127.0.0.1:%d" % fleet_port,
            "--fleet-token",
            "fleet-secret",
            "--interval",
            "1",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    failures = []
    try:
        if not wait_health(queue_port):
            print("the queue did not become healthy")
            return 1

        # ---- a task on the devices ----------------------------------------
        status, body = call(queue_port, "POST", "/api/tasks", {
            "prompt": "把服务重启一下",
            "target": "fleet",
            "fleet": {"names": ["class-a-01", "class-a-02"], "timeout_s": 30},
        })
        if status not in (200, 201):
            failures.append("create: HTTP %s %r" % (status, body))
        task_id = ((body or {}).get("data") or {}).get("id") or ""
        if not task_id:
            failures.append("create: no task id in %r" % (body,))
            return 1

        def finished():
            return task_of(queue_port, task_id).get("status") in ("done", "failed")

        if not wait_until(finished, 40):
            failures.append("the fleet task did not finish: %r" % task_of(queue_port, task_id))
        else:
            task = task_of(queue_port, task_id)
            if task.get("status") != "done":
                failures.append("the fleet task failed: %r" % task)
            result = task.get("result") or ""
            if "class-a-01" not in result or "重启过了" not in result:
                failures.append("the answer of the device is not in the result: %r" % result[:200])
            if "class-a-02" not in result:
                failures.append("the device that failed is not in the result: %r" % result[:200])

        if not MockFleet.records:
            failures.append("the fleet service was never asked")
        else:
            sent = MockFleet.records[-1]
            if "重启" not in (sent["body"].get("prompt") or ""):
                failures.append("the prompt did not reach the fleet: %r" % sent["body"])
            if sent["body"].get("names") != ["class-a-01", "class-a-02"]:
                failures.append("the device selection did not reach the fleet: %r" % sent["body"])
            if sent["auth"] != "Bearer fleet-secret":
                failures.append("the fleet token was not sent: %r" % sent["auth"])

        # the local agent was not asked: the work ran on the devices
        if MockAgent.records["chat"]:
            failures.append("the local agent was asked although the task was for the fleet")

        # ---- a task without the target still runs locally -------------------
        status, body = call(queue_port, "POST", "/api/tasks", {"prompt": "本机做一件事"})
        local_id = ((body or {}).get("data") or {}).get("id") or ""
        if not wait_until(lambda: task_of(queue_port, local_id).get("status") in ("done", "failed"),
                          40):
            failures.append("the local task did not finish")
        elif "本机做完了" not in (task_of(queue_port, local_id).get("result") or ""):
            failures.append("the local task did not run on the local agent: %r"
                            % task_of(queue_port, local_id).get("result"))
    finally:
        queue.terminate()
        try:
            queue.wait(timeout=10)
        except subprocess.TimeoutExpired:
            queue.kill()
        for server in (agent, fleet):
            try:
                server.shutdown()
            except Exception:  # noqa: BLE001
                pass
        shutil.rmtree(tmp, ignore_errors=True)

    if failures:
        print("FAIL")
        for f in failures:
            print(" -", f)
        return 1
    print("PASS: a task can run on the devices' own agents")
    return 0


if __name__ == "__main__":
    sys.exit(main())
