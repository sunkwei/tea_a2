#!/usr/bin/env python3
"""The chat front end must answer for a **cancelled** task too.

A cancelled task used to be dropped by the channel: the queue publishes
"task_cancelled" (task_server.py), the front end did not know that name,
and the person who asked got **nothing at all** - no answer, no excuse,
silence.  A task that is stopped is still an answer ("it was cancelled"),
so it goes out like the done/failed ones.

    python3 tests/smoke_channel_events.py
"""

import os
import sys
import tempfile
import types

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "sched"))

import channel  # noqa: E402

failures = []
checks = []


def check(name, ok, detail=""):
    checks.append(name)
    print(("PASS " if ok else "FAIL ") + name +
          ((("  [" + str(detail) + "]")) if not ok else ""))
    if not ok:
        failures.append(name)


class FakeChannel(channel.TaskChannel):
    """A channel that records what it would send."""

    def __init__(self):
        args = types.SimpleNamespace(tasks="http://127.0.0.1:1", agent="http://127.0.0.1:1",
                                     speech=False, workspace_path=tempfile.mkdtemp())
        super().__init__(args)
        self.sent = []

    def send_task_files(self, user_id, task):
        """No files here: this test is about the text."""
        return 0

    def send_text(self, user_id, text):
        self.sent.append((user_id, text))
        return True

    def recipient_of(self, task):
        return "u-test"


def event(kind, **task):
    t = {"id": "t-1"}
    t.update(task)
    return {"type": kind, "task": t}


def main():
    c = FakeChannel()

    # ---- the bug: a cancelled task was dropped -------------------------
    c.sent = []
    sent = c.notify_event(event("task_cancelled", error="用户停掉了"))
    check("a cancelled task sends a message", sent is True and len(c.sent) == 1,
          "sent=%r" % (c.sent,))
    if c.sent:
        text = c.sent[0][1]
        check("the message says it was cancelled", "取消" in text, text)
        check("the message names the task", "t-1" in text, text)
        check("the message carries the reason", "用户停掉了" in text, text)

    # the reason may come from other fields too
    for field in ("stop_reason", "note"):
        c.sent = []
        c.notify_event(event("task_cancelled", **{field: "因为" + field}))
        ok = len(c.sent) == 1 and ("因为" + field) in c.sent[0][1]
        check("the reason can come from %s" % field, ok, repr(c.sent))

    c.sent = []
    c.notify_event(event("task_cancelled"))
    check("a cancelled task without a reason still answers",
          len(c.sent) == 1 and "取消" in c.sent[0][1], repr(c.sent))

    # ---- the others keep working ---------------------------------------
    c.sent = []
    c.notify_event(event("task_done", result="做完了"))
    check("a finished task still sends its result",
          len(c.sent) == 1 and "做完了" in c.sent[0][1], repr(c.sent))

    c.sent = []
    c.notify_event(event("task_failed", error="炸了"))
    check("a failed task still sends the error",
          len(c.sent) == 1 and "炸了" in c.sent[0][1], repr(c.sent))

    c.sent = []
    c.notify_event(event("task_waiting_confirmation", question="要哪个？"))
    check("a task waiting for an answer still asks",
          len(c.sent) == 1 and "要哪个" in c.sent[0][1], repr(c.sent))

    # ---- what is not ours stays out -------------------------------------
    c.sent = []
    ok = c.notify_event(event("speech", text="说句话")) is False
    check("an event that is not a task result is not sent here",
          ok and not c.sent, repr(c.sent))

    c.sent = []
    ok = c.notify_event({"type": "something_new", "task": {"id": "t-2"}}) is False
    check("an unknown event kind is still dropped", ok and not c.sent, repr(c.sent))

    print("%d/%d checks passed" % (len(checks) - len(failures), len(checks)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
