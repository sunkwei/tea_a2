#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The bridge: queue events -> the platform, and what it does when the queue goes.

    python3 tests/smoke_bus_pump.py

Covers FR-07 (docs/message-platform.md): the three mappings, the idempotency
key, the slim payload, the skip of types with no topic, and - the hard part -
what happens across a queue restart: a gap is recorded, and the bridge picks the
stream up again. A real bus_server, a real bus_pump, a fake queue that can be
killed and restarted on the same port.
"""
import http.client
import json
import os
import random
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler

try:
    from http.server import ThreadingHTTPServer
except ImportError:  # Python 3.6
    from http.server import HTTPServer
    from socketserver import ThreadingMixIn

    class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
        daemon_threads = True

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUS = os.path.join(REPO_ROOT, "sched", "bus_server.py")
PUMP = os.path.join(REPO_ROOT, "sched", "bus_pump.py")


def free_port():
    while True:
        port = random.randint(20000, 60000)
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue


class FakeQueue(object):
    """The queue's /api/events, with a `push()` a test can call."""

    def __init__(self, port):
        self.port = port
        self.cond = threading.Condition()
        self.frames = []
        self.stop = threading.Event()
        queue = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_GET(self):  # noqa: N802
                if self.path != "/api/events":
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                seen = 0
                try:
                    while not queue.stop.is_set():
                        with queue.cond:
                            queue.cond.wait(timeout=0.2)
                            pending = queue.frames[seen:]
                        if pending:
                            for frame in pending:
                                self.wfile.write(
                                    ("data: " + json.dumps(frame, ensure_ascii=False)
                                     + "\n\n").encode("utf-8"))
                                self.wfile.flush()
                            seen += len(pending)
                        else:
                            self.wfile.write(b": keepalive\n\n")
                            self.wfile.flush()
                except OSError:
                    return

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.1}, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def push(self, event):
        with self.cond:
            self.frames.append(event)
            self.cond.notify_all()

    def stop_it(self):
        self.stop.set()
        try:
            with self.cond:
                self.cond.notify_all()
        except Exception:  # noqa: BLE001
            pass
        self.server.shutdown()
        self.server.server_close()


class SseReader(object):
    """Read a bus subscription in the background."""

    def __init__(self, port, token, flt, since=None):
        self.frames = []
        self.cond = threading.Condition()
        self.status = 0
        self.conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
        path = "/api/subscribe?filter=" + urllib.parse.quote(flt, safe="")
        if since is not None:
            path += "&since=%s" % since
        self.conn.request("GET", path, headers={"Authorization": "Bearer " + token,
                                                "Accept": "text/event-stream"})
        self.resp = self.conn.getresponse()
        self.status = self.resp.status
        if self.status != 200:
            self.error = self.resp.read()
            return
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        frame = {}
        while True:
            try:
                line = self.resp.readline()
            except (OSError, ValueError):
                break
            if not line:
                break
            text = line.decode("utf-8", "replace").rstrip("\r\n")
            if text == "":
                if frame:
                    with self.cond:
                        self.frames.append(frame)
                        self.cond.notify_all()
                    frame = {}
                continue
            if text.startswith(":"):
                continue
            if text.startswith("id: "):
                frame["id"] = int(text[4:])
            elif text.startswith("event: "):
                frame["event"] = text[7:]
            elif text.startswith("data: "):
                try:
                    frame["data"] = json.loads(text[6:])
                except ValueError:
                    frame["data"] = {}

    def wait(self, count, timeout=10.0):
        deadline = time.time() + timeout
        with self.cond:
            while True:
                if len(self.frames) >= count:
                    return self.frames
                left = deadline - time.time()
                if left <= 0:
                    return self.frames
                self.cond.wait(min(0.2, left))

    def close(self):
        try:
            self.conn.close()
        except OSError:
            pass


def call(port, method, path, body=None, token=None, timeout=20):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    headers = {}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token
    try:
        conn.request(method, path, body=data, headers=headers)
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8", "replace")
        status = resp.status
    finally:
        conn.close()
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, {"raw": raw}


def task(task_id, status="done", **extra):
    record = {
        "id": task_id,
        "title": "把 README 的例子跑一遍",
        "status": status,
        "workspace_id": "ws_mock",
        "created_at": "2026-11-27T01:00:00Z",
        "updated_at": extra.pop("updated_at", "2026-11-27T01:02:00Z"),
        "prompt": "x" * 5000,
    }
    record.update(extra)
    return record


