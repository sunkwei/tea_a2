#!/usr/bin/env python3
"""The watchdog: something outside the agent that watches the work.

The agent is good at doing one thing well and it is bad at noticing that it
stopped doing it. A turn that hangs on a slow endpoint, a queue whose task
worker died with the process, a provider whose quota ran out - none of these
say anything by themselves. Somebody has to look, and "somebody" cannot be
the agent: an agent that is stuck cannot notice that it is stuck.

So the watchdog is a separate process. It looks at four things:

  * the tasks that are running in the queue: a task whose record has not
    moved for a long time is stuck (a slow turn, a dead worker, a provider
    that stopped answering);
  * the agent: is it still there at all;
  * the quota of the provider the queue works on;
  * the devices of the fleet, when there is a fleet.

What it does about it depends on how it was started:

  * by default it only **says so** - it puts an alert into the queue's event
    stream, which is the road to the person who queued the work (the chat
    front ends listen to that stream), and it writes the finding down;
  * with --auto it may also **act**: mark a stuck task failed, or fail it
    and queue it again.

The default is "say so" on purpose. A watchdog that kills work by itself
kills work that was about to finish, and that is a much worse failure than
a warning nobody read.

Usage:
    python3 sched/watchdog.py                       # watch, and only say so
    python3 sched/watchdog.py --once                # one pass and out
    python3 sched/watchdog.py --auto --action fail  # and act on stuck tasks

Only the standard library: it has to run on a device where nothing is
installed (the same rule the queue and the fleet follow).
"""

import argparse
import calendar
import json
import os
import sys
import time
import urllib.error
import urllib.request

DEFAULT_TASKS = os.environ.get("TEA_TASK_URL", "http://127.0.0.1:8283")
DEFAULT_AGENT = os.environ.get("TEA_AGENT_URL", "http://127.0.0.1:8282")
DEFAULT_FLEET = os.environ.get("TEA_FLEET_URL", "")
DEFAULT_DATA_DIR = os.environ.get(
    "TEA_WATCHDOG_DATA", os.path.join("~", ".tea_agent", "watchdog"))

# The actions the watchdog may take on a stuck task.
ESCALATE = "escalate"
FAIL = "fail"
RETRY = "retry"
ACTIONS = (ESCALATE, FAIL, RETRY)


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def parse_iso(text):
    """The epoch seconds of a stamp the queue wrote ('' -> None)."""
    if not text:
        return None
    try:
        return calendar.timegm(time.strptime(text, "%Y-%m-%dT%H:%M:%SZ"))
    except ValueError:
        return None


