#!/usr/bin/env python3
"""Run queued tea_agent tasks while the provider still has quota.

The idea: work that does not have to happen right now is put in a queue instead
of being run by hand. A scheduler picks the queue up when the provider can
afford it - for MiniMax M Plan that means the subscription has quota left - and
lets the agent work through it. When the agent needs a human, the task says so
and waits.

  tea_agent  -- the HTTP API (POST /api/tasks, GET /api/tasks, .../start)
  this file  -- the scheduler: poll the quota, start queued tasks, watch them

Usage:

  # one round: check the quota, start one queued task, wait for it
  python3 sched/task_runner.py --once

  # keep going, every 5 minutes, working on this workspace
  python3 sched/task_runner.py --workspace ws_xxx --interval 300

  # put a task in the queue
  python3 sched/task_runner.py --submit "把 README 的例子跑一遍"

Standard library only: this runs on the device, next to the agent, where a pip
install is not an option.
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_BASE = "http://127.0.0.1:8282"
TERMINAL = ("done", "failed", "cancelled", "waiting_confirmation")


class ApiError(Exception):
    """The server answered, but not with what we asked for."""

    def __init__(self, status, message, body=""):
        super().__init__("%s: %s" % (status, message))
        self.status = status
        self.message = message
        self.body = body


class Api:
    """The little bit of the tea_agent HTTP API the scheduler needs."""

    def __init__(self, base, timeout=60):
        self.base = base.rstrip("/")
        self.timeout = timeout

    def _request(self, method, path, body=None):
        url = self.base + path
        data = None
        headers = {}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            message = raw
            try:
                err = json.loads(raw)
                message = (err.get("error") or {}).get("message") or raw
            except ValueError:
                pass
            raise ApiError(exc.code, message or exc.reason, raw) from None
        except urllib.error.URLError as exc:
            raise ApiError(0, "cannot reach %s: %s" % (url, exc.reason)) from None
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except ValueError:
            raise ApiError(0, "the answer is not JSON: " + raw[:200]) from None

    def health(self):
        return self._request("GET", "/api/health")

    def usage(self, provider):
        """The provider's quota. Raises ApiError when it has no such endpoint."""
        return self._request("GET", "/api/providers/" + urllib.parse.quote(provider) + "/usage")

    def workspaces(self):
        return self._request("GET", "/api/workspaces").get("workspaces") or []

    def tasks(self, workspace_id, status=None):
        query = {"workspace_id": workspace_id}
        if status:
            query["status"] = status
        return self._request("GET", "/api/tasks?" + urllib.parse.urlencode(query))

    def task(self, workspace_id, task_id):
        query = urllib.parse.urlencode({"workspace_id": workspace_id})
        return self._request("GET", "/api/tasks/%s?%s" % (task_id, query))

    def create_task(self, workspace_id, prompt, title=None, provider=None):
        body = {"workspace_id": workspace_id, "prompt": prompt}
        if title:
            body["title"] = title
        if provider:
            body["provider"] = provider
        return self._request("POST", "/api/tasks", body)

    def start_task(self, workspace_id, task_id, provider=None):
        query = urllib.parse.urlencode({"workspace_id": workspace_id})
        body = {}
        if provider:
            # The work must run on the provider whose quota was checked.
            body["provider"] = provider
        return self._request("POST", "/api/tasks/%s/start?%s" % (task_id, query), body)

    def confirm_task(self, workspace_id, task_id, message):
        query = urllib.parse.urlencode({"workspace_id": workspace_id})
        return self._request("POST", "/api/tasks/%s/confirm?%s" % (task_id, query),
                             {"message": message})


def log(message, verbose=False):
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    print("%s  %s" % (stamp, message), flush=True)


def remaining_percent(quota, window):
    """How much of the chosen window is left, as a percentage (None = unknown)."""
    key = "weekly" if window == "weekly" else "window"
    block = quota.get(key) or {}
    value = block.get("remaining_percent")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def window_ends_in_ms(quota, window):
    key = "weekly" if window == "weekly" else "window"
    block = quota.get(key) or {}
    value = block.get("remains_ms")
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def pick_workspace(api, wanted):
    workspaces = api.workspaces()
    if not workspaces:
        raise SystemExit("the server has no workspaces; add one first")
    if wanted:
        for ws in workspaces:
            if ws.get("id") == wanted:
                return wanted
        raise SystemExit("no workspace with id %r (known: %s)"
                         % (wanted, ", ".join(w.get("id", "?") for w in workspaces)))
    return workspaces[0].get("id")


def wait_for_task(api, workspace_id, task_id, poll, timeout, deadline):
    """Watch a task until it is no longer running. Returns the final record."""
    last = None
    while time.time() < deadline:
        try:
            task = api.task(workspace_id, task_id)
        except ApiError as exc:
            log("  task %s: %s" % (task_id, exc))
            return None
        if task != last:
            note = task.get("note") or ""
            log("  task %s: %s %d%%%s%s" % (
                task_id, task.get("status"), task.get("progress", 0),
                (" - " + note) if note else "",
                "  [needs confirmation]" if task.get("needs_confirmation") else ""))
            last = task
        status = task.get("status")
        if status in TERMINAL:
            return task
        time.sleep(poll)
    log("  task %s: still running after %d s; giving up on watching it (it keeps running)"
        % (task_id, timeout))
    return None