def main():
    tmp = tempfile.mkdtemp(prefix="tea_pump_")
    data = os.path.join(tmp, "bus")
    os.makedirs(data)
    bus_port = free_port()
    queue_port = free_port()
    failures = []

    def check(name, ok, detail=""):
        if not ok:
            failures.append("%s: %s" % (name, detail))

    bus = None
    queue = FakeQueue(queue_port)
    pump = None
    try:
        env = dict(os.environ)
        env["TEA_AGENT_HOME"] = tmp
        bus = subprocess.Popen(
            [sys.executable, BUS, "--port", str(bus_port), "--data-dir", data,
             "--heartbeat-topic", "3600", "--keepalive", "1"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
        token = ""
        deadline = time.time() + 20
        while time.time() < deadline:
            status, _ = call(bus_port, "GET", "/health")
            if status == 200:
                break
            time.sleep(0.1)
        token = open(os.path.join(data, "token"), encoding="utf-8").read().strip()

        queue.start()
        pump = subprocess.Popen(
            [sys.executable, PUMP,
             "--tasks", "http://127.0.0.1:%d" % queue_port,
             "--bus", "http://127.0.0.1:%d" % bus_port,
             "--data-dir", data,
             "--retry", "0.3", "--max-retry", "0.6", "--read-timeout", "20"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)

        reader = SseReader(bus_port, token, "a2/queue/#", since=0)
        check("the subscription is open", reader.status == 200, str(reader.status))

        # ---- AC-07.1: task_created -> a2/queue/task ----
        queue.push({"type": "task_created", "task": task("t_1", status="queued")})
        frames = reader.wait(1, timeout=10)
        check("task_created reaches a2/queue/task",
              bool(frames) and frames[0]["data"].get("topic") == "a2/queue/task",
              repr(frames[:1]))
        if frames:
            payload = frames[0]["data"].get("payload") or {}
            check("the payload names the task", payload.get("task", {}).get("id") == "t_1",
                  repr(payload))
            check("the payload is slim (no prompt)",
                  "prompt" not in json.dumps(payload, ensure_ascii=False), repr(payload)[:200])
            check("the message carries a key",
                  bool(frames[0]["data"].get("key")), repr(frames[0]["data"]))

        # ---- AC-07.2: task_done -> a2/queue/task_done with the result ----
        queue.push({"type": "task_done", "task": task("t_1", result="做完了，报告在这里")})
        frames = reader.wait(2, timeout=10)
        done = [f for f in frames if f["data"].get("topic") == "a2/queue/task_done"]
        check("task_done reaches a2/queue/task_done", len(done) == 1, repr(frames))
        if done:
            payload = done[0]["data"].get("payload") or {}
            check("the result travels with it",
                  payload.get("task", {}).get("result") == "做完了，报告在这里",
                  repr(payload))

        # ---- the same event twice is published once ----
        before = len(reader.frames)
        queue.push({"type": "task_done", "task": task("t_1", result="做完了，报告在这里")})
        time.sleep(1.5)
        after = len(reader.frames)
        check("a redelivered event is not published twice", after == before,
              "%d -> %d" % (before, after))

        # ---- a type with no topic is skipped, not guessed ----
        queue.push({"type": "task_cancelled", "task": task("t_2", status="cancelled")})
        time.sleep(1.0)
        check("task_cancelled is not published under a lie",
              len(reader.frames) == after, "%d -> %d" % (after, len(reader.frames)))

        # ---- AC-07.3: the queue goes away, a gap is recorded, it reconnects ----
        queue.stop_it()
        state_path = os.path.join(data, "pump-state.json")

        def gaps():
            try:
                with open(state_path, encoding="utf-8") as fh:
                    return json.load(fh).get("gaps") or []
            except (OSError, ValueError):
                return []

        deadline = time.time() + 15
        while time.time() < deadline and not gaps():
            time.sleep(0.3)
        check("the downtime is recorded as a gap", len(gaps()) >= 1, repr(gaps()))

        queue = FakeQueue(queue_port).start()
        queue.push({"type": "task_failed", "task": task(
            "t_3", status="failed", error="the build broke")})
        frames = reader.wait(3, timeout=15)
        failed = [f for f in frames if f["data"].get("topic") == "a2/queue/task_failed"]
        check("the bridge reconnects after the queue comes back", len(failed) == 1,
              repr([f["data"].get("topic") for f in frames]))
        if failed:
            check("the failure reason travels with it",
                  (failed[0]["data"].get("payload") or {}).get("task", {}).get("error")
                  == "the build broke", repr(failed[0]["data"]))
        reader.close()

        # ---- --status says the same thing the file says ----
        out = subprocess.check_output([sys.executable, PUMP, "--status",
                                       "--data-dir", data],
                                      stderr=subprocess.STDOUT).decode("utf-8", "replace")
        check("--status prints the gaps",
              re.search(r"gaps\s*:\s*[1-9]", out) is not None, out[-400:])
        check("--status counts what was published", "published" in out, out[-400:])
    finally:
        if pump is not None:
            pump.terminate()
            try:
                pump.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pump.kill()
        try:
            queue.stop_it()
        except Exception:  # noqa: BLE001
            pass
        if bus is not None:
            bus.terminate()
            try:
                bus.wait(timeout=10)
            except subprocess.TimeoutExpired:
                bus.kill()
        shutil.rmtree(tmp, ignore_errors=True)

    if failures:
        print("FAIL")
        for f in failures:
            print(" -", f)
        return 1
    print("PASS: the bridge maps, dedupes, skips what it cannot map and records its gaps")
    return 0


if __name__ == "__main__":
    sys.exit(main())
