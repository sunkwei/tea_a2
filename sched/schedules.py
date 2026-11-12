#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The schedules: the work that comes back on its own, minute by minute.

A schedule is a **recipe**, not a task: "every working day at 09:30, look at
the inbox and write a short summary". Every time the recipe's minute comes,
the queue makes a task out of it - the same task, over and over.

The recipe lives in its own file under <data dir>/schedules/, next to the
task records but not among them: the queue's task list reads every *.json of
its data directory, and a schedule is not a task.

The vocabulary:

    cron      when it happens ("*/5 * * * *") - see cron.py
    prompt    what it does (the text the agent gets)
    last_fired_at  the minute it last fired for (the missed minutes
                   collapse into one run, so a queue that was down does
                   not wake up to a flood of tasks)

Nothing here talks to the network: the schedule is validated, stored and
read here; the queue (sched/task_server.py) does the rest - it watches the
minutes, creates the tasks and writes down what happened.
"""

import calendar
import json
import os
import tempfile
import threading
import time

import cron

MAX_PROMPT = 4000
MAX_TITLE = 120


class ScheduleError(Exception):
    """The schedule cannot be made: the message says what is wrong."""


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def iso_from_epoch(epoch):
    """An ISO-8601 UTC stamp from epoch seconds."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(epoch)))


def parse_iso(text):
    """The epoch seconds of a stamp this module wrote ('' -> None)."""
    if not text:
        return None
    try:
        return int(calendar.timegm(time.strptime(text, "%Y-%m-%dT%H:%M:%SZ")))
    except (ValueError, OverflowError):
        return None


# The parsed expressions, kept around: the queue asks the same schedule the
# same question every round, and parsing is not free.
_CRON_CACHE = {}
_CRON_LOCK = threading.Lock()


def parse_cron(expression):
    """The expression -> a parsed cron. Raises ScheduleError."""
    text = str(expression or "").strip()
    with _CRON_LOCK:
        cached = _CRON_CACHE.get(text)
    if cached is not None:
        if isinstance(cached, ScheduleError):
            raise cached
        return cached
    try:
        parsed = cron.parse(text)
    except cron.CronError as exc:
        error = ScheduleError(str(exc))
        with _CRON_LOCK:
            _CRON_CACHE[text] = error
        raise error
    with _CRON_LOCK:
        _CRON_CACHE[text] = parsed
    return parsed


def _text(body, key, default=""):
    return str(body.get(key) if body.get(key) is not None else default).strip()


def _flag(body, key, default=True):
    """A boolean field: a web form sends "true"/"false" as text."""
    raw = body.get(key)
    if raw is None or raw == "":
        return default
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if text in ("1", "true", "yes", "on", "是", "开"):
        return True
    if text in ("0", "false", "no", "off", "否", "关"):
        return False
    raise ScheduleError("'%s' 要么是 true 要么是 false" % key)


def normalize(body):
    """The body of a schedule -> the fields of a schedule record.

    Everything that is wrong is said here, at the door: a schedule that
    cannot fire is not a schedule.
    """
    body = body if isinstance(body, dict) else {}
    expression = (_text(body, "cron") or _text(body, "expression")
                  or _text(body, "schedule") or _text(body, "expr"))
    if not expression:
        raise ScheduleError("要一个 cron 表达式（cron: \"*/5 * * * *\"）")
    parsed = parse_cron(expression)
    prompt = _text(body, "prompt")
    if not prompt:
        raise ScheduleError("要一件要做的事（prompt 是空的）")
    title = _text(body, "title") or prompt.splitlines()[0][:60]
    priority = body.get("priority")
    try:
        priority = int(priority or 0)
    except (TypeError, ValueError):
        raise ScheduleError("priority 要是一个整数")
    allow_paths = body.get("allow_paths")
    if isinstance(allow_paths, str):
        allow_paths = allow_paths.replace(";", ",").split(",")
    if allow_paths in (None, ""):
        allow_paths = []
    if not isinstance(allow_paths, list):
        raise ScheduleError("allow_paths 要是一个列表")
    meta = body.get("meta")
    if meta in (None, ""):
        meta = {}
    if not isinstance(meta, dict):
        raise ScheduleError("meta 要是一个对象")
    return {
        "cron": parsed.expression,
        "cron_written": expression,
        "prompt": prompt[:MAX_PROMPT],
        "title": title[:MAX_TITLE],
        "workspace_id": _text(body, "workspace_id") or _text(body, "workspace"),
        "provider": _text(body, "provider"),
        "model": _text(body, "model"),
        "priority": priority,
        "allow_paths": [str(one).strip() for one in allow_paths if str(one).strip()],
        "meta": dict(meta),
        # Off by default: a schedule that is off does not fire until
        # somebody turns it on.
        "enabled": _flag(body, "enabled", True),
        # Off by default: the work of one run must be finished before the
        # next one starts (a queue that piles up is a queue that drowns).
        "overlap": _flag(body, "overlap", False),
    }