def run_round(api, args, workspace_id):
    """One round: check the quota, then start what can be started."""
    # ---- the gate: the provider's quota --------------------------------
    try:
        quota = api.usage(args.provider)
    except ApiError as exc:
        if exc.status == 400:
            log("provider %r does not report a quota (%s)" % (args.provider, exc.message))
        else:
            log("cannot read the quota of %r: %s" % (args.provider, exc))
        if not args.allow_without_quota:
            log("not starting anything (use --allow-without-quota to work anyway)")
            return 0
        log("working anyway (--allow-without-quota)")
        quota = {}

    left = remaining_percent(quota, args.window)
    if left is None:
        if not args.allow_without_quota:
            log("the provider did not say how much is left; not starting anything "
                "(use --allow-without-quota to start anyway)")
            return 0
        log("the provider did not say how much is left; starting anyway (--allow-without-quota)")
    else:
        if left < args.min_remaining:
            resets = window_ends_in_ms(quota, args.window)
            log("quota of %r: %s%% left (need %d%%) - not starting anything%s"
                % (args.provider, left, args.min_remaining,
                   (", the window resets in %.0f min" % (resets / 60000.0)) if resets else ""))
            return 0
        log("quota of %r: %s%% left - going to work" % (args.provider, left))

    # ---- the queue ------------------------------------------------------
    try:
        listing = api.tasks(workspace_id, status="queued")
    except ApiError as exc:
        log("cannot read the task list: %s" % exc)
        return 0
    queued = listing.get("tasks") or []
    if not queued:
        log("nothing queued")
    # A task that is still marked "running" is either being worked on right now
    # or was left behind by a crash. The second case used to be invisible: the
    # scheduler only looks at queued tasks, so such a task sat in the queue
    # forever. The agent now recovers those at startup (they come back as
    # failed, with a reason), and this line makes it visible either way.
    try:
        counts = (api.tasks(workspace_id) or {}).get("counts") or {}
    except ApiError:
        counts = {}
    if counts.get("running"):
        log("%s task(s) are marked running (worked on right now, or left behind by a "
            "crash - the agent marks those failed when it starts)" % counts["running"])

    started = 0
    for task in queued:
        if started >= args.max_per_round:
            break
        if task.get("provider") and task.get("provider") != args.provider:
            log("  task %s wants provider %r, not %r - leaving it alone"
                % (task.get("id"), task.get("provider"), args.provider))
            continue
        task_id = task.get("id")
        try:
            api.start_task(workspace_id, task_id, provider=args.provider)
        except ApiError as exc:
            log("  task %s could not be started: %s" % (task_id, exc))
            continue
        started += 1
        log("  started task %s (%s)" % (task_id, task.get("title") or task.get("prompt", "")[:40]))
        final = wait_for_task(api, workspace_id, task_id, args.poll, args.timeout,
                              time.time() + args.timeout)
        if final and final.get("status") == "waiting_confirmation":
            log("  task %s is waiting for the user: %s"
                % (task_id, final.get("question") or "(no question given)"))
            log("  answer it with: curl -X POST '%s/api/tasks/%s/confirm?workspace_id=%s' "
                "-H 'Content-Type: application/json' -d '{\"message\":\"...\"}'"
                % (api.base, task_id, workspace_id))
            break  # one thing at a time; the human is in the loop now
    return started


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Run queued tea_agent tasks while the provider has quota.")
    parser.add_argument("--base", default=os.environ.get("TEA_AGENT_URL", DEFAULT_BASE),
                        help="the agent's HTTP API (default: %s)" % DEFAULT_BASE)
    parser.add_argument("--workspace", default=os.environ.get("TEA_AGENT_WORKSPACE", ""),
                        help="the workspace whose queue is worked on (default: the first one)")
    parser.add_argument("--provider", default=os.environ.get("TEA_AGENT_PROVIDER", "minimax"),
                        help="the provider whose quota gates the work (default: minimax)")
    parser.add_argument("--window", default="window", choices=["window", "weekly"],
                        help="which quota window to watch (default: the short one)")
    parser.add_argument("--min-remaining", type=float, default=5.0,
                        help="only work while at least this much of the window is left (%%)")
    parser.add_argument("--allow-without-quota", action="store_true",
                        help="work even when the provider does not report a quota")
    parser.add_argument("--interval", type=float, default=60.0,
                        help="seconds between rounds (default: 60)")
    parser.add_argument("--poll", type=float, default=5.0,
                        help="seconds between task status checks (default: 5)")
    parser.add_argument("--timeout", type=float, default=3600.0,
                        help="how long to watch a started task (default: 3600)")
    parser.add_argument("--max-per-round", type=int, default=1,
                        help="how many queued tasks to start in one round (default: 1)")
    parser.add_argument("--once", action="store_true", help="do one round and exit")
    parser.add_argument("--submit", metavar="PROMPT",
                        help="put a task in the queue and exit (no quota needed)")
    parser.add_argument("--title", default="", help="title for --submit")
    args = parser.parse_args(argv)

    api = Api(args.base)

    if args.submit:
        workspace_id = pick_workspace(api, args.workspace)
        task = api.create_task(workspace_id, args.submit, args.title or None,
                               provider=args.provider)
        log("queued task %s (%s)" % (task.get("id"), task.get("title") or ""))
        return 0

    try:
        api.health()
    except ApiError as exc:
        print("cannot reach the agent at %s: %s" % (args.base, exc), file=sys.stderr)
        return 2

    try:
        workspace_id = pick_workspace(api, args.workspace)
    except SystemExit as exc:
        print(str(exc), file=sys.stderr)
        return 2

    log("scheduler up: %s, workspace %s, provider %s" % (args.base, workspace_id, args.provider))

    while True:
        try:
            run_round(api, args, workspace_id)
        except ApiError as exc:
            log("round failed: %s" % exc)
        except KeyboardInterrupt:
            log("stopped")
            return 0
        if args.once:
            return 0
        try:
            time.sleep(args.interval)
        except KeyboardInterrupt:
            log("stopped")
            return 0


if __name__ == "__main__":
    sys.exit(main())
