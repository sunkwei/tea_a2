#!/usr/bin/env python3
"""Smoke test for the watchdog (sched/watchdog.py) and the queue's new levers.

What this pins:

  * the queue can be paused and resumed, and a paused queue starts NOTHING
    (the pause survives a restart of the service: it lives in a file);
  * a task that stops making progress is noticed by the watchdog - and by
    default the watchdog only SAYS SO: the task is still running afterwards;
  * with --auto the watchdog may act, and then the task ends up failed with
    a reason (and the reason is in the audit);
  * an agent that is not there is noticed;
  * the alert reaches the queue's event stream (that is the road to the
    person who queued the work: the chat front ends listen to it).

Usage: python3 tests/smoke_watchdog.py [path-to-tea_agent-binary]
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
WATCHDOG = os.path.join(REPO_ROOT, "sched", "watchdog.py")


class MockAgent(BaseHTTPRequestHandler):
    """An agent whose turn never finishes on its own.

    That is the point: the task stays "running" and makes no progress, which
    is exactly the situation the watchdog has to notice.
    """

    stopped = False
    ws_dir = ""
    records = {"chat": 0, "stop": 0, "usage": 0}

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
            return self._json({"ok": True, "version": "mock", "provider": "mock",
                               "model": "mock"})
        if self.path == "/api/workspaces":
            return self._json({"workspaces": [{"id": "ws_mock", "path": MockAgent.ws_dir,
                                               "name": "mock"}]})
        if self.path.startswith("/api/providers/"):
            MockAgent.records["usage"] += 1
            return self._json({"window": {"remaining_percent": 95.0}})
        return self._json({"error": "unknown path " + self.path}, 404)

    def do_POST(self):  # noqa: N802 (http.server API)
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length:
            self.rfile.read(length)
        if self.path == "/api/chat":
            MockAgent.records["chat"] += 1
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            frame = {"type": "start", "session_id": "sess_mock", "workspace_id": "ws_mock"}
            try:
                self.wfile.write(("data: " + json.dumps(frame) + "\n\n").encode())
                self.wfile.flush()
            except OSError:
                return
            deadline = time.time() + 120
            while not MockAgent.stopped and time.time() < deadline:
                time.sleep(0.2)
            return
        if self.path == "/api/chat/stop":
            MockAgent.stopped = True
            MockAgent.records["stop"] += 1
            return self._json({"ok": True})
        if self.path == "/api/workspaces":
            return self._json({"id": "ws_mock"})
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


def call(port, method, path, body=None, timeout=20):
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


def wait_until(predicate, timeout=30.0, interval=0.3):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if predicate():
                return True
        except Exception:  # noqa: BLE001 - a polling helper must not explode
            pass
        time.sleep(interval)
    return False


def read_audit(path):
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return out


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
    if not os.path.exists(WATCHDOG):
        print("watchdog not found:", WATCHDOG)
        return 1

    tmp = tempfile.mkdtemp(prefix="tea_watchdog_")
    ws_dir = os.path.join(tmp, "ws")
    os.makedirs(ws_dir)
    MockAgent.ws_dir = ws_dir

    agent_port = free_port()
    queue_port = free_port()
    agent = ThreadingHTTPServer(("127.0.0.1", agent_port), MockAgent)
    threading.Thread(target=agent.serve_forever, daemon=True).start()

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
            "--interval",
            "1",
            "--max-per-round",
            "1",
            "--chat-timeout",
            "120",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    failures = []
    watchdog = None
    try:
        if not wait_health(queue_port):
            print("the queue did not become healthy")
            return 1

        # ---- the queue can be paused and resumed -------------------------
        status, body = call(queue_port, "POST", "/api/queue/pause", {"reason": "smoke test"})
        if status != 200:
            failures.append("pause: HTTP %s %s" % (status, body))
        state = (body or {}).get("data") or {}
        if not state.get("paused") or state.get("reason") != "smoke test":
            failures.append("pause: the queue does not say it is paused: %r" % state)

        status, body = call(queue_port, "GET", "/api/queue/state")
        if status != 200 or not (body or {}).get("data", {}).get("paused"):
            failures.append("queue state: %s %r" % (status, body))

        # A paused queue starts nothing.
        status, body = call(queue_port, "POST", "/api/tasks", {"prompt": "paused work"})
        if status not in (200, 201):
            failures.append("create: HTTP %s %r" % (status, body))
        task_id = ((body or {}).get("data") or {}).get("id") or ""
        if not task_id:
            failures.append("create: no task id in %r" % (body,))
            return 1
        time.sleep(3)
        task = task_of(queue_port, task_id)
        if task.get("status") != "queued":
            failures.append("a paused queue started the task anyway: %r" % task.get("status"))
        if MockAgent.records["chat"]:
            failures.append("a paused queue talked to the agent: %d chat request(s)"
                            % MockAgent.records["chat"])

        status, body = call(queue_port, "POST", "/api/queue/resume", {})
        if status != 200:
            failures.append("resume: HTTP %s %r" % (status, body))
        if not wait_until(lambda: task_of(queue_port, task_id).get("status") == "running", 20):
            failures.append("the task did not start after the resume: %r"
                            % task_of(queue_port, task_id).get("status"))

        # ---- the watchdog notices a task that makes no progress ----------
        watchdog_dir = os.path.join(tmp, "watchdog")
        audit_path = os.path.join(watchdog_dir, "audit.jsonl")
        watchdog = subprocess.Popen(
            [
                sys.executable,
                WATCHDOG,
                "--tasks",
                "http://127.0.0.1:%d" % queue_port,
                "--agent",
                "http://127.0.0.1:%d" % agent_port,
                "--stall-after",
                "0.05",          # three seconds
                "--interval",
                "1",
                "--data-dir",
                watchdog_dir,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        def stalled_alert():
            for entry in read_audit(audit_path):
                if entry.get("check") == "stalled_task":
                    return True
            return False

        if not wait_until(stalled_alert, 40):
            failures.append("the watchdog did not notice the stuck task")
        else:
            # By default the watchdog only says so: the task is untouched.
            task = task_of(queue_port, task_id)
            if task.get("status") != "running":
                failures.append("the watchdog ended the task although it only had to say so: %r"
                                % task.get("status"))
            # The alert went into the queue's event stream - that is the road
            # to the person who queued the work.
            status, body = call(queue_port, "GET", "/api/tasks/%s/events" % task_id)
            events = ((body or {}).get("data") or {}).get("events") or []
            alerts = [e for e in events if (e.get("payload") or {}).get("type") == "watchdog_alert"]
            if not alerts:
                failures.append("no watchdog_alert in the task's events: %r" % events)
            else:
                payload = alerts[-1].get("payload") or {}
                if payload.get("check") != "stalled_task" or not payload.get("reason"):
                    failures.append("the alert does not say what happened: %r" % payload)

        # ---- with --auto the watchdog may act ---------------------------
        watchdog.terminate()
        watchdog.wait(timeout=10)
        watchdog = subprocess.Popen(
            [
                sys.executable,
                WATCHDOG,
                "--tasks",
                "http://127.0.0.1:%d" % queue_port,
                "--agent",
                "http://127.0.0.1:%d" % agent_port,
                "--stall-after",
                "0.05",
                "--interval",
                "1",
                "--auto",
                "--action",
                "fail",
                "--data-dir",
                os.path.join(tmp, "watchdog_auto"),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        def task_failed():
            return task_of(queue_port, task_id).get("status") == "failed"

        if not wait_until(task_failed, 40):
            failures.append("--auto did not fail the stuck task: %r"
                            % task_of(queue_port, task_id).get("status"))
        else:
            task = task_of(queue_port, task_id)
            if not (task.get("error") or ""):
                failures.append("the failed task has no reason: %r" % task)
            auto_audit = read_audit(os.path.join(tmp, "watchdog_auto", "audit.jsonl"))
            if not [e for e in auto_audit if e.get("action") == "fail"]:
                failures.append("the action is not in the audit: %r" % auto_audit)

        # ---- the chat front end pushes the alert to the person ---------
        # The event stream is the road to the user; the front end is what
        # walks that road. Check the shared half (sched/channel.py) with a
        # fake transport: no network, no bot.
        sys.path.insert(0, os.path.join(REPO_ROOT, "sched"))
        import channel  # noqa: E402  (the path is only known here)

        class FakeChannel(channel.TaskChannel):
            def __init__(self):  # nothing that would touch the network
                self.args = type("Args", (), {"tasks": "", "agent": "",
                                              "workspace_path": "", "workspace_name": ""})()
                self.queue = None
                self.agent = None
                self.workspace_id = "ws_mock"
                self.owner = {"t_other": "u_other"}
                self.sent = []

            def send_text(self, user_id, text):
                self.sent.append((user_id, text))
                return True

        front = FakeChannel()
        alert = {"type": "watchdog_alert", "check": "stalled_task", "level": "warn",
                 "reason": "no progress for 3 minutes",
                 "task": {"id": task_id, "workspace_id": "ws_mock",
                          "meta": {"user_id": "u_owner"}}}
        if not front.notify_event(alert):
            failures.append("the chat front end did not pass the watchdog alert on")
        elif not front.sent or front.sent[0][0] != "u_owner":
            failures.append("the alert went to the wrong person: %r" % (front.sent,))
        elif "no progress" not in front.sent[0][1]:
            failures.append("the alert does not say what happened: %r" % (front.sent,))

        # An alert without a task is about the whole machine: everybody who
        # ever talked to the bot hears about it.
        front.sent = []
        front.notify_event({"type": "watchdog_alert", "check": "agent_down",
                             "level": "error", "reason": "the agent does not answer"})
        if not front.sent:
            failures.append("a machine-wide alert reached nobody")

        # ---- an agent that is not there is noticed ----------------------
        agent.shutdown()
        agent.server_close()

        def agent_down():
            for entry in read_audit(os.path.join(tmp, "watchdog_auto", "audit.jsonl")):
                if entry.get("check") == "agent_down":
                    return True
            return False

        if not wait_until(agent_down, 30):
            failures.append("the watchdog did not notice that the agent is gone")
    finally:
        if watchdog is not None:
            watchdog.terminate()
            try:
                watchdog.wait(timeout=10)
            except subprocess.TimeoutExpired:
                watchdog.kill()
        queue.terminate()
        try:
            queue.wait(timeout=10)
        except subprocess.TimeoutExpired:
            queue.kill()
        try:
            agent.shutdown()
        except Exception:  # noqa: BLE001
            pass
        shutil.rmtree(tmp, ignore_errors=True)

    if failures:
        print("FAIL")
        for f in failures:
            print(" -", f)
        return 1
    print("PASS: the watchdog watches the queue, the agent and the stuck tasks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