def log(message):
    print("%s  %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), message), flush=True)


def http_json(url, method="GET", body=None, timeout=10, headers=None):
    """One JSON request. Returns (status, parsed_body).

    Never raises: "the thing I watch is not there" is a result, not a
    crash - that is the whole job of this program.
    """
    data = None
    head = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
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
        return status, {"error": raw[:500]}


def unwrap(body):
    """The queue answers in one shape: {code, message, request_id, data}."""
    if isinstance(body, dict) and "data" in body and "code" in body:
        if body.get("code"):
            return {"error": body.get("message") or "the queue refused"}
        return body.get("data") or {}
    return body


class Watchdog(object):
    """One pass over everything it watches."""

    def __init__(self, args):
        self.args = args
        self.tasks_url = args.tasks.rstrip("/")
        self.agent_url = args.agent.rstrip("/")
        self.fleet_url = args.fleet.rstrip("/") if args.fleet else ""
        self.data_dir = os.path.abspath(os.path.expanduser(args.data_dir))
        self.audit_path = args.audit or os.path.join(self.data_dir, "audit.jsonl")
        self.state_path = args.state or os.path.join(self.data_dir, "state.json")
        self.state = self._read_state()
        self.findings = []

    # ---- the state it remembers between passes ----------------------------
    def _read_state(self):
        try:
            with open(self.state_path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        data.setdefault("tasks", {})
        data.setdefault("seen", {})
        return data

    def _write_state(self):
        try:
            os.makedirs(self.data_dir, exist_ok=True)
            with open(self.state_path, "w", encoding="utf-8") as f:
                json.dump(self.state, f, ensure_ascii=False, indent=2)
        except OSError:
            pass

    # ---- the audit log (only ever appended) ------------------------------
    def audit(self, check, target="", action="", reason="", detail=None):
        entry = {"at": now_iso(), "check": check, "target": target or "",
                 "action": action or "", "reason": reason or "",
                 "auto": bool(self.args.auto)}
        if detail:
            entry["detail"] = detail
        try:
            os.makedirs(self.data_dir, exist_ok=True)
            with open(self.audit_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass
        return entry

    # ---- what it does about a finding -------------------------------------
    def escalate(self, check, reason, task=None, level="warn"):
        """Say so: put the alert into the queue's event stream.

        The stream is the road to the person who queued the work: the chat
        front ends (WeChat, Telegram) listen to it and push what they hear
        to the user. An alert without a task is about the whole queue and
        goes to everybody; an alert with a task goes to that task's project.
        """
        body = {"type": "watchdog_alert", "check": check, "level": level,
                "reason": reason, "action": ESCALATE, "source": "watchdog"}
        if task:
            body["task_id"] = task.get("id")
            body["workspace_id"] = task.get("workspace_id")
        status, answer = http_json(self.tasks_url + "/api/events", "POST", body,
                                   timeout=self.args.timeout)
        if status == 0:
            log("cannot tell the queue about it: %s" % (answer or {}).get("error"))
            return False
        if status >= 400:
            log("the queue refused the alert: HTTP %s %s" % (status, answer))
            return False
        self.audit(check, (task or {}).get("id", ""), ESCALATE, reason)
        log("%s: %s" % (check, reason))
        return True

    def fail_task(self, task, reason):
        """Mark a stuck task failed (only with --auto)."""
        status, answer = http_json(
            self.tasks_url + "/api/tasks/%s/fail" % task.get("id"), "POST",
            {"reason": reason}, timeout=self.args.timeout)
        if status == 0 or status >= 400:
            log("cannot fail task %s: %s" % (task.get("id"), answer))
            self.audit("fail_task", task.get("id"), FAIL, reason,
                       {"http_status": status, "answer": answer})
            return False
        self.audit("fail_task", task.get("id"), FAIL, reason)
        log("task %s marked failed: %s" % (task.get("id"), reason))
        return True

    def retry_task(self, task, reason):
        """Fail the stuck task and queue it again (only with --auto).

        The queue's retry only accepts a finished task, so a stuck one is
        failed first and then retried: the attempt counter goes up and the
        work goes back into the line.
        """
        if not self.fail_task(task, reason):
            return False
        status, answer = http_json(
            self.tasks_url + "/api/tasks/%s/retry" % task.get("id"), "POST",
            {}, timeout=self.args.timeout)
        if status == 0 or status >= 400:
            log("cannot retry task %s: %s" % (task.get("id"), answer))
            self.audit("retry_task", task.get("id"), RETRY, reason,
                       {"http_status": status, "answer": answer})
            return False
        self.audit("retry_task", task.get("id"), RETRY, reason)
        log("task %s queued again: %s" % (task.get("id"), reason))
        return True

    def act_on(self, task, reason):
        """Do what --action says about a stuck task."""
        action = self.args.action
        if action == FAIL:
            return self.fail_task(task, reason)
        if action == RETRY:
            return self.retry_task(task, reason)
        return self.escalate("stalled_task", reason, task)

    # ---- the checks --------------------------------------------------------
    def running_tasks(self):
        """The tasks the queue says are running right now."""
        status, body = http_json(self.tasks_url + "/api/tasks?status=running",
                                 timeout=self.args.timeout)
        if status == 0:
            return None, body
        data = unwrap(body)
        if not isinstance(data, dict):
            return [], {"error": "unexpected answer"}
        return data.get("tasks") or [], None

    def check_stalled(self):
        """A task whose record has not moved for a long time is stuck.

        Two thresholds, because one is not enough:

          * the record must be quiet for --stall-after minutes (a task that
            reports progress every minute is working, however long it takes);
          * the progress must not have moved between two passes of the
            watchdog (one quiet moment is a slow tool call, two are a stall).

        The second one needs the state of the previous pass, which is why
        the watchdog remembers what it saw.
        """
        if not self.args.stall_after:
            return []
        tasks, error = self.running_tasks()
        if tasks is None:
            # The queue itself is not reachable: that is the agent_down check's
            # business, not a stalled task.
            return []
        out = []
        now = time.time()
        for task in tasks:
            task_id = task.get("id") or ""
            if not task_id:
                continue
            stamp = parse_iso(task.get("updated_at") or task.get("started_at") or "")
            age = (now - stamp) if stamp is not None else 0
            if age < self.args.stall_after * 60:
                continue
            progress = int(task.get("progress") or 0)
            seen = self.state["tasks"].get(task_id) or {}
            before = seen.get("progress")
            if before is None:
                # First time we see it quiet: wait for the next pass before
                # calling it stuck. One slow moment is not a stall.
                self.state["tasks"][task_id] = {"progress": progress, "seen_at": now_iso()}
                continue
            if before != progress:
                # It moved since the last pass: it is working, just slow.
                self.state["tasks"][task_id] = {"progress": progress, "seen_at": now_iso()}
                continue
            if seen.get("alerted"):
                # Already said so; do not say it again every pass.
                continue
            # The age is written the way it is measured: a threshold of three
            # seconds must not be reported as "0 minutes".
            waited = ("%d s" % int(age)) if age < 90 else ("%.1f min" % (age / 60.0))
            reason = ("no progress for %s (progress %d%%, last note: %s)"
                      % (waited, progress, (task.get("note") or "-")[:80]))
            self.state["tasks"][task_id] = {"progress": progress, "seen_at": now_iso(),
                                            "alerted": True}
            self.act_on(task, reason)
            out.append(task)
        # Forget the tasks that are not running any more.
        alive = set(t.get("id") for t in tasks)
        for task_id in list(self.state["tasks"].keys()):
            if task_id not in alive:
                del self.state["tasks"][task_id]
        return out

    def check_agent(self):
        """Is the agent still there?"""
        status, body = http_json(self.agent_url + "/api/health", timeout=self.args.timeout)
        if status == 0 or status >= 400:
            reason = "the agent does not answer: %s" % ((body or {}).get("error") or
                                                        ("HTTP %s" % status))
            if self.state["seen"].get("agent_down"):
                return False
            self.state["seen"]["agent_down"] = True
            self.escalate("agent_down", reason, None, level="error")
            return True
        self.state["seen"]["agent_down"] = False
        return False

    def check_quota(self):
        """Is there quota left for the work the queue wants to do?"""
        if not self.args.min_quota:
            return False
        status, body = http_json(self.tasks_url + "/api/health", timeout=self.args.timeout)
        if status == 0 or status >= 400:
            return False
        data = unwrap(body)
        if not isinstance(data, dict):
            return False
        left = data.get("quota_percent")
        if left is None:
            return False
        if float(left) >= float(self.args.min_quota):
            self.state["seen"]["quota_low"] = False
            return False
        if self.state["seen"].get("quota_low"):
            return False
        self.state["seen"]["quota_low"] = True
        self.escalate("quota_low", "the quota of %s is down to %s%% (the queue will not "
                                   "start new work below %s%%)"
                      % (data.get("provider") or "the provider", left, self.args.min_quota),
                      None, level="warn")
        return True

    def check_devices(self):
        """The devices of the fleet (when there is a fleet to look at).

        A device that is down is written down, but not shouted about: the
        fleet's daily report already says that, and an alert per dead device
        every pass would drown the alerts that matter.
        """
        if not self.fleet_url:
            return False
        status, body = http_json(self.fleet_url + "/api/fleet/status",
                                 timeout=self.args.timeout)
        if status == 0 or status >= 400:
            return False
        data = body if isinstance(body, dict) else {}
        devices = data.get("devices") or []
        down = [d for d in devices if not (d.get("ok") if isinstance(d, dict) else False)]
        if not down:
            self.state["seen"]["device_offline"] = False
            return False
        names = ",".join(str(d.get("name") or "?") for d in down[:10])
        if self.state["seen"].get("device_offline") == names:
            return False
        self.state["seen"]["device_offline"] = names
        self.audit("device_offline", names, "record", "%d device(s) do not answer" % len(down),
                   {"devices": [d.get("name") for d in down[:20]]})
        log("device_offline: %s" % names)
        return True

    # ---- one pass ----------------------------------------------------------
    def run_once(self):
        self.check_agent()
        self.check_quota()
        self.check_stalled()
        self.check_devices()
        self._write_state()

    def run_forever(self):
        while True:
            try:
                self.run_once()
            except Exception as exc:  # noqa: BLE001 - the watchdog must not die
                log("pass failed: %s: %s" % (type(exc).__name__, exc))
            if self.args.once:
                return 0
            time.sleep(max(5.0, self.args.interval))


def build_parser():
    parser = argparse.ArgumentParser(
        description="Watch the queue, the agent, the quota and the devices.")
    parser.add_argument("--tasks", default=DEFAULT_TASKS, help="the task server's HTTP API")
    parser.add_argument("--agent", default=DEFAULT_AGENT, help="the agent's HTTP API")
    parser.add_argument("--fleet", default=DEFAULT_FLEET,
                        help="the fleet service's HTTP API (empty = do not look)")
    parser.add_argument("--interval", type=float, default=30.0,
                        help="seconds between two passes")
    parser.add_argument("--stall-after", type=float, default=15.0,
                        help="minutes without progress before a task counts as stuck "
                             "(0 = do not watch for stuck tasks)")
    parser.add_argument("--min-quota", type=float, default=0.0,
                        help="the quota percentage below which the queue is warned about "
                             "(0 = do not watch the quota)")
    parser.add_argument("--action", default=ESCALATE, choices=list(ACTIONS),
                        help="what to do about a stuck task: escalate (say so), "
                             "fail it, or fail it and queue it again")
    parser.add_argument("--auto", action="store_true",
                        help="allow the watchdog to act (fail / retry). Without it the "
                             "watchdog only says so.")
    parser.add_argument("--once", action="store_true", help="one pass and out")
    parser.add_argument("--timeout", type=float, default=10.0,
                        help="seconds for one HTTP request")
    parser.add_argument("--data-dir", default=DEFAULT_DATA_DIR,
                        help="where the audit log and the state live")
    parser.add_argument("--audit", default="", help="the audit log (default: under --data-dir)")
    parser.add_argument("--state", default="", help="the state file (default: under --data-dir)")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    watchdog = Watchdog(args)
    if args.action != ESCALATE and not args.auto:
        # Asking for an action without --auto is a contradiction: say it once
        # and behave the safe way.
        log("--action %s without --auto: the watchdog will only say so" % args.action)
        args.action = ESCALATE
    log("watching: tasks=%s agent=%s%s" % (args.tasks, args.agent,
                                          (" fleet=" + args.fleet) if args.fleet else ""))
    return watchdog.run_forever()


if __name__ == "__main__":
    sys.exit(main())
