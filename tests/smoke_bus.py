#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The message platform on its own: publish, subscribe, window, auth, gaps.

    python3 tests/smoke_bus.py

Covers the acceptance criteria of FR-01 ~ FR-06 in docs/message-platform.md:
the process and its config, publishing (key/retain/limits), subscribing
(backlog, `since`, gaps, a slow reader), the window, the token and the
platform's own health. The router/watchdog side lives in
tests/smoke_bus_pump.py / tests/smoke_bus_e2e.py.

The server is a real process: the point is the socket, not a stub.
"""
import http.client
import json
import os
import random
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(REPO_ROOT, "sched", "bus_server.py")


def free_port():
    while True:
        port = random.randint(20000, 60000)
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue


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
            if status == 200 and body.get("ok"):
                return body
        except OSError:
            pass
        time.sleep(0.1)
    return None


class SseReader(object):
    """One subscription, read in the background so a test can wait on frames."""

    def __init__(self, port, token, flt, since=None):
        self.frames = []
        self.cond = threading.Condition()
        self.status = 0
        self.error = ""
        self.conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        path = "/api/subscribe?filter=" + urllib.parse.quote(flt, safe="")
        if since is not None:
            path += "&since=%s" % since
        headers = {"Accept": "text/event-stream"}
        if token:
            headers["Authorization"] = "Bearer " + token
        self.conn.request("GET", path, headers=headers)
        self.resp = self.conn.getresponse()
        self.status = self.resp.status
        if self.status != 200:
            self.error = self.resp.read().decode("utf-8", "replace")
            return
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

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
                    frame["data"] = {"raw": text[6:]}

    def wait(self, count, timeout=10.0, where=None):
        """Wait for `count` frames (optionally filtered by `where`)."""
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


def start(port, data_dir, extra=None):
    env = dict(os.environ)
    env["TEA_AGENT_HOME"] = os.path.dirname(data_dir)
    args = [sys.executable, SERVER,
            "--port", str(port), "--data-dir", data_dir]
    args += list(extra or [])
    return subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)


def stop(proc):
    if proc is None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def main():
    if not os.path.exists(SERVER):
        print("server not found:", SERVER)
        return 1

    tmp = tempfile.mkdtemp(prefix="tea_bus_")
    data = os.path.join(tmp, "bus")
    os.makedirs(data)
    port = free_port()
    failures = []

    def check(name, ok, detail=""):
        if not ok:
            failures.append("%s: %s" % (name, detail))

    server = None
    try:
        server = start(port, data, [
            "--window-messages", "50",
            "--window-minutes", "60",
            "--subscriber-buffer", "5",
            "--max-payload", "2048",
            "--keepalive", "1",
            "--heartbeat-file", "0.3",
            "--heartbeat-topic", "1",
        ])
        health = wait_health(port)
        if health is None:
            print("server did not come up")
            print((server.stdout.read() or b"").decode("utf-8", "replace")[-2000:])
            return 1

        token_path = os.path.join(data, "token")
        with open(token_path, encoding="utf-8") as fh:
            token = fh.read().strip()
        check("token file", len(token) >= 32, "token=%r" % token[:8] if token else "empty")
        if os.name != "nt":
            mode = stat.S_IMODE(os.stat(token_path).st_mode)
            check("token mode 0600", mode == 0o600, "mode=%o" % mode)

        # ---- FR-01 / AC-01.1: /health without a token ----
        check("health port", health.get("port") == port, repr(health.get("port")))
        check("health persisted=false", health.get("persisted") is False,
              repr(health.get("persisted")))
        check("health version", bool(health.get("version")), repr(health.get("version")))
        check("health window", health.get("window", {}).get("capacity") == 50,
              repr(health.get("window")))
        check("health subscribers is a list", isinstance(health.get("subscribers"), list),
              repr(health.get("subscribers")))
        check("health backlog is a number", isinstance(health.get("backlog"), int),
              repr(health.get("backlog")))

        # ---- FR-05: the token ----
        status, body = call(port, "POST", "/api/publish",
                            {"topic": "a2/test/x", "payload": {}})
        check("publish without token is 401", status == 401 and body.get("code") == "unauthorized",
              "%s %r" % (status, body))
        status, _ = call(port, "POST", "/api/publish",
                         {"topic": "a2/test/x", "payload": {}}, token="wrong")
        check("publish with a wrong token is 401", status == 401, str(status))
        reader = SseReader(port, "", "a2/test/#")
        check("subscribe without token is 401", reader.status == 401, str(reader.status))
        reader.close()

        # ---- FR-02 / FR-03: publish and subscribe ----
        reader = SseReader(port, token, "a2/test/#", since=0)
        check("subscribe status", reader.status == 200, "%s %s" % (reader.status, reader.error))
        ids = []
        for i in range(3):
            status, body = call(port, "POST", "/api/publish",
                                {"topic": "a2/test/hello", "payload": {"i": i}}, token=token)
            check("publish %d" % i, status == 200 and isinstance(body.get("id"), int),
                  "%s %r" % (status, body))
            ids.append(body.get("id"))
        frames = reader.wait(3, timeout=10)
        check("3 messages arrive", len(frames) == 3, "got %d" % len(frames))
        if len(frames) == 3:
            check("the ids go up", frames[0]["id"] < frames[1]["id"] < frames[2]["id"],
                  repr([f.get("id") for f in frames]))
            check("the order is the publish order",
                  [f["data"].get("payload", {}).get("i") for f in frames] == [0, 1, 2],
                  repr([f.get("data") for f in frames]))

        # ---- AC-02.2: the same key is the same message ----
        status1, first = call(port, "POST", "/api/publish",
                              {"topic": "a2/test/key", "payload": {"k": 1}, "key": "k1"},
                              token=token)
        status2, second = call(port, "POST", "/api/publish",
                               {"topic": "a2/test/key", "payload": {"k": 1}, "key": "k1"},
                               token=token)
        check("same key, same id", status1 == 200 and status2 == 200
              and first.get("id") == second.get("id"),
              "%r vs %r" % (first, second))
        check("the second answer is marked duplicate", second.get("duplicate") is True,
              repr(second))
        got = reader.wait(4, timeout=10, where=lambda f: f["data"].get("topic") == "a2/test/key")
        check("the duplicated key is delivered once", len(got) == 1, "got %d" % len(got))

        # ---- AC-02.3: bad input is refused, loudly ----
        for bad_topic in ["A2/x", "a2/", "a2//x", "a2/x y", "a2/**", "/a2/x", "a2/x/#"]:
            status, body = call(port, "POST", "/api/publish",
                                {"topic": bad_topic, "payload": {}}, token=token)
            check("bad topic %r is 400" % bad_topic,
                  status == 400 and body.get("code") == "bad_topic",
                  "%s %r" % (status, body))
        status, body = call(port, "POST", "/api/publish",
                            {"topic": "a2/test/x", "payload": [1, 2]}, token=token)
        check("a payload that is not an object is 400 bad_payload",
              status == 400 and body.get("code") == "bad_payload",
              "%s %r" % (status, body))
        status, body = call(port, "POST", "/api/publish",
                            {"topic": "a2/test/x", "payload": {}, "key": "a b"},
                            token=token)
        check("a key with a space is 400 bad_key",
              status == 400 and body.get("code") == "bad_key",
              "%s %r" % (status, body))
        big = {"blob": "x" * 4000}
        status, body = call(port, "POST", "/api/publish",
                            {"topic": "a2/test/big", "payload": big}, token=token)
        check("a payload over the limit is 413",
              status == 413 and body.get("code") == "payload_too_large",
              "%s %r" % (status, body))
        bad = SseReader(port, token, "a2/**")
        check("a bad filter is 400", bad.status == 400, "%s %s" % (bad.status, bad.error))
        bad.close()
        status, body = call(port, "GET", "/api/subscribe?filter=a2/%23&since=abc",
                            token=token)
        check("a bad since is 400", status == 400 and body.get("code") == "bad_since",
              "%s %r" % (status, body))

        # ---- AC-02.4: retain ----
        call(port, "POST", "/api/publish",
             {"topic": "a2/test/state", "payload": {"v": 1}, "retain": True}, token=token)
        late = SseReader(port, token, "a2/test/state", since=0)
        frames = late.wait(1, timeout=10)
        check("a retained message reaches a late subscriber",
              len(frames) == 1 and frames[0]["data"].get("payload", {}).get("v") == 1,
              repr(frames[:1]))
        late.close()

        # ---- AC-03.2: reconnect with since ----
        # The cursor comes from the server's newest id, not from "whatever
        # the reader thread happened to have appended yet": a race here
        # would test the wrong thing.
        last = (wait_health(port) or {}).get("window", {}).get("newest")
        if not isinstance(last, int):
            failures.append("no newest id to reconnect from")
            last = 0
        reader.close()
        time.sleep(0.3)
        call(port, "POST", "/api/publish",
             {"topic": "a2/test/again", "payload": {"n": 1}}, token=token)
        call(port, "POST", "/api/publish",
             {"topic": "a2/test/again", "payload": {"n": 2}}, token=token)
        again = SseReader(port, token, "a2/test/#", since=last)
        frames = again.wait(2, timeout=10, where=lambda f: f["event"] == "message")
        check("the reconnect gets exactly the missed messages",
              len([f for f in frames if f.get("id", 0) > last]) == 2,
              repr([f.get("data", {}).get("payload") for f in frames]))
        check("the reconnect repeats nothing",
              all(f.get("id", 0) > last for f in frames),
              repr([f.get("id") for f in frames]))
        again.close()

        # ---- AC-04.1 / AC-03.3: the window and the gap ----
        for i in range(60):
            call(port, "POST", "/api/publish",
                 {"topic": "a2/test/win", "payload": {"i": i}}, token=token)
        health = wait_health(port)
        window = health.get("window", {})
        check("the window is capped", window.get("messages") == 50, repr(window))
        oldest = window.get("oldest")
        check("the window has an oldest id", isinstance(oldest, int), repr(window))
        gap = SseReader(port, token, "a2/test/win", since=1)
        frames = gap.wait(2, timeout=10)
        check("a since outside the window starts with a gap frame",
              bool(frames) and frames[0].get("event") == "gap"
              and frames[0]["data"].get("reason") == "window",
              repr(frames[:1]))
        check("the backlog still follows the gap frame",
              len(frames) > 1 and frames[1].get("event") == "message",
              repr(frames[:2]))
        check("the gap says where the window starts",
              bool(frames) and frames[0].get("data", {}).get("oldest") == oldest,
              "%r vs %r" % (frames[:1], oldest))
        check("a gap frame carries no id",
              bool(frames) and "id" not in frames[0], repr(frames[:1]))
        gap.close()

        # ---- AC-03.4: a subscriber that stops reading is counted ----
        slow = socket.create_connection(("127.0.0.1", port), timeout=10)
        request = ("GET /api/subscribe?filter=a2/test/slow&since=0 HTTP/1.1\r\n"
                   "Host: 127.0.0.1\r\nAuthorization: Bearer %s\r\n\r\n" % token)
        slow.sendall(request.encode("utf-8"))
        time.sleep(0.3)
        for i in range(30):
            call(port, "POST", "/api/publish",
                 {"topic": "a2/test/slow", "payload": {"i": i}}, token=token)
        health = wait_health(port)

        def slow_dropped():
            for sub in health.get("subscribers") or []:
                if sub.get("filter") == "a2/test/slow" and sub.get("dropped", 0) > 0:
                    return sub
            return None

        sub = None
        deadline = time.time() + 5
        while time.time() < deadline and sub is None:
            health = wait_health(port) or health
            sub = slow_dropped()
            if sub is None:
                time.sleep(0.2)
        check("a slow subscriber is counted as dropping", sub is not None, repr(health.get("subscribers")))
        check("its backlog stays capped",
              sub is not None and sub.get("queued", 0) <= 5, repr(sub))
        slow.close()
        gone = False
        deadline = time.time() + 8
        while time.time() < deadline:
            health = wait_health(port) or {}
            if not any(s.get("filter") == "a2/test/slow"
                       for s in health.get("subscribers") or []):
                gone = True
                break
            time.sleep(0.3)
        check("a subscriber that is gone is dropped from /health", gone,
              repr((health or {}).get("subscribers")))

        # ---- AC-06.1: the heartbeat file and the heartbeat topic ----
        beat = os.path.join(data, "bus.heartbeat")
        check("the heartbeat file exists", os.path.exists(beat), beat)
        hb = SseReader(port, token, "a2/heartbeat/#", since=0)
        frames = hb.wait(1, timeout=8, where=lambda f: f["data"].get("topic") == "a2/heartbeat/bus")
        check("the bus publishes its own heartbeat", len(frames) >= 1, repr(frames[:1]))
        if frames:
            check("the heartbeat says who and when",
                  frames[0]["data"].get("payload", {}).get("pid")
                  and frames[0]["data"].get("payload", {}).get("at"),
                  repr(frames[0].get("data")))
        hb.close()

        # ---- AC-01.3 / AC-01.2: one instance per data dir, one per port ----
        second = start(free_port(), data, ["--window-messages", "10"])
        try:
            code = second.wait(timeout=15)
        except subprocess.TimeoutExpired:
            second.kill()
            code = None
        check("a second instance on the same data dir refuses to start", code == 3,
              "exit=%r" % (code,))
        other_data = os.path.join(tmp, "other")
        os.makedirs(other_data)
        clash = start(port, other_data)
        try:
            code = clash.wait(timeout=15)
        except subprocess.TimeoutExpired:
            clash.kill()
            code = None
        check("a second instance on the same port refuses to start", code == 3,
              "exit=%r" % (code,))

        # ---- AC-04.2 / FR-06: a restart forgets the window and keeps the token ----
        stop(server)
        server = None
        time.sleep(0.5)
        # A pid file left behind by a hard stop (Windows has no SIGTERM
        # delivery) must not block the next start - it is checked against
        # the pid, not trusted as a lock.
        server = start(port, data, [
            "--window-messages", "50", "--window-minutes", "60",
            "--keepalive", "1", "--heartbeat-file", "0.3",
            "--heartbeat-topic", "3600",
        ])
        health = wait_health(port)
        check("the restart took over the pid file",
              health is not None and isinstance(health.get("pid"), int)
              and health.get("pid") == server.pid,
              repr((health or {}).get("pid")))
        # The bus publishes its own heartbeat as soon as it starts, so
        # "forgot the window" has to be asked over the work topics:
        after = SseReader(port, token, "a2/test/#", since=0)
        frames = after.wait(1, timeout=1.5)
        after.close()
        check("the restart forgets the a2/test/* window",
              len(frames) == 0,
              repr([f.get("data", {}).get("topic") for f in frames]))
        check("the ids start again at 1 after a restart",
              (health or {}).get("window", {}).get("oldest") == 1,
              repr((health or {}).get("window")))
        with open(token_path, encoding="utf-8") as fh:
            check("the token survives a restart", fh.read().strip() == token)
        call(port, "POST", "/api/publish",
             {"topic": "a2/test/after", "payload": {"n": 1}}, token=token)
        old = SseReader(port, token, "a2/test/#", since=99999)
        frames = old.wait(1, timeout=10)
        check("a since from another run is answered with no_history",
              bool(frames) and frames[0].get("event") == "gap"
              and frames[0]["data"].get("reason") == "no_history",
              repr(frames[:1]))
        old.close()

        # ---- the version ----
        out = subprocess.check_output([sys.executable, SERVER, "--version"],
                                      stderr=subprocess.STDOUT).decode().strip()
        check("--version works", out and out.split(".")[0].isdigit(), out)
    finally:
        stop(server)
        shutil.rmtree(tmp, ignore_errors=True)

    if failures:
        print("FAIL")
        for f in failures:
            print(" -", f)
        return 1
    print("PASS: the platform publishes, subscribes, remembers the window and says what it lost")
    return 0


if __name__ == "__main__":
    sys.exit(main())
