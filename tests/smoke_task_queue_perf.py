#!/usr/bin/env python3
"""The queue must not burn a core when it is busy.

There was a shape that made a2_task.service sit at 100% CPU: a long task
holds the project, the queue behind it is full, and every question the
scheduler asked ("is this project busy?") was answered by reading every
record of the ledger. That is quadratic: the more work waited, the more CPU
it took to look at the queue - and the answer never came in time, so the
scheduler never rested.

The same shape appeared in the task list (every task asked for its place in
the line, and every question read the whole ledger) and in the event log
(every event re-read the whole log to number itself).

These checks measure the work, not the shape: a slow disk may be slow, but
quadratic is quadratic on every disk.

  python3 tests/smoke_task_queue_perf.py
"""

import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "sched"))

import task_server as ts  # noqa: E402

QUEUED_N = 800
ROUND_LIMIT = 3.0        # seconds for one round of the scheduler
LIST_LIMIT = 3.0         # seconds for the whole task list
EVENTS_N = 3000
EVENTS_LIMIT = 2.5       # seconds for 3000 events of one task

failed = []


def check(ok, what):
    print("%s %s" % ("PASS" if ok else "FAIL", what))
    if not ok:
        failed.append(what)


class FakeAgent:
    def usage(self, provider):
        return {"window": {"remaining_percent": 100.0}}

    def workspaces(self):
        return [{"id": "ws_shared", "path": "/tmp/ws", "name": "ws"}]


def build(root, n):
    store = ts.Store(root)
    for i in range(n):
        task = ts.new_task({"prompt": "x" * 100}, "ws_shared")
        task["id"] = "t_%05d" % i
        task["status"] = ts.QUEUED
        task["created_at"] = "2026-10-28T00:00:00Z"
        store.create(task)
    # One task holds the project: every queued task has to wait for it.
    running = ts.new_task({"prompt": "x"}, "ws_shared")
    running["id"] = "t_running"
    running["status"] = ts.RUNNING
    running["updated_at"] = ts.now_iso()
    store.create(running)
    return store


def main():
    root = tempfile.mkdtemp(prefix="task-perf-")
    try:
        store = build(root, QUEUED_N)
        args = ts.argparse.Namespace(
            provider="minimax", window="window", min_remaining=5.0,
            allow_without_quota=True, max_concurrent=0, quota_interval=60.0,
            stale_after=300.0, interval=2.0, max_per_round=4, workspace="ws_shared",
            workspace_path=root, workspace_name="perf", agent="http://127.0.0.1:1",
            data_dir=root, chat_timeout=3600.0, sse_idle_timeout=3600.0)
        engine = ts.Engine(store, FakeAgent(), ts.Events(store), args)
        app = ts.App(args)
        app.store = store

        # ---- one round of the scheduler --------------------------------
        start = time.time()
        engine.round()
        round_took = time.time() - start
        print("     one round with %d queued tasks: %.2f s" % (QUEUED_N, round_took))
        check(round_took < ROUND_LIMIT,
              "one round with %d queued tasks stays under %.0f s (took %.2f s)"
              % (QUEUED_N, ROUND_LIMIT, round_took))

        # ---- the task list --------------------------------------------
        tasks = store.list()
        # `order` is the queue order of the answer; a version of the server
        # that does not have it computes it per task (that is the bug).
        order = app.queue_order() if hasattr(app, "queue_order") else None
        start = time.time()
        if order is None:
            for one in tasks:
                app.decorate(one)
        else:
            for one in tasks:
                app.decorate(one, order=order)
        list_took = time.time() - start
        print("     the list of %d tasks: %.2f s" % (len(tasks), list_took))
        check(list_took < LIST_LIMIT,
              "the list of %d tasks stays under %.0f s (took %.2f s)"
              % (len(tasks), LIST_LIMIT, list_took))

        # ---- the queue positions are still right ----------------------
        positions = [app.queue_position(one, order) if order is not None
                     else app.queue_position(one) for one in tasks
                     if one.get("status") == ts.QUEUED]
        check(len(positions) == QUEUED_N and sorted(positions) == list(range(1, QUEUED_N + 1)),
              "every queued task has its own place in the line")

        # ---- the event log ---------------------------------------------
        task_id = "t_evt"
        task = ts.new_task({"prompt": "x"}, "ws_shared")
        task["id"] = task_id
        store.create(task)
        start = time.time()
        for i in range(EVENTS_N):
            store.record_event(task_id, "task_updated", {"i": i})
        events_took = time.time() - start
        print("     %d events of one task: %.2f s" % (EVENTS_N, events_took))
        check(events_took < EVENTS_LIMIT,
              "%d events of one task stay under %.1f s (took %.2f s)"
              % (EVENTS_N, EVENTS_LIMIT, events_took))

        events = store.events(task_id)
        check([e["event_id"] for e in events] == list(range(1, EVENTS_N + 1)),
              "the events are numbered without a gap")

        # ---- the numbering survives a restart -------------------------
        fresh = ts.Store(root)
        event = fresh.record_event(task_id, "task_updated", {"i": EVENTS_N})
        check(event["event_id"] == EVENTS_N + 1,
              "a fresh process continues the numbering (got %d)" % event["event_id"])
    finally:
        shutil.rmtree(root, ignore_errors=True)

    if failed:
        print("\n%d check(s) failed" % len(failed))
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
