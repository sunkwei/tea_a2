#!/usr/bin/env python3
"""Smoke test for the router (the thread context and the shadow speaker).

What this pins:

  * a task's progress (the notes it reports, its result) goes into the
    thread it belongs to;
  * the NEXT task of the same thread is told what happened before it - that
    is the whole point of the router: the sessions are separate, the
    conversation is not;
  * what falls out of the window is not lost: it moves to the spill, and
    the router compresses it into a summary;
  * the router says one sentence per event (shadow mode: it writes it down
    instead of pushing it to the chat front ends).

Usage: python3 tests/smoke_router.py [path-to-tea_agent-binary]
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
ROUTER = os.path.join(REPO_ROOT, "sched", "router.py")


class MockAgent(BaseHTTPRequestHandler):
    """A fake agent whose turns report progress and then finish."""

    notes_per_turn = 12
    records = {"chat": [], "llm": 0}
    counter = 0
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
        if self.path == "/api/llm/chat":
            MockAgent.records["llm"] += 1
            return self._json({"ok": True, "content": "这些进展合起来是一件事：修好了。",
                               "reasoning": "", "finish_reason": "stop"})
        if self.path == "/api/chat":
            try:
                body = json.loads(raw or b"{}")
            except ValueError:
                body = {}
            MockAgent.records["chat"].append(body)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            frames = [{"type": "start", "session_id": "sess_mock",
                       "workspace_id": "ws_mock"}]
            for i in range(MockAgent.notes_per_turn):
                MockAgent.counter += 1
                frames.append({"type": "tool_call", "name": "toolkit_task",
                               "arguments": {"progress": 10 + i * 5,
                                             "note": "第 %d 步" % MockAgent.counter}})
            frames.append({"type": "done", "content": "做完了"})
            for frame in frames:
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


def main():
    binary = (
        sys.argv[1] if len(sys.argv) > 1 else os.path.join(REPO_ROOT, "build", "bin", "tea_agent")
    )
    if not os.path.exists(binary):
        print("binary not found:", binary)
        return 1
    if not os.path.exists(ROUTER):
        print("router not found:", ROUTER)
        return 1

    tmp = tempfile.mkdtemp(prefix="tea_router_")
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
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    router = None
    failures = []
    try:
        if not wait_health(queue_port):
            print("the queue did not become healthy")
            return 1

        router_dir = os.path.join(tmp, "router")
        router = subprocess.Popen(
            [
                sys.executable,
                ROUTER,
                "--tasks",
                "http://127.0.0.1:%d" % queue_port,
                "--agent",
                "http://127.0.0.1:%d" % agent_port,
                "--spill-after",
                "2",
                "--data-dir",
                router_dir,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        # ---- a trigger becomes a task, and the task belongs to a thread --
        status, body = call(queue_port, "POST", "/api/triggers",
                             {"source": "mail", "text": "把字体设置拆成三行",
                              "dedup_key": "chat:1", "thread_key": "chat:1"})
        trigger = (body or {}).get("data", {}).get("trigger") or {}
        thread_id = trigger.get("thread_id") or ""
        first_task = trigger.get("task_id") or ""
        if not thread_id:
            failures.append("the trigger did not get a thread: %r" % trigger)
            return 1

        # ---- the progress of the task goes into the thread --------------
        def thread_has_notes():
            status, body = call(queue_port, "GET", "/api/threads/%s/context" % thread_id)
            data = (body or {}).get("data") or {}
            return len(data.get("context") or []) > 0

        if not wait_until(thread_has_notes, 40):
            failures.append("the progress of the task did not reach the thread")
        else:
            status, body = call(queue_port, "GET", "/api/threads/%s/context" % thread_id)
            data = (body or {}).get("data") or {}
            kinds = [e.get("kind") for e in (data.get("context") or [])]
            if "note" not in kinds:
                failures.append("the thread does not have the progress notes: %r" % kinds)
            if not data.get("prompt"):
                failures.append("the thread has no prompt for the next task")

        # ---- the next task of the thread is told what happened before ---
        before_chats = len(MockAgent.records["chat"])
        status, body = call(queue_port, "POST", "/api/triggers",
                             {"source": "mail", "text": "接着刚才那件事继续",
                              "dedup_key": "chat:2", "thread_key": "chat:1"})
        second = (body or {}).get("data", {}).get("trigger") or {}
        if second.get("thread_id") != thread_id:
            failures.append("the second trigger did not land in the same thread")

        def second_task_started():
            if len(MockAgent.records["chat"]) <= before_chats:
                return False
            body = MockAgent.records["chat"][-1]
            return "这条线索之前的事" in (body.get("task_context") or "")

        if not wait_until(second_task_started, 40):
            failures.append("the second task was not told what happened before it")
        else:
            body = MockAgent.records["chat"][-1]
            context = body.get("task_context") or ""
            if "第 " not in context:
                failures.append("the context does not carry the earlier steps: %r" % context[:200])

        # ---- what falls out of the window is compressed ------------------
        def thread_has_summary():
            status, body = call(queue_port, "GET", "/api/threads/%s/context" % thread_id)
            data = (body or {}).get("data") or {}
            for entry in (data.get("spill") or []):
                if entry.get("kind") == "summary" and entry.get("text"):
                    return True
            return False

        if not wait_until(thread_has_summary, 60):
            failures.append("the router did not compress what fell out of the window")
        else:
            status, body = call(queue_port, "GET", "/api/threads/%s/context" % thread_id)
            data = (body or {}).get("data") or {}
            if "修好了" not in (data.get("prompt") or ""):
                failures.append("the summary did not reach the prompt: %r" % data.get("prompt"))

        # ---- the full record of the thread is a file the model can read ---
        record = os.path.join(ws_dir, ".tea_agent_run", "threads", "%s.md" % thread_id)
        if not wait_until(lambda: os.path.exists(record), 30):
            failures.append("the record of the thread was not written into the workspace")
        else:
            with open(record, encoding="utf-8") as f:
                body = f.read()
            if "第 " not in body:
                failures.append("the record does not carry the steps: %r" % body[:200])
            # The next task is told where the record is.
            contexts = [c.get("task_context") or "" for c in MockAgent.records["chat"]]
            if not any(".tea_agent_run/threads/%s.md" % thread_id in c for c in contexts):
                failures.append("no task was told where the record of the thread is")

        # ---- the router's line goes into the queue's event stream ---------
        def speech_in_the_bus():
            status, body = call(queue_port, "GET", "/api/tasks/%s/events" % first_task)
            for event in ((body or {}).get("data") or {}).get("events") or []:
                payload = event.get("payload") or {}
                if payload.get("type") == "speech" and payload.get("line"):
                    return payload
            return None

        line = wait_until(speech_in_the_bus, 40) and speech_in_the_bus()
        if not line:
            failures.append("the router did not hand its sentence to the queue")
        else:
            if "做完了" not in (line.get("line") or ""):
                failures.append("the sentence is not about the finished work: %r" % line)
            if "字体" not in (line.get("line") or ""):
                failures.append("the sentence does not say which conversation it is about: %r"
                                % line)
            if line.get("speech_of") != "task_done":
                failures.append("the sentence does not say which event it is about: %r" % line)

            # ---- the chat front end delivers the router's words ----------
            sys.path.insert(0, os.path.join(REPO_ROOT, "sched"))
            import channel  # noqa: E402  (the path is only known here)

            class FakeChannel(channel.TaskChannel):
                def __init__(self):
                    self.args = type("Args", (), {"tasks": "", "agent": "", "workspace_path": "",
                                                  "workspace_name": "", "speech": True,
                                                  "provider": "", "allow_path": []})()
                    self.queue = None
                    self.agent = None
                    self.workspace_id = "ws_mock"
                    self.owner = {first_task: "u1"}
                    self.sent = []

                def send_text(self, user_id, text):
                    self.sent.append((user_id, text))
                    return True

            front = FakeChannel()
            if not front.notify_event({"type": "speech", "line": line.get("line"),
                                       "speech_of": "task_done",
                                       "task": {"id": first_task, "workspace_id": "ws_mock",
                                                "meta": {"user_id": "u1"}}}):
                failures.append("the chat front end did not deliver the router's sentence")
            elif not front.sent or front.sent[0][0] != "u1":
                failures.append("the sentence went to the wrong person: %r" % (front.sent,))

            # The raw events are somebody else's business now.
            front.sent = []
            front.notify_event({"type": "task_done", "task": {"id": first_task,
                                                              "workspace_id": "ws_mock",
                                                              "meta": {"user_id": "u1"}}})
            if front.sent:
                failures.append("the front end still speaks for itself: %r" % (front.sent,))

        # ---- the router says something about every event -----------------
        speech_path = os.path.join(router_dir, "speech.jsonl")
        if not wait_until(lambda: os.path.exists(speech_path)
                          and os.path.getsize(speech_path) > 0, 30):
            failures.append("the router did not say anything")
        else:
            lines = []
            with open(speech_path, encoding="utf-8") as f:
                for raw in f:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        lines.append(json.loads(raw).get("line") or "")
                    except ValueError:
                        continue
            if not any("做完了" in line for line in lines):
                failures.append("the router did not say that the work is done: %r" % lines[:5])
    finally:
        if router is not None:
            router.terminate()
            try:
                router.wait(timeout=10)
            except subprocess.TimeoutExpired:
                router.kill()
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
    print("PASS: the threads remember, the router compresses and speaks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