def new_schedule(fields):
    """A schedule record, from the fields normalize() checked."""
    record = {
        "id": "sc_" + os.urandom(6).hex(),
        "tenant_id": "",
        "identity": "",
        "idempotency_key": "",
        "model_params": {},
        "limits": {},
        "policy": {},
        "created_at": now_iso(),
        "updated_at": now_iso(),
        # The minute it last fired for: "" = it never fired.
        "last_fired_at": "",
        "last_run_at": "",
        "last_task_id": "",
        "run_count": 0,
        "skipped_count": 0,
        "last_error": "",
    }
    record.update(fields)
    return record


def is_due(schedule, now=None):
    """Is this schedule due at this moment?

    "Due" means: there is a minute of the expression between the last time
    it fired (or the time it was made) and now. The minutes that were
    missed - the queue was down, or it was busy - collapse into one run:
    a schedule that was asleep for an hour does not wake up to sixty tasks.
    """
    try:
        parsed = parse_cron(schedule.get("cron") or "")
    except ScheduleError:
        return False
    now = time.time() if now is None else float(now)
    last = parse_iso(schedule.get("last_fired_at") or "") or \
        parse_iso(schedule.get("created_at") or "") or 0
    previous = parsed.prev_at_or_before(now)
    if previous is None:
        return False
    return previous > last


def next_fire(schedule, now=None):
    """The epoch of the next minute of this schedule (None when there is none)."""
    try:
        parsed = parse_cron(schedule.get("cron") or "")
    except ScheduleError:
        return None
    now = time.time() if now is None else float(now)
    return parsed.next_after(now)


class ScheduleStore(object):
    """The schedule records: one file per schedule, in their own directory."""

    def __init__(self, root):
        self.root = os.path.abspath(os.path.expanduser(root))
        self.schedules_dir = os.path.join(self.root, "schedules")
        self.lock = threading.RLock()
        try:
            os.makedirs(self.schedules_dir, exist_ok=True)
        except OSError:
            pass

    def _path(self, schedule_id):
        return os.path.join(self.schedules_dir, schedule_id + ".json")

    def new_schedule_id(self):
        return "sc_" + os.urandom(6).hex()

    def save(self, schedule):
        with self.lock:
            try:
                os.makedirs(self.schedules_dir, exist_ok=True)
                schedule["updated_at"] = now_iso()
                fd, tmp = tempfile.mkstemp(dir=self.schedules_dir, prefix=".schedule-")
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(schedule, f, ensure_ascii=False, indent=2)
                os.replace(tmp, self._path(schedule.get("id") or ""))
            except OSError:
                pass
        return schedule

    def load(self, schedule_id):
        if not schedule_id or not isinstance(schedule_id, str):
            return None
        try:
            with open(self._path(schedule_id), encoding="utf-8") as f:
                loaded = json.load(f)
        except (OSError, ValueError):
            return None
        if not isinstance(loaded, dict) or not loaded.get("id"):
            return None
        return loaded

    def list_schedules(self, workspace_id="", limit=100):
        """The schedules, newest first."""
        out = []
        try:
            names = os.listdir(self.schedules_dir)
        except OSError:
            return []
        for name in names:
            if not name.endswith(".json"):
                continue
            schedule = self.load(name[:-5])
            if not schedule:
                continue
            if workspace_id and schedule.get("workspace_id") != workspace_id:
                continue
            out.append(schedule)
        out.sort(key=lambda s: (s.get("created_at") or "", s.get("id") or ""),
                 reverse=True)
        return out[:limit] if limit else out

    def delete(self, schedule_id):
        """The schedule is gone. True when there was one."""
        path = self._path(schedule_id)
        with self.lock:
            if not os.path.exists(path):
                return False
            try:
                os.remove(path)
            except OSError:
                return False
        return True

    def find_by_key(self, key):
        """The schedule a repeated submission with the same key must return."""
        if not key:
            return None
        for schedule in self.list_schedules(limit=200):
            if schedule.get("idempotency_key") == key:
                return schedule
        return None
