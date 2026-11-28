#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The bridge: whatever the queue announces is published to the platform.

The queue's own event stream is the truth about work - it is where tasks are
created, finished and failed. It is also a plain broadcast: whoever is not
connected at the moment an event happens never hears about it. So one small
process sits next to the queue, follows that stream and republishes the three
events an observer outside the queue needs onto the platform:

    task_created -> a2/queue/task
    task_done    -> a2/queue/task_done
    task_failed  -> a2/queue/task_failed

Everything else stays on the queue stream (the router still reads that one;
`docs/message-platform.md` Q2 = both). Publishing a `task_cancelled` as
"task_failed" would be a lie, and inventing a topic is not allowed without
registering it - so those are counted and skipped, not guessed.

What it cannot do: replay. The queue's stream has no `since`, so when this
process or the queue was down, the events that happened then are gone. The
bridge does not pretend otherwise: every reconnect is written down as a gap
(`<data>/pump-state.json`, last 20) with the window it could not cover, and
`--status` prints them. Silence would look like "nothing happened".

Usage:

  python3 sched/bus_pump.py                      # follow the queue, publish to the bus
  python3 sched/bus_pump.py --status             # what did it see, what did it miss
  python3 sched/bus_pump.py --once --once-timeout 3   # for tests

Standard library only, Python 3.6.
"""

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request

if sys.version_info[0] < 3:
    sys.stderr.write("这个脚本要 Python 3：请用 python3 跑"
                     "（这台机器的 python 是 2.7）。\n")
    sys.exit(2)

VERSION = "1.0.0"

TEA_HOME = os.path.expanduser(os.environ.get("TEA_AGENT_HOME", os.path.join("~", ".tea_agent")))
DEFAULT_TASKS = os.environ.get("TEA_TASK_URL", "http://127.0.0.1:8283")
DEFAULT_BUS = os.environ.get("TEA_BUS_URL", "http://127.0.0.1:8299")

# The only queue events that have a topic on the bus. A type that is not here
# is counted and dropped on purpose (see the docstring).
TOPIC_OF = {
    "task_created": "a2/queue/task",
    "task_done": "a2/queue/task_done",
    "task_failed": "a2/queue/task_failed",
}

MAX_GAPS = 20
RESULT_SNIPPET = 2000
ERROR_SNIPPET = 1000


def now_iso():
    millis = int((time.time() % 1) * 1000)
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + (".%03dZ" % millis)


def log(message):
    print("%s  %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), message), flush=True)


_LOG_LAST = {}


def log_throttled(key, message, interval=60.0):
    now = time.time()
    last = _LOG_LAST.get(key)
    if last is not None and (now - last) < interval:
        return False
    _LOG_LAST[key] = now
    log(message)
    return True


def http_json(url, method="GET", body=None, timeout=10, headers=None):
    """One JSON request -> (status, parsed). Never raises."""
    data = None
    head = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        head["Content-Type"] = "application/json"
    if headers:
        head.update(headers)
    req = urllib.request.Request(url, data=data, headers=head, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            status = resp.status
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        status = exc.code
    except urllib.error.URLError as exc:
        return 0, {"error": "cannot reach %s: %s" % (url, exc.reason)}
    except Exception as exc:  # noqa: BLE001 - socket timeouts and the like
        return 0, {"error": "cannot reach %s: %s" % (url, exc)}
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, {"error": raw[:300]}


def read_token(path, given=""):
    if given:
        return given
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


class PumpState(object):
    """What the bridge has done, and which windows it could not cover.

    The file is small and rewritten atomically; the point of keeping it is that
    a gap must outlive the process that discovered it. Losing a few seconds of
    counters on a hard stop is fine - losing the gap is not.
    """

    def __init__(self, path):
        self.path = path
        self.data = {
            "started_at": now_iso(),
            "last_event_at": "",
            "last_publish_at": "",
            "published": 0,
            "skipped": 0,
            "failed": 0,
            "gaps": [],
        }
        self._dirty = False
        self._last_save = 0.0
        try:
            with open(path, encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                for key in self.data:
                    if key in loaded:
                        self.data[key] = loaded[key]
        except (OSError, ValueError):
            pass

    def save(self, force=False):
        now = time.time()
        if not force and now - self._last_save < 2.0:
            self._dirty = True
            return
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh, ensure_ascii=False, indent=2)
                fh.write("\n")
            os.replace(tmp, self.path)
            self._last_save = now
            self._dirty = False
        except OSError as exc:
            log_throttled("state-save", "pump: cannot write %s: %s" % (self.path, exc))

    def record_event(self, at):
        self.data["last_event_at"] = at
        self.save()

    def record_gap(self, from_at, to_at, reason):
        gap = {"from_at": from_at, "to_at": to_at, "reason": reason}
        gaps = self.data.setdefault("gaps", [])
        gaps.append(gap)
        while len(gaps) > MAX_GAPS:
            gaps.pop(0)
        self.save(force=True)
        log("pump: gap %s -> %s (%s) - events in that window are gone"
            % (from_at or "?", to_at, reason))


def event_key(event, task):
    """The idempotency key of one queue event.

    Same task, same type, same `updated_at` = the same event: a reconnect that
    (hypothetically) redelivers it must not publish it twice. Without a
    timestamp the event's own bytes are the identity.
    """
    task_id = str(task.get("id") or "")
    kind = str(event.get("type") or "")
    stamp = str(task.get("updated_at") or task.get("created_at") or "")
    if not stamp:
        raw = json.dumps(event, ensure_ascii=False, sort_keys=True).encode("utf-8")
        stamp = hashlib.sha1(raw).hexdigest()[:16]
    return "queue:%s:%s:%s" % (task_id, kind, stamp)


def slim(event):
    """What an observer needs, not the whole task record.

    The prompt and the full result can be bigger than the platform's payload
    limit; the queue keeps them, this is the copy that travels.
    """
    task = event.get("task") or {}
    kind = str(event.get("type") or "")
    out = {
        "type": kind,
        "at": now_iso(),
        "task": {
            "id": task.get("id"),
            "title": (task.get("title") or "")[:200],
            "status": task.get("status"),
            "kind": task.get("kind"),
            "workspace_id": task.get("workspace_id"),
            "created_at": task.get("created_at"),
            "updated_at": task.get("updated_at"),
        },
    }
    if kind == "task_done":
        out["task"]["result"] = (task.get("result") or "")[:RESULT_SNIPPET]
    if kind == "task_failed":
        out["task"]["error"] = (task.get("error") or "")[:ERROR_SNIPPET]
    return out


class Pump(object):
    def __init__(self, args, token):
        self.args = args
        self.tasks = args.tasks.rstrip("/")
        self.bus = args.bus.rstrip("/")
        self.token = token
        self.state = PumpState(os.path.join(args.data_dir, "pump-state.json"))
        self.stopped = False
        self.gap_from = ""

    # -- publishing -------------------------------------------------------
    def publish_event(self, event):
        kind = str(event.get("type") or "")
        topic = TOPIC_OF.get(kind)
        if not topic:
            self.state.data["skipped"] = int(self.state.data.get("skipped") or 0) + 1
            return False
        task = event.get("task") or {}
        body = {
            "topic": topic,
            "payload": slim(event),
            "key": event_key(event, task),
        }
        headers = {}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        status, answer = http_json(self.bus + "/api/publish", "POST", body,
                                   timeout=self.args.publish_timeout, headers=headers)
        if status == 200:
            self.state.data["published"] = int(self.state.data.get("published") or 0) + 1
            self.state.data["last_publish_at"] = now_iso()
            self.state.record_event(now_iso())
            at = (task.get("updated_at") or "")[:19]
            log("pump: %s %s %s" % (kind, task.get("id") or "?", at))
            return True
        self.state.data["failed"] = int(self.state.data.get("failed") or 0) + 1
        if status == 0:
            log_throttled("bus-down", "pump: the bus is unreachable (%s); this event is "
                                      "not retried - the queue keeps it" % answer.get("error"))
        else:
            log_throttled("bus-refused-%s" % status,
                          "pump: the bus refused %s: %s %s" % (kind, status, answer))
        return False

    # -- following the queue ------------------------------------------------
    def follow_once(self, timeout):
        """Read the queue's stream until it ends (or `timeout` seconds pass)."""
        url = self.tasks + "/api/events"
        req = urllib.request.Request(url, headers={"Accept": "text/event-stream"})
        started = time.time()
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            for raw in resp:
                if self.stopped:
                    return
                if timeout and time.time() - started > timeout:
                    return
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue                    # keepalive comments, blank lines
                payload = line[5:].strip()
                if not payload or payload == "[DONE]":
                    continue
                try:
                    event = json.loads(payload)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                self.publish_event(event)

    def run(self):
        log("pump %s: %s -> %s" % (VERSION, self.tasks, self.bus))
        backoff = self.args.retry
        while not self.stopped:
            self.gap_from = self.state.data.get("last_event_at") or ""
            try:
                self.follow_once(self.args.once_timeout if self.args.once else self.args.read_timeout)
                reason = "stream_ended"
            except Exception as exc:  # noqa: BLE001 - the stream ends all the time
                reason = "queue_unreachable: %s" % exc
                log_throttled("queue-down", "pump: the queue is unreachable (%s)" % exc)
            if self.args.once:
                break
            # A stream that ended is a window we could not see. Say so, even if
            # nothing happened in it: "we were not there" is a fact worth having.
            self.state.record_gap(self.gap_from, now_iso(), reason)
            time.sleep(backoff)
            backoff = min(self.args.max_retry, backoff * 1.6)
        self.state.save(force=True)

    def status(self):
        data = self.state.data
        print("pump %s" % VERSION)
        print("last event  : %s" % (data.get("last_event_at") or "-"))
        print("last publish: %s" % (data.get("last_publish_at") or "-"))
        print("published   : %s" % data.get("published", 0))
        print("skipped     : %s  (event types with no topic on the bus)" % data.get("skipped", 0))
        print("failed      : %s  (the bus refused or was unreachable)" % data.get("failed", 0))
        gaps = data.get("gaps") or []
        print("gaps        : %d" % len(gaps))
        for gap in gaps[-5:]:
            print("  %s -> %s (%s)" % (gap.get("from_at") or "?", gap.get("to_at"),
                                       gap.get("reason")))


