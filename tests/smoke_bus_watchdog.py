#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The watchdog on the platform: who stopped beating, and one alert in three places.

    python3 tests/smoke_bus_watchdog.py

Covers FR-09 (docs/message-platform.md): heartbeats are read from
`a2/heartbeat/#` (retained), a component that goes quiet is reported as
`router_missing` / `channel_missing` / `bus_missing`, the alert lands in the
audit log, the queue's event stream *and* `a2/watchdog/alert`, and a repeated
alert is suppressed for the cooldown.

A real bus, a real watchdog, and a tiny fake queue that records what the
watchdog posts. The intervals are the real ones (the watchdog never passes more
often than every 5 s), so the test takes about a minute.
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
    """What the watchdog needs of the queue: /api/events (POST) and /api/health."""

    def __init__(self, port):
        self.port = port
        self.events = []
        self.lock = threading.Lock()
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
                if self.path.startswith("/api/health"):
                    return self._json(200, {"code": 0, "data": {
                        "ok": True, "agent": "http://127.0.0.1:1",
                        "quota_percent": None, "counts": {}, "metrics": {}}})
                if self.path.startswith("/api/tasks"):
                    return self._json(200, {"code": 0, "data": {"tasks": [], "total": 0}})
                return self._json(404, {"code": 404, "message": "no"})

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", "0") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    body = json.loads(raw.decode("utf-8"))
                except ValueError:
                    body = {}
                if self.path == "/api/events":
                    with queue.lock:
                        queue.events.append(body)
                    return self._json(200, {"code": 0, "data": {"ok": True}})
                return self._json(404, {"code": 404, "message": "no"})

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.1}, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def posted(self, check=None):
        with self.lock:
            items = list(self.events)
        if check is None:
            return items
        return [item for item in items if item.get("check") == check]

    def stop_it(self):
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
            status, body = call(port, "GET", "/health", timeout=2)
            if status == 200:
                return body
        except OSError:
            pass
        time.sleep(0.1)
    return None


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
    tmp = tempfile.mkdtemp(prefix="tea_bus_watchdog_")
    data = os.path.join(tmp, "bus")
    wd_dir = os.path.join(tmp, "watchdog")
    os.makedirs(data)
    bus_port = free_port()
    queue_port = free_port()
    audit_path = os.path.join(wd_dir, "audit.jsonl")
    failures = []

    def check(name, ok, detail=""):
        if not ok:
            failures.append("%s: %s" % (name, detail))

    bus = None
    watchdog = None
    queue = FakeQueue(queue_port)
    reader = None
    channel_stop = threading.Event()
    router_stop = threading.Event()
    try:
        env = dict(os.environ)
        env["TEA_AGENT_HOME"] = tmp

        def start_bus():
            proc = subprocess.Popen(
                [sys.executable, BUS, "--port", str(bus_port), "--data-dir", data,
                 "--heartbeat-topic", "3600", "--keepalive", "1"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
            return proc, wait_health(bus_port)

        bus, health = start_bus()
        if health is None:
            print("the bus did not come up")
            return 1
        token = open(os.path.join(data, "token"), encoding="utf-8").read().strip()
        queue.start()

        def publish_beat(topic, extra=None):
            payload = {"at": "2026-11-27T00:00:00.000Z", "pid": os.getpid(),
                       "version": "1.0.0", "uptime_s": 10}
            payload.update(extra or {})
            try:
                call(bus_port, "POST", "/api/publish",
                     {"topic": topic, "payload": payload, "retain": True},
                     token=token)
            except OSError:
                pass          # the bus is killed at the end of the test

        def beat_forever(topic, stop, extra=None):
            while not stop.is_set():
                publish_beat(topic, extra)
                stop.wait(1.0)

        # Two beaters on purpose: stopping the channel must not stop the
        # router, otherwise the test cannot tell one alert from two.
        threading.Thread(target=beat_forever,
                         args=("a2/heartbeat/router", router_stop),
                         daemon=True).start()
        threading.Thread(target=beat_forever,
                         args=("a2/heartbeat/channel", channel_stop, {"channel": "fake"}),
                         daemon=True).start()

        watchdog = subprocess.Popen(
            [sys.executable, WATCHDOG,
             "--tasks", "http://127.0.0.1:%d" % queue_port,
             "--agent", "http://127.0.0.1:1",
             "--bus", "http://127.0.0.1:%d" % bus_port,
             "--expect-heartbeat", "a2/heartbeat/router",
             "--expect-heartbeat", "a2/heartbeat/channel",
             "--heartbeat-timeout", "6",
             "--alert-cooldown", "60",
             "--bus-grace", "1",
             "--interval", "5",
             "--timeout", "3",
             "--data-dir", wd_dir],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)

        reader = SseReader(bus_port, token, "a2/watchdog/alert", since=0)

        # ---- AC-09.1 (the happy half): both beat, nobody is reported ----
        time.sleep(8)
        early = [e for e in read_audit(audit_path) if e.get("check", "").endswith("_missing")]
        check("no alert while everything beats", not early, repr(early[-3:]))

        # ---- AC-09.1 (the other half): the channel goes quiet ----
        channel_stop.set()
        deadline = time.time() + 30
        missing = []
        while time.time() < deadline:
            missing = [e for e in read_audit(audit_path)
                       if e.get("check") == "channel_missing"]
            if missing:
                break
            time.sleep(0.5)
        check("a channel that stops beating is reported", bool(missing),
              repr([e.get("check") for e in read_audit(audit_path)][-5:]))
        check("the alert names the topic",
              bool(missing) and missing[-1].get("target") == "a2/heartbeat/channel",
              repr(missing[-1:]))
        check("the router is still fine (it kept beating until the end)",
              not [e for e in read_audit(audit_path) if e.get("check") == "router_missing"],
              repr([e for e in read_audit(audit_path) if e.get("check") == "router_missing"]))

        # ---- AC-09.2: the same alert in three places ----
        queued = queue.posted("channel_missing")
        check("the queue's event stream got the alert", bool(queued), repr(queue.posted()[-3:]))
        if queued:
            check("the queue's copy says what happened",
                  "a2/heartbeat/channel" in (queued[-1].get("reason") or ""),
                  repr(queued[-1]))
        on_bus = reader.wait(1, timeout=10,
                             where=lambda f: (f["data"].get("payload") or {}).get("check")
                             == "channel_missing")
        check("the platform got the alert", bool(on_bus), repr(reader.frames[-3:]))
        if on_bus:
            payload = on_bus[0]["data"].get("payload") or {}
            check("the platform's copy carries check/target/reason",
                  payload.get("target") == "a2/heartbeat/channel"
                  and payload.get("reason"), repr(payload))

        # ---- AC-09.3: the same alert is not repeated inside the cooldown ----
        time.sleep(8)
        repeated = [e for e in read_audit(audit_path) if e.get("check") == "channel_missing"]
        check("the alert is not repeated inside the cooldown", len(repeated) == 1,
              "%d entries" % len(repeated))

        # ---- AC-09.1: the platform itself dies ----
        bus.terminate()
        try:
            bus.wait(timeout=10)
        except subprocess.TimeoutExpired:
            bus.kill()
        bus = None
        deadline = time.time() + 30
        bus_missing = []
        while time.time() < deadline:
            bus_missing = [e for e in read_audit(audit_path) if e.get("check") == "bus_missing"]
            if bus_missing:
                break
            time.sleep(0.5)
        check("a platform that stops answering is reported", bool(bus_missing),
              repr([e.get("check") for e in read_audit(audit_path)][-5:]))
        check("the platform-down alert still reaches the audit log",
              bool(bus_missing) and bus_missing[-1].get("reason"),
              repr(bus_missing[-1:]))
        check("the watchdog is still running", watchdog.poll() is None,
              "exit=%r" % watchdog.poll())
    finally:
        channel_stop.set()
        router_stop.set()
        if reader is not None:
            reader.close()
        if watchdog is not None:
            watchdog.terminate()
            try:
                watchdog.wait(timeout=10)
            except subprocess.TimeoutExpired:
                watchdog.kill()
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
    print("PASS: the watchdog reads heartbeats, alerts in three places, and survives the platform")
    return 0


if __name__ == "__main__":
    sys.exit(main())
