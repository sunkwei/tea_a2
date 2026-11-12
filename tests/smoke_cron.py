#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Smoke test for the cron schedules: the work that comes back on its own.

What this pins:

  * the expression is read the way a crontab reads it (every five minutes,
    the working days, the macros) and a broken one is refused with a
    message, not a crash;
  * the finest grain is **one minute**: "every minute" is the fastest thing
    a schedule can do, and the next minute is always within the next one;
  * a schedule becomes a task when its minute comes, and the task knows
    which schedule it came from;
  * the work of the last run must be finished before the next one starts
    (a queue that piles up is a queue that drowns);
  * a schedule can be paused, resumed and deleted;
  * the minutes that were missed (the queue was down) collapse into one
    run: a schedule that was asleep does not wake up to a flood of tasks.

Usage: python3 tests/smoke_cron.py [path-to-tea_agent-binary]
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
sys.path.insert(0, os.path.join(REPO_ROOT, "sched"))

import cron  # noqa: E402
import schedules  # noqa: E402


class MockAgent(BaseHTTPRequestHandler):
    """A fake agent: the health check, the quota, the workspaces, the turns."""

    records = {"chat": 0, "usage": 0, "order": []}
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
        if self.path == "/api/chat":
            MockAgent.records["chat"] += 1
            try:
                body = json.loads(raw or b"{}")
            except ValueError:
                body = {}
            message = body.get("message") or ""
            MockAgent.records["order"].append(message)
            if "慢活儿" in message:
                # A slow piece of work: the test needs a run that is still
                # going when the next minute comes.
                time.sleep(5)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            frames = [{"type": "start", "session_id": "sess_mock"},
                      {"type": "done", "content": "「%s」做完了"
                       % message.splitlines()[0][:20]}]
            for frame in frames:
                try:
                    self.wfile.write(("data: " + json.dumps(frame) + "\n\n").encode())
                    self.wfile.flush()
                except OSError:
                    return
            return
        if self.path == "/api/llm/chat":
            return self._json({"ok": True, "provider": "mock", "model": "mock",
                               "content": "", "reasoning": "", "finish_reason": "stop"})
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


