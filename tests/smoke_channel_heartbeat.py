#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The chat front end announces itself on a2/heartbeat/channel.

    python3 tests/smoke_channel_heartbeat.py

Covers the channel half of FR-09 (docs/message-platform.md): with `--bus` the
shared TaskChannel starts one heartbeat thread (not two), the message names the
channel and its workspace, and without `--bus` nothing is started at all - the
chat front ends keep working exactly as before.
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
import types
import urllib.parse

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "sched"))

BUS = os.path.join(REPO_ROOT, "sched", "bus_server.py")


def free_port():
    while True:
        port = random.randint(20000, 60000)
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue


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


def make_channel(bus, token=""):
    """A TaskChannel subclass that touches no network of its own."""
    import channel as channel_mod

    class FakeChannel(channel_mod.TaskChannel):
        def send_text(self, user_id, text):
            pass

        def send_file(self, user_id, path, name=None):
            pass

        def channel_name(self):
            return "fake"

    args = types.SimpleNamespace(
        tasks="http://127.0.0.1:1", agent="http://127.0.0.1:1",
        bus=bus, bus_token=token, bus_heartbeat=5.0,
    )
    return FakeChannel(args)


def main():
    tmp = tempfile.mkdtemp(prefix="tea_channel_hb_")
    data = os.path.join(tmp, "bus")
    os.makedirs(data)
    port = free_port()
    failures = []

    def check(name, ok, detail=""):
        if not ok:
            failures.append("%s: %s" % (name, detail))

    bus = None
    reader = None
    try:
        env = dict(os.environ)
        env["TEA_AGENT_HOME"] = tmp
        bus = subprocess.Popen(
            [sys.executable, BUS, "--port", str(port), "--data-dir", data,
             "--heartbeat-topic", "3600", "--keepalive", "1"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
        check("the platform came up", wait_health(port))
        token = open(os.path.join(data, "token"), encoding="utf-8").read().strip()

        # ---- without --bus: nothing happens ----
        quiet = make_channel("")
        quiet.workspace_id = "ws_quiet"
        quiet.start_bus_heartbeat()
        check("without --bus no heartbeat thread is started",
              quiet._bus_heartbeat_started is False,
              repr(quiet._bus_heartbeat_started))

        # ---- with --bus: one heartbeat, and calling twice does not double it ----
        reader = SseReader(port, token, "a2/heartbeat/channel", since=0)
        check("the subscription is open", reader.status == 200, str(reader.status))
        front = make_channel("http://127.0.0.1:%d" % port, token)
        front.workspace_id = "ws_mock"
        front.start_bus_heartbeat()
        front.start_bus_heartbeat()          # must not start a second thread
        frames = reader.wait(1, timeout=10)
        check("the channel announces itself", len(frames) >= 1, repr(frames[:1]))
        if frames:
            payload = frames[0]["data"].get("payload") or {}
            check("the heartbeat names the channel and the workspace",
                  payload.get("channel") == "fake" and payload.get("workspace_id") == "ws_mock",
                  repr(payload))
            check("it carries a pid and a time",
                  payload.get("pid") and payload.get("at"), repr(payload))
        time.sleep(1.5)
        check("calling start twice did not double the beats", len(reader.frames) <= 2,
              "%d frames" % len(reader.frames))
    finally:
        if reader is not None:
            reader.close()
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
    print("PASS: a chat front end says it is here, once, and only when asked to")
    return 0


if __name__ == "__main__":
    sys.exit(main())
