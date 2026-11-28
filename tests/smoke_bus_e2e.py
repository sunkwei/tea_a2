#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""End to end: the queue -> the router -> the platform -> the watchdog.

    python3 tests/smoke_bus_e2e.py

Covers FR-10 (docs/message-platform.md) - the drill the requirement asks for:
one command starts the platform, the router and the watchdog; a queue event
becomes a sentence on the platform; and killing the router makes the watchdog
say `router_missing` (in the audit log and on the platform), which is the whole
point of putting the two on a bus: the failure used to be silent.

The queue is fake (its own test covers it); the router, the watchdog and the
bus are the real programs.
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
PUMP = os.path.join(REPO_ROOT, "sched", "bus_pump.py")
WATCHDOG = os.path.join(REPO_ROOT, "sched", "watchdog.py")


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
    """SSE events, POSTed alerts and the two health endpoints."""

    def __init__(self, port):
        self.port = port
        self.cond = threading.Condition()
        self.frames = []
        self.events = []
        self.stop = threading.Event()
        queue = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _json(self, status, payload):
                raw = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):  # noqa: N802
                if self.path.startswith("/api/events"):
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
                    return
                if self.path.startswith("/api/health"):
                    return self._json(200, {"code": 0, "data": {
                        "ok": True, "agent": "http://127.0.0.1:1",
                        "quota_percent": None, "counts": {}, "metrics": {}}})
                if self.path.startswith("/api/tasks"):
                    return self._json(200, {"code": 0, "data": {"tasks": [], "total": 0}})
                if self.path.startswith("/api/threads"):
                    return self._json(200, {"code": 0, "data": {"threads": []}})
                return self._json(404, {"code": 404, "message": "no"})

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", "0") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    body = json.loads(raw.decode("utf-8"))
                except ValueError:
                    body = {}
                if self.path == "/api/events":
                    with queue.cond:
                        queue.events.append(body)
                    return self._json(200, {"code": 0, "data": {"ok": True}})
                return self._json(404, {"code": 404, "message": "no"})

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

    def posted(self, check=None):
        with self.cond:
            items = list(self.events)
        if check is None:
            return items
        return [item for item in items if item.get("check") == check]

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

    def wait(self, count, timeout=20.0, where=None):
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


def call(port, method, path, body=None, token=None, timeout=10):
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
            status, _ = call(port, "GET", "/health", timeout=2)
            if status == 200:
                return True
        except OSError:
            pass
        time.sleep(0.1)
    return False


def read_audit(path):
    out = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
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