def call(port, method, path, body=None, timeout=30, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    data = json.dumps(body).encode() if body is not None else None
    head = {"Content-Type": "application/json"} if data else {}
    if headers:
        head.update(headers)
    conn.request(method, path, body=data, headers=head)
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


def fast_forward(data_dir, schedule_id, minutes=10):
    """Pretend the schedule last fired minutes ago.

    The queue watches the minutes of the wall clock; the test cannot wait
    for them (a minute is a minute), so it moves the schedule's own history
    instead: the next minute it names is due right now.
    """
    path = os.path.join(data_dir, "schedules", schedule_id + ".json")
    with open(path, encoding="utf-8") as f:
        record = json.load(f)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - minutes * 60))
    record["last_fired_at"] = stamp
    record["created_at"] = stamp
    with open(path, "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False, indent=2)
    return record


def wait_for(predicate, timeout=20.0, step=0.3):
    """The first value the predicate likes (None when the time is up)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(step)
    return None


def main():
    failures = []

    def check(name, ok, detail=""):
        print(("PASS " if ok else "FAIL ") + name + (("  " + str(detail)) if not ok else ""))
        if not ok:
            failures.append(name)

    # ---- the expression itself (no network) ----------------------------
    every_five = cron.parse("*/5 * * * *")
    check("every five minutes is every five minutes",
          sorted(every_five.minutes) == [0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55],
          sorted(every_five.minutes))
    workday = cron.parse("30 9 * * 1-5")
    check("a working day at 09:30 is read right",
          sorted(workday.hours) == [9] and sorted(workday.minutes) == [30]
          and sorted(workday.dows) == [1, 2, 3, 4, 5],
          (sorted(workday.hours), sorted(workday.minutes), sorted(workday.dows)))
    check("the macros are the expressions they stand for",
          cron.parse("@daily").expression == "0 0 * * *"
          and cron.parse("@hourly").expression == "0 * * * *",
          cron.parse("@daily").expression)
    check("the months and the weekdays have names",
          sorted(cron.parse("0 0 1 JAN *").months) == [1]
          and sorted(cron.parse("0 0 * * SUN").dows) == [0],
          "names")

    for bad in ["", "* * * *", "60 * * * *", "a b c d e", "*/0 * * * *", "5-1 * * * *"]:
        try:
            cron.parse(bad)
            check("the broken expression %r is refused" % bad, False)
        except cron.CronError as exc:
            check("the broken expression %r is refused" % bad, bool(str(exc)), exc)

    # ---- the grain is one minute ---------------------------------------
    moment = int(time.mktime((2026, 11, 12, 10, 30, 0, 0, 0, -1)))
    quarter = cron.parse("*/15 * * * *")
    check("the next quarter hour is the next one",
          time.strftime("%H:%M", time.localtime(quarter.next_after(moment))) == "10:45",
          time.strftime("%H:%M", time.localtime(quarter.next_after(moment))))
    check("the last quarter hour is the one before",
          time.strftime("%H:%M", time.localtime(quarter.prev_at_or_before(moment))) == "10:30",
          time.strftime("%H:%M", time.localtime(quarter.prev_at_or_before(moment))))
    every_minute = cron.parse("* * * * *")
    now = time.time()
    nxt = every_minute.next_after(now)
    check("the finest grain is one minute",
          nxt is not None and 0 < nxt - now <= 60, (nxt, now))
    check("a minute that just passed is not the next one",
          every_minute.prev_at_or_before(now) <= now
          and every_minute.prev_at_or_before(now) > now - 60, now)

    # ---- when is a schedule due -----------------------------------------
    old = {"cron": "* * * * *", "created_at": "2026-11-12T10:00:00Z",
           "last_fired_at": ""}
    check("a schedule that never fired is due when its minute comes",
          schedules.is_due(old, time.time()) is True)
    just_fired = {"cron": "* * * * *", "created_at": "2026-11-12T10:00:00Z",
                  "last_fired_at": schedules.iso_from_epoch(time.time())}
    check("a schedule that just fired waits for the next minute",
          schedules.is_due(just_fired, time.time()) is False)

    tmp = tempfile.mkdtemp(prefix="tea_cron_")
    ws_dir = os.path.join(tmp, "ws")
    os.makedirs(ws_dir)
    MockAgent.ws_dir = ws_dir
    data_dir = os.path.join(tmp, "tasks")

    agent_port = free_port()
    queue_port = free_port()
    agent = ThreadingHTTPServer(("127.0.0.1", agent_port), MockAgent)
    threading.Thread(target=agent.serve_forever, daemon=True).start()

    queue = subprocess.Popen(
        [
            sys.executable,
            SERVER,
            "--agent", "http://127.0.0.1:%d" % agent_port,
            "--host", "127.0.0.1",
            "--port", str(queue_port),
            "--provider", "mock",
            "--data-dir", data_dir,
            "--workspace", "ws_mock",
            "--interval", "1",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        if not wait_health(queue_port):
            print("the queue did not become healthy")
            return 1

        # ---- a schedule is accepted --------------------------------------
        status, answer = call(queue_port, "POST", "/api/schedules",
                              {"cron": "* * * * *", "prompt": "慢活儿：定时做点事",
                               "title": "每分钟"})
        check("a schedule is accepted", status == 200, answer)
        schedule = (answer or {}).get("data") or {}
        schedule_id = schedule.get("id") or ""
        check("the schedule has an id", bool(schedule_id), schedule)
        check("the schedule is on", schedule.get("status") == "enabled", schedule.get("status"))
        check("the schedule knows when it fires next",
              bool(schedule.get("next_fire_local")), schedule.get("next_fire_local"))

        status, answer = call(queue_port, "POST", "/api/schedules",
                              {"cron": "nope", "prompt": "x"})
        check("a broken expression is refused by the API", status == 400, status)
        status, answer = call(queue_port, "POST", "/api/schedules",
                              {"cron": "* * * * *"})
        check("a schedule without work is refused by the API", status == 400, status)
        status, answer = call(queue_port, "GET", "/api/schedules")
        check("the schedules are listed",
              (answer or {}).get("data", {}).get("count", 0) == 1, answer)

        # ---- the minute comes: the work becomes a task -------------------
        fast_forward(data_dir, schedule_id)

        def task_of_the_schedule():
            status, answer = call(queue_port, "GET", "/api/tasks")
            tasks = (answer or {}).get("data", {}).get("tasks") or []
            for one in tasks:
                if (one.get("meta") or {}).get("schedule_id") == schedule_id:
                    return one
            return None

        task = wait_for(task_of_the_schedule, timeout=30)
        check("the schedule became a task", task is not None, "no task yet")
        if task:
            check("the task knows its schedule",
                  (task.get("meta") or {}).get("schedule_id") == schedule_id,
                  task.get("meta"))
            check("the task says what to do",
                  "定时做点事" in (task.get("prompt") or ""), task.get("prompt"))
            check("the task is in the queue", task.get("status") in ("queued", "running"),
                  task.get("status"))

        status, answer = call(queue_port, "GET", "/api/schedules/" + schedule_id)
        view = (answer or {}).get("data") or {}
        check("the schedule says it ran", int(view.get("run_count") or 0) == 1,
              view.get("run_count"))
        check("the schedule knows its task", bool(view.get("last_task_id")),
              view.get("last_task_id"))

        # ---- the work of the last run must be finished ------------------
        fast_forward(data_dir, schedule_id)

        def skipped():
            status, answer = call(queue_port, "GET", "/api/schedules/" + schedule_id)
            one = (answer or {}).get("data") or {}
            return one if int(one.get("skipped_count") or 0) >= 1 else None

        note = wait_for(skipped, timeout=20)
        check("a run that is still going makes the next one wait", note is not None,
              (answer or {}).get("data"))
        check("the schedule says why it waited",
              "还没做完" in ((note or {}).get("last_error") or ""),
              (note or {}).get("last_error"))

        # ---- the work is really done -------------------------------------
        def finished():
            status, answer = call(queue_port, "GET", "/api/tasks/" + (task or {}).get("id", ""))
            one = (answer or {}).get("data") or {}
            return one if one.get("status") in ("done", "failed", "cancelled") else None

        done = wait_for(finished, timeout=60)
        check("the scheduled work runs to the end",
              done is not None and done.get("status") == "done",
              (done or {}).get("status"))

        # ---- a schedule can be paused, resumed and deleted --------------
        status, answer = call(queue_port, "POST", "/api/schedules/%s/pause" % schedule_id)
        check("a schedule can be paused", status == 200, answer)
        check("the paused schedule says so",
              ((answer or {}).get("data") or {}).get("status") == "paused",
              (answer or {}).get("data"))
        status, answer = call(queue_port, "POST", "/api/schedules/%s/resume" % schedule_id)
        check("a schedule can be resumed", status == 200, answer)
        check("the resumed schedule says so",
              ((answer or {}).get("data") or {}).get("status") == "enabled",
              (answer or {}).get("data"))

        status, answer = call(queue_port, "DELETE", "/api/schedules/" + schedule_id)
        check("a schedule can be deleted", status == 200, answer)
        status, answer = call(queue_port, "GET", "/api/schedules/" + schedule_id)
        check("a deleted schedule is a 404", status == 404, status)
        status, answer = call(queue_port, "GET", "/api/schedules")
        check("the deleted schedule is gone",
              (answer or {}).get("data", {}).get("count", 0) == 0, answer)
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
    print("PASS: a schedule reads its minute, makes a task and waits for the last one")
    return 0


if __name__ == "__main__":
    sys.exit(main())
