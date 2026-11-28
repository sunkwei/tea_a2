#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The router on the platform: its sentences and its heartbeat.

    python3 tests/smoke_bus_router.py

Covers FR-08 (docs/message-platform.md): every sentence it writes to
speech.jsonl is also published as `a2/router/speech`, the router announces
itself on `a2/heartbeat/router` (retained), and a dead platform never stops it
from speaking - it only loses the copy.

The queue is a fake SSE server: the point here is the router's output, and the
input side already has its own test (tests/smoke_router.py).
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
ROUTER = os.path.join(REPO_ROOT, "sched", "router.py")


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
    """Just enough of /api/events: a list the test pushes into."""

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
    def __init__(self, port, token, flt, since=None):
        self.frames = []
        self.cond = threading.Condition()
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

    def wait(self, count, timeout=10.0, where=None):
        deadline = time.time() + timeout
        with self.cond:
            while True:
                got = [f for f in self.frames if where is None or where(f)]
                if len(got) >= count:
                    return got
                left = deadline - time.time()
                if left <= 0:
                    return got
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


def wait_health(port, timeout=20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            status, body = call(port, "GET", "/health", timeout=2)
            if status == 200:
                return body
        except OSError:
            pass
        time.sleep(0.1)
    return None


def lines_of(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]
    except (OSError, ValueError):
        return []


def main():
    tmp = tempfile.mkdtemp(prefix="tea_bus_router_")
    data = os.path.join(tmp, "bus")
    os.makedirs(data)
    router_data = os.path.join(tmp, "router")
    bus_port = free_port()
    queue_port = free_port()
    failures = []

    def check(name, ok, detail=""):
        if not ok:
            failures.append("%s: %s" % (name, detail))

    bus = None
    router = None
    queue = FakeQueue(queue_port)
    reader = None
    try:
        env = dict(os.environ)
        env["TEA_AGENT_HOME"] = tmp

        def start_bus():
            proc = subprocess.Popen(
                [sys.executable, BUS, "--port", str(bus_port), "--data-dir", data,
                 "--heartbeat-topic", "3600", "--keepalive", "1"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
            body = wait_health(bus_port)
            return proc, body

        bus, health = start_bus()
        if health is None:
            print("the bus did not come up")
            return 1
        token = open(os.path.join(data, "token"), encoding="utf-8").read().strip()

        queue.start()
        router = subprocess.Popen(
            [sys.executable, ROUTER,
             "--tasks", "http://127.0.0.1:%d" % queue_port,
             "--agent", "http://127.0.0.1:1",          # never asked for a summary here
             "--data-dir", router_data,
             "--bus", "http://127.0.0.1:%d" % bus_port,
             "--bus-heartbeat", "1",
             "--bus-timeout", "2"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)

        reader = SseReader(bus_port, token, "a2/#", since=0)
        check("the subscription is open", reader.status == 200, str(reader.status))

        # ---- AC-08.1: the sentence goes to the platform ----
        event = {"type": "task_done",
                 "task": {"id": "t_9", "title": "跑一遍构建", "status": "done",
                          "workspace_id": "ws_mock", "result": "构建通过"},
                 "thread": {"id": "th_1", "title": "跑一遍构建"}}
        queue.push(event)
        frames = reader.wait(1, timeout=10,
                             where=lambda f: f["data"].get("topic") == "a2/router/speech")
        check("the sentence reaches a2/router/speech", len(frames) >= 1, repr(frames[:1]))
        if frames:
            payload = frames[0]["data"].get("payload") or {}
            check("the sentence is the one in speech.jsonl",
                  "做完了" in (payload.get("line") or ""), repr(payload))
            check("it says which event and which task",
                  payload.get("speech_of") == "task_done" and payload.get("task_id") == "t_9",
                  repr(payload))
            check("the speech log has the same line",
                  any("做完了" in (entry.get("line") or "")
                      for entry in lines_of(os.path.join(router_data, "speech.jsonl"))),
                  repr(lines_of(os.path.join(router_data, "speech.jsonl"))))

        # ---- AC-08.2: the heartbeat (retained) ----
        beats = reader.wait(1, timeout=8,
                            where=lambda f: f["data"].get("topic") == "a2/heartbeat/router")
        check("the router announces itself", len(beats) >= 1, repr(beats[:1]))
        if beats:
            payload = beats[0]["data"].get("payload") or {}
            check("the heartbeat carries pid, version and uptime",
                  payload.get("pid") and payload.get("version") and "uptime_s" in payload,
                  repr(payload))

        # ---- AC-08.3: the platform dies, the router keeps speaking ----
        bus.terminate()
        try:
            bus.wait(timeout=10)
        except subprocess.TimeoutExpired:
            bus.kill()
        bus = None
        time.sleep(0.5)
        before = len(lines_of(os.path.join(router_data, "speech.jsonl")))
        queue.push({"type": "task_created",
                    "task": {"id": "t_10", "title": "第二个活儿", "status": "queued",
                             "workspace_id": "ws_mock"},
                    "thread": {"id": "th_2", "title": "第二个活儿"}})
        deadline = time.time() + 10
        while time.time() < deadline:
            if len(lines_of(os.path.join(router_data, "speech.jsonl"))) > before:
                break
            time.sleep(0.2)
        check("the router still speaks when the platform is down",
              len(lines_of(os.path.join(router_data, "speech.jsonl"))) > before,
              "lines=%d" % len(lines_of(os.path.join(router_data, "speech.jsonl"))))
        check("the router is still running", router.poll() is None, "exit=%r" % router.poll())

        # ---- AC-08.3 (the other half): it comes back ----
        bus, health = start_bus()
        check("the bus came back", health is not None)
        reader.close()
        reader = SseReader(bus_port, token, "a2/heartbeat/#", since=0)
        beats = reader.wait(1, timeout=10,
                            where=lambda f: f["data"].get("topic") == "a2/heartbeat/router")
        check("the heartbeat resumes after the platform is back", len(beats) >= 1,
              repr(beats[:1]))
    finally:
        if reader is not None:
            reader.close()
        if router is not None:
            router.terminate()
            try:
                router.wait(timeout=10)
            except subprocess.TimeoutExpired:
                router.kill()
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
    print("PASS: the router speaks on the platform, heartbeats, and survives its death")
    return 0


if __name__ == "__main__":
    sys.exit(main())
