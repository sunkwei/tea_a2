#!/usr/bin/env python3
"""Smoke test for the trigger entry (the queue's door for outside events).

What this pins:

  * the rules decide before the model is asked (the operator's word beats the
    model's, and the model call costs money);
  * when the rules do not settle it, the model decides - and its answer is
    what the trigger says;
  * the same dedup key means the same event: the second one does not queue
    the work again;
  * two triggers of the same conversation end up in the same thread;
  * the old way (POST /api/tasks) still works - the trigger entry is an
    added door, not a replaced one.

Usage: python3 tests/smoke_triggers.py [path-to-tea_agent-binary]
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
    """A fake agent: it answers the health check, the workspaces, the quota
    and - when it is asked - the question "is this worth doing?"."""

    answer = '{"decision": "accept", "reason": "这是一件要做的事"}'
    records = {"llm": 0, "chat": 0, "usage": 0}
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
            MockAgent.records["usage"] += 1
            return self._json({"window": {"remaining_percent": 95.0}})
        return self._json({"error": "unknown path " + self.path}, 404)

    def do_POST(self):  # noqa: N802 (http.server API)
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length) if length else b""
        if self.path == "/api/llm/chat":
            MockAgent.records["llm"] += 1
            try:
                body = json.loads(raw or b"{}")
            except ValueError:
                body = {}
            return self._json({"ok": True, "provider": "mock", "model": "mock",
                               "content": MockAgent.answer, "reasoning": "",
                               "finish_reason": "stop"})
        if self.path == "/api/chat":
            MockAgent.records["chat"] += 1
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


def main():
    binary = (
        sys.argv[1] if len(sys.argv) > 1 else os.path.join(REPO_ROOT, "build", "bin", "tea_agent")
    )
    if not os.path.exists(binary):
        print("binary not found:", binary)
        return 1

    tmp = tempfile.mkdtemp(prefix="tea_triggers_")
    ws_dir = os.path.join(tmp, "ws")
    os.makedirs(ws_dir)
    MockAgent.ws_dir = ws_dir

    # The rules: one source that is always work, one that never is.
    rules_path = os.path.join(tmp, "triggers.conf")
    with open(rules_path, "w", encoding="utf-8") as f:
        f.write("# 每个来源一段；/.../ 是正则\n"
                "[webhook]\n"
                "require = 部署\n"
                "ignore = 广告\n"
                "[noise]\n"
                "ignore = .*\n")

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
            "--trigger-config",
            rules_path,
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

        # ---- the rules decide, the model is not asked -------------------
        before = MockAgent.records["llm"]
        status, body = call(queue_port, "POST", "/api/triggers",
                             {"source": "webhook", "text": "把服务部署到新机器上",
                              "dedup_key": "deploy:1"})
        if status != 200:
            failures.append("submit: HTTP %s %r" % (status, body))
        trigger = (body or {}).get("data", {}).get("trigger") or {}
        if trigger.get("decision") != "accept":
            failures.append("the rule did not accept the trigger: %r" % trigger)
        if not trigger.get("task_id"):
            failures.append("an accepted trigger did not become a task: %r" % trigger)
        if MockAgent.records["llm"] != before:
            failures.append("the model was asked although the rules had settled it")

        # ---- the rules ignore it, again without the model ---------------
        status, body = call(queue_port, "POST", "/api/triggers",
                             {"source": "webhook", "text": "广告：双十一大促",
                              "dedup_key": "ad:1"})
        trigger = (body or {}).get("data", {}).get("trigger") or {}
        if trigger.get("decision") != "ignore":
            failures.append("the rule did not ignore the trigger: %r" % trigger)
        if trigger.get("task_id"):
            failures.append("an ignored trigger still became a task: %r" % trigger)
        if MockAgent.records["llm"] != before:
            failures.append("the model was asked although the rules had settled it")

        # ---- the model decides when the rules do not -------------------
        before = MockAgent.records["llm"]
        status, body = call(queue_port, "POST", "/api/triggers",
                             {"source": "mail", "text": "构建失败了：单元测试报错",
                              "dedup_key": "mail:1"})
        if MockAgent.records["llm"] != before + 1:
            failures.append("the model was not asked when the rules had nothing to say")
        trigger = (body or {}).get("data", {}).get("trigger") or {}
        if trigger.get("decision") != "accept":
            failures.append("the model's answer was not used: %r" % trigger)
        if not trigger.get("task_id"):
            failures.append("the trigger the model accepted did not become a task: %r" % trigger)
        if "要做的事" not in (trigger.get("reason") or ""):
            failures.append("the model's reason was not kept: %r" % trigger.get("reason"))

        # ---- the model says no -> no task ------------------------------
        MockAgent.answer = '{"decision": "ignore", "reason": "只是问候"}'
        status, body = call(queue_port, "POST", "/api/triggers",
                             {"source": "mail", "text": "早上好",
                              "dedup_key": "mail:2"})
        trigger = (body or {}).get("data", {}).get("trigger") or {}
        if trigger.get("decision") != "ignore":
            failures.append("the model said no and the trigger was accepted: %r" % trigger)
        if trigger.get("task_id"):
            failures.append("a trigger the model ignored still became a task: %r" % trigger)
        MockAgent.answer = '{"decision": "accept", "reason": "这是一件要做的事"}'

        # ---- the same key is the same event ----------------------------
        status, body = call(queue_port, "POST", "/api/triggers",
                             {"source": "mail", "text": "再跑一次测试",
                              "dedup_key": "mail:3"})
        first = (body or {}).get("data", {}).get("trigger") or {}
        first_task = first.get("task_id")
        status, body = call(queue_port, "POST", "/api/triggers",
                             {"source": "mail", "text": "再跑一次测试",
                              "dedup_key": "mail:3"})
        again = (body or {}).get("data", {}).get("trigger") or {}
        if not (body or {}).get("data", {}).get("duplicate"):
            failures.append("the second trigger with the same key was not a duplicate: %r" % body)
        if again.get("id") != first.get("id"):
            failures.append("the duplicate is a different trigger: %r vs %r"
                            % (again.get("id"), first.get("id")))
        if again.get("task_id") != first_task:
            failures.append("the duplicate queued the work again")

        # ---- the same conversation is the same thread ------------------
        status, body = call(queue_port, "POST", "/api/triggers",
                             {"source": "mail", "text": "第一件事",
                              "dedup_key": "chat:9:1", "thread_key": "chat:9"})
        one = (body or {}).get("data", {}).get("trigger") or {}
        status, body = call(queue_port, "POST", "/api/triggers",
                             {"source": "mail", "text": "第二件事",
                              "dedup_key": "chat:9:2", "thread_key": "chat:9"})
        two = (body or {}).get("data", {}).get("trigger") or {}
        if not one.get("thread_id") or one.get("thread_id") != two.get("thread_id"):
            failures.append("the two triggers did not land in the same thread: %r vs %r"
                            % (one.get("thread_id"), two.get("thread_id")))
        status, body = call(queue_port, "GET", "/api/threads/" + str(one.get("thread_id")))
        thread = (body or {}).get("data") or {}
        if len(thread.get("task_ids") or []) < 2:
            failures.append("the thread does not know both tasks: %r" % thread)

        # ---- the trigger list ------------------------------------------
        status, body = call(queue_port, "GET", "/api/triggers")
        items = (body or {}).get("data", {}).get("triggers") or []
        if len(items) < 5:
            failures.append("the trigger list is short: %d" % len(items))

        # ---- the chat front end can go through the trigger entry ---------
        sys.path.insert(0, os.path.join(REPO_ROOT, "sched"))
        import channel  # noqa: E402  (the path is only known here)

        class FakeChannel(channel.TaskChannel):
            def __init__(self):  # nothing that would touch the network
                self.args = type("Args", (), {"tasks": "", "agent": "", "workspace_path": "",
                                              "workspace_name": "", "triggers": True,
                                              "provider": "", "allow_path": []})()
                self.queue = None
                self.agent = None
                self.workspace_id = "ws_mock"
                self.owner = {}
                self.sent = []

            def send_text(self, user_id, text):
                self.sent.append((user_id, text))
                return True

            def ensure_workspace(self):
                return "ws_mock"

        front = FakeChannel()
        front.queue = channel.Queue("http://127.0.0.1:%d" % queue_port)
        reply = front.handle("请把服务部署到新机器上", "u1")
        if "已排队" not in reply:
            failures.append("the chat front end did not queue the message: %r" % reply)

        MockAgent.answer = '{"decision": "ignore", "reason": "只是问候"}'
        reply = front.handle("早上好呀", "u1")
        if "先不管" not in reply:
            failures.append("the chat front end did not refuse the greeting: %r" % reply)
        MockAgent.answer = '{"decision": "accept", "reason": "这是一件要做的事"}'

        # ---- the old way still works ------------------------------------
        status, body = call(queue_port, "POST", "/api/tasks", {"prompt": "直接排一个任务"})
        if status not in (200, 201) or not ((body or {}).get("data") or {}).get("id"):
            failures.append("the old way (POST /api/tasks) is broken: %s %r" % (status, body))
    finally:
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
    print("PASS: the trigger entry decides, dedups and files the work")
    return 0


if __name__ == "__main__":
    sys.exit(main())