def main():
    tmp = tempfile.mkdtemp(prefix="tea_bus_e2e_")
    data = os.path.join(tmp, "bus")
    router_dir = os.path.join(tmp, "router")
    wd_dir = os.path.join(tmp, "watchdog")
    pump_dir = os.path.join(tmp, "pump")
    os.makedirs(data)
    os.makedirs(pump_dir)
    bus_port = free_port()
    queue_port = free_port()
    audit_path = os.path.join(wd_dir, "audit.jsonl")
    failures = []

    def check(name, ok, detail=""):
        if not ok:
            failures.append("%s: %s" % (name, detail))

    bus = None
    pump = None
    router = None
    watchdog = None
    queue = FakeQueue(queue_port)
    reader = None
    try:
        env = dict(os.environ)
        env["TEA_AGENT_HOME"] = tmp

        bus = subprocess.Popen(
            [sys.executable, BUS, "--port", str(bus_port), "--data-dir", data,
             "--heartbeat-topic", "3600", "--keepalive", "1"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
        check("the platform came up", wait_health(bus_port))
        token = open(os.path.join(data, "token"), encoding="utf-8").read().strip()

        queue.start()
        pump = subprocess.Popen(
            [sys.executable, PUMP,
             "--tasks", "http://127.0.0.1:%d" % queue_port,
             "--bus", "http://127.0.0.1:%d" % bus_port,
             "--bus-token", token,
             "--data-dir", pump_dir,
             "--retry", "0.5", "--max-retry", "1",
             "--read-timeout", "20", "--bus-heartbeat", "5"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
        router = subprocess.Popen(
            [sys.executable, ROUTER,
             "--tasks", "http://127.0.0.1:%d" % queue_port,
             "--agent", "http://127.0.0.1:1",
             "--data-dir", router_dir,
             "--bus", "http://127.0.0.1:%d" % bus_port,
             "--bus-heartbeat", "1", "--bus-timeout", "2"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
        watchdog = subprocess.Popen(
            [sys.executable, WATCHDOG,
             "--tasks", "http://127.0.0.1:%d" % queue_port,
             "--agent", "http://127.0.0.1:1",
             "--bus", "http://127.0.0.1:%d" % bus_port,
             "--expect-heartbeat", "a2/heartbeat/router",
             "--expect-heartbeat", "a2/heartbeat/pump",
             "--heartbeat-timeout", "5",
             "--alert-cooldown", "600",
             "--bus-grace", "1",
             "--interval", "5",
             "--timeout", "3",
             "--data-dir", wd_dir],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)

        reader = SseReader(bus_port, token, "a2/#", since=0)
        check("the subscription is open", reader.status == 200, str(reader.status))

        # ---- a queue event becomes a sentence on the platform ----
        queue.push({"type": "task_done",
                    "task": {"id": "t_e2e", "title": "跑一遍构建", "status": "done",
                             "workspace_id": "ws_mock", "result": "构建通过"},
                    "thread": {"id": "th_e2e", "title": "跑一遍构建"}})
        speech = reader.wait(1, timeout=15,
                             where=lambda f: f["data"].get("topic") == "a2/router/speech")
        check("the router's sentence reached the platform", len(speech) >= 1,
              repr(reader.frames[-3:]))

        # ---- the bridge forwards the queue event and says it is alive ----
        forwarded = reader.wait(
            1, timeout=15,
            where=lambda f: f["data"].get("topic") == "a2/queue/task_done")
        check("the bridge put the queue event on the platform", len(forwarded) >= 1,
              repr([f["data"].get("topic") for f in reader.frames][-5:]))
        beats = reader.wait(
            1, timeout=15,
            where=lambda f: f["data"].get("topic") == "a2/heartbeat/pump")
        check("the bridge announced itself", len(beats) >= 1,
              repr([f["data"].get("topic") for f in reader.frames][-5:]))

        # ---- the router is alive: the watchdog must stay quiet ----
        time.sleep(7)
        early = [e for e in read_audit(audit_path) if e.get("check") == "router_missing"]
        check("no router_missing while the router beats", not early, repr(early[-2:]))

        # ---- kill the router: the watchdog has to notice ----
        router.terminate()
        try:
            router.wait(timeout=10)
        except subprocess.TimeoutExpired:
            router.kill()
        router = None
        deadline = time.time() + 30
        missing = []
        while time.time() < deadline:
            missing = [e for e in read_audit(audit_path) if e.get("check") == "router_missing"]
            if missing:
                break
            time.sleep(0.5)
        check("killing the router is reported as router_missing", bool(missing),
              repr([e.get("check") for e in read_audit(audit_path)][-5:]))
        alert = reader.wait(1, timeout=15,
                            where=lambda f: (f["data"].get("payload") or {}).get("check")
                            == "router_missing")
        check("the alert is on the platform as well", bool(alert),
              repr([f["data"].get("topic") for f in reader.frames][-5:]))
        check("the alert reached the queue's event stream",
              bool(queue.posted("router_missing")), repr(queue.posted()[-2:]))
    finally:
        if reader is not None:
            reader.close()
        for proc in (router, watchdog, pump, bus):
            if proc is not None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
        try:
            queue.stop_it()
        except Exception:  # noqa: BLE001
            pass
        shutil.rmtree(tmp, ignore_errors=True)

    if failures:
        print("FAIL")
        for f in failures:
            print(" -", f)
        return 1
    print("PASS: queue -> router -> platform -> watchdog, and the killed router is noticed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