def build_parser():
    parser = argparse.ArgumentParser(
        description="Bridge the queue's events onto the message platform.")
    parser.add_argument("--tasks", default=DEFAULT_TASKS,
                        help="the task server's HTTP API (default %s)" % DEFAULT_TASKS)
    parser.add_argument("--bus", default=DEFAULT_BUS,
                        help="the platform's URL (default %s)" % DEFAULT_BUS)
    parser.add_argument("--bus-token", default="",
                        help="the token (default: <data-dir>/token)")
    parser.add_argument("--data-dir", default=None,
                        help="where the token and pump-state.json live "
                             "(default <tea home>/bus)")
    parser.add_argument("--retry", type=float, default=2.0,
                        help="seconds before the first reconnect (default 2)")
    parser.add_argument("--max-retry", type=float, default=10.0,
                        help="the longest wait between reconnects (default 10)")
    parser.add_argument("--read-timeout", type=float, default=120.0,
                        help="seconds without a byte before the stream is read again "
                             "(default 120; the queue sends a keepalive every 30)")
    parser.add_argument("--publish-timeout", type=float, default=10.0,
                        help="seconds for one publish request (default 10)")
    parser.add_argument("--once", action="store_true",
                        help="follow the stream once and exit (tests)")
    parser.add_argument("--once-timeout", type=float, default=3.0,
                        help="with --once: how long to read before exiting (default 3)")
    parser.add_argument("--status", action="store_true",
                        help="print what the bridge has seen and missed, then exit")
    parser.add_argument("--version", action="store_true", help="print the version and exit")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.version:
        print(VERSION)
        return 0
    args.data_dir = os.path.abspath(os.path.expanduser(
        args.data_dir or os.environ.get("TEA_BUS_DATA") or os.path.join(TEA_HOME, "bus")))
    os.makedirs(args.data_dir, exist_ok=True)
    token = read_token(os.path.join(args.data_dir, "token"), args.bus_token)
    pump = Pump(args, token)
    if args.status:
        pump.status()
        return 0
    try:
        pump.run()
    except KeyboardInterrupt:
        log("pump: stopping")
    return 0


if __name__ == "__main__":
    sys.exit(main())
