#!/usr/bin/env python3
"""The task queue: enqueue, run, watch - on top of the agent's HTTP API.

The agent (tea_agent) is the **workspace channel**: it runs the turns, holds the
sessions, touches the files. This server is the **queue**: work that does not
have to happen right now is put in a list and worked through when the provider
can afford it - for MiniMax M Plan that means the subscription has quota left.

  tea_agent -- the channel: POST /api/chat (SSE), GET /api/providers/{n}/usage,
               POST /api/chat/stop, GET /api/workspaces
  this file  -- the queue:   the records, the scheduler, the task API

The agent knows nothing about the queue: it is told "this turn works on task X"
and the agent reports on it (toolkit_task); the report is read back from the
turn's event stream. So the agent runs perfectly well without this server, and
this server is optional.

Usage:

  # serve the task API (default: agent on 8282, this server on 8283)
  python3 sched/task_server.py

  # put a task in the queue (no server needed: it writes straight into the store)
  python3 sched/task_server.py --submit "把 README 的例子跑一遍"

  # what is in the queue
  curl http://127.0.0.1:8283/api/tasks

Standard library only: this runs on the device, next to the agent, where a pip
install is not an option.
"""

import argparse
import base64
import calendar
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
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

DEFAULT_AGENT = os.environ.get("TEA_AGENT_URL", "http://127.0.0.1:8282")
DEFAULT_PORT = int(os.environ.get("TEA_TASK_PORT", "8283"))
DEFAULT_WORKSPACE_PATH = os.path.expanduser(os.environ.get(
    "TEA_TASK_WORKSPACE", os.path.join("~", ".tea_agent", "task-workspace")))
# The ledger lives in the queue's own workspace, next to the work: one directory
# holds everything about the queue.
DEFAULT_DATA_DIR = os.path.expanduser(os.environ.get(
    "TEA_TASK_DATA", os.path.join(DEFAULT_WORKSPACE_PATH, "tasks")))
# The provider whose quota gates the work. MiniMax M Plan reports one; a
# provider without a quota endpoint is worked on anyway (with a warning).
DEFAULT_PROVIDER = os.environ.get("TEA_TASK_PROVIDER", "minimax")

QUEUED = "queued"
RUNNING = "running"
WAITING = "waiting_confirmation"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"
TERMINAL = (DONE, FAILED, CANCELLED)


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def parse_iso(text):
    """The epoch seconds of a stamp this module wrote ('' -> None)."""
    if not text:
        return None
    try:
        return calendar.timegm(time.strptime(text, "%Y-%m-%dT%H:%M:%SZ"))
    except ValueError:
        return None


def log(message):
    print("%s  %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), message), flush=True)


class AgentError(Exception):
    """The agent answered, but not with what we asked for."""

    def __init__(self, status, message):
        super().__init__("%s: %s" % (status, message))
        self.status = status
        self.message = message


class Agent:
    """The workspace channel: the part of the agent's API the queue needs."""

    def __init__(self, base=DEFAULT_AGENT, timeout=60):
        self.base = base.rstrip("/")
        self.timeout = timeout

    def _request(self, method, path, body=None, timeout=None):
        url = self.base + path
        data = None
        headers = {}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            message = raw
            try:
                err = json.loads(raw)
                message = (err.get("error") or {}).get("message") or raw
            except ValueError:
                pass
            raise AgentError(exc.code, message or str(exc.reason)) from None
        except urllib.error.URLError as exc:
            raise AgentError(0, "cannot reach %s: %s" % (url, exc.reason)) from None
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except ValueError:
            raise AgentError(0, "the answer is not JSON: " + raw[:200]) from None

    def health(self):
        return self._request("GET", "/api/health")

    def workspaces(self):
        return self._request("GET", "/api/workspaces").get("workspaces") or []

    def session(self, session_id):
        """The conversation behind a task (its messages, artifacts, todos)."""
        if not session_id:
            return {}
        try:
            return self._request("GET", "/api/sessions/%s" % urllib.parse.quote(session_id))
        except AgentError:
            return {}

    def download(self, workspace_id, path):
        """One file the task produced. Returns the bytes."""
        if not workspace_id or not path:
            return b""
        url = ("%s/api/workspaces/%s/download?path=%s"
               % (self.base, urllib.parse.quote(workspace_id),
                  urllib.parse.quote(path)))
        try:
            with urllib.request.urlopen(url, timeout=60) as resp:
                return resp.read()
        except (urllib.error.URLError, OSError):
            return b""

    def usage(self, provider):
        """How much of the provider's subscription is left.

        Raises AgentError(400) when the provider does not report a quota at all.
        """
        return self._request("GET", "/api/providers/" + urllib.parse.quote(provider) + "/usage")

    def stop(self, session_id):
        return self._request("POST", "/api/chat/stop", {"session_id": session_id})

    def register_workspace(self, path, name=""):
        """Add a workspace to the agent's index (it is the agent's registry)."""
        return self._request("POST", "/api/workspaces", {"path": path, "name": name})

    def chat(self, workspace_id, message, provider=None, session_id=None,
             task_id=None, task_context=None, allow_paths=None):
        """Run one turn and yield the agent's events as they happen.

        This is the whole contract between the queue and the channel: the
        agent announces the session, reports on the task (toolkit_task) and
        finishes (done / error / stopped). The queue reads the progress out of
        the stream - it never has to ask the agent where it is.
        """
        body = {"workspace_id": workspace_id, "message": message}
        if provider:
            body["provider"] = provider
        if session_id:
            body["session_id"] = session_id
        if task_id:
            body["task_id"] = task_id
        if task_context:
            body["task_context"] = task_context
        if allow_paths:
            body["allow_paths"] = list(allow_paths)
        req = urllib.request.Request(
            self.base + "/api/chat", data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            resp = urllib.request.urlopen(req, timeout=None)
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            message = raw
            try:
                message = (json.loads(raw).get("error") or {}).get("message") or raw
            except ValueError:
                pass
            raise AgentError(exc.code, message) from None
        except urllib.error.URLError as exc:
            raise AgentError(0, "cannot reach %s: %s" % (self.base, exc.reason)) from None
        with resp:
            for frame in self._sse(resp):
                yield frame

    @staticmethod
    def _sse(stream):
        """Parse a text/event-stream into dictionaries."""
        for raw_line in stream:
            line = raw_line.decode("utf-8", "replace").strip()
            if not line or not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                yield json.loads(payload)
            except ValueError:
                continue


class Store:
    """The queue's own records: one file per task, one directory per workspace.

    The records live here and not in the workspace: the workspace belongs to the
    agent, this is the queue's own bookkeeping.
    """

    def __init__(self, root):
        self.root = os.path.abspath(os.path.expanduser(root))
        self.lock = threading.RLock()
        os.makedirs(self.root, exist_ok=True)

    def _path(self, task_id):
        return os.path.join(self.root, task_id + ".json")

    def _events_path(self, task_id):
        return os.path.join(self.root, task_id + ".events.jsonl")

    def record_path(self, workspace_id, task_id):
        """Where a task's record lives - the page shows it."""
        return self._path(task_id)

    def events_path(self, task_id):
        return self._events_path(task_id)

    # ---- the event log -------------------------------------------------
    def record_event(self, task_id, event_type, payload):
        """Append one event to the task's log. Returns the event.

        The events used to be a broadcast and nothing else: whoever was
        listening saw them, and after a restart there was no way to find out
        what had happened to a task. Now every state change is written down,
        with a number the client can ask "everything after N".
        """
        with self.lock:
            events = self.events(task_id)
            number = (events[-1]["event_id"] + 1) if events else 1
            event = {"event_id": number, "task_id": task_id, "type": event_type,
                     "payload": payload if isinstance(payload, dict) else {},
                     "created_at": now_iso()}
            try:
                with open(self._events_path(task_id), "a", encoding="utf-8") as f:
                    f.write(json.dumps(event, ensure_ascii=False) + "\n")
            except OSError:
                pass
            return event

    def events(self, task_id, since_event_id=0):
        """The task's events, newest last. `since_event_id` skips the old ones."""
        out = []
        try:
            with open(self._events_path(task_id), encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if event.get("event_id", 0) > since_event_id:
                        out.append(event)
        except OSError:
            pass
        return out

    # ---- the audit log -------------------------------------------------
    def audit(self, action, task_id="", detail=None, actor=""):
        """One line in the audit log: who did what to which task."""
        entry = {"at": now_iso(), "action": action, "task_id": task_id or "",
                 "actor": actor or "", "detail": detail if isinstance(detail, dict) else {}}
        try:
            with open(os.path.join(self.root, "audit.log"), "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass
        return entry

    def audit_log(self, task_id="", limit=200):
        out = []
        try:
            with open(os.path.join(self.root, "audit.log"), encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
                    if task_id and entry.get("task_id") != task_id:
                        continue
                    out.append(entry)
        except OSError:
            pass
        return out[-limit:]

    # ---- the projects and their keys -------------------------------------
    def _projects_path(self):
        return os.path.join(self.root, "projects.json")

    def project_key(self, workspace_id):
        """The key of a project (a workspace). It is minted on first use.

        The key is the credential: a caller that presents it may work on that
        project - its tasks, its events, its files. A key of another project
        gets nothing (403). Without a key the caller is the local operator,
        which is what the web page on the same machine is.
        """
        if not workspace_id:
            return ""
        with self.lock:
            projects = self._read_projects()
            entry = projects.get(workspace_id)
            if not entry:
                entry = {"key": base64.b32encode(os.urandom(20)).decode().strip("=")}
                projects[workspace_id] = entry
                self._write_projects(projects)
            return entry.get("key") or ""

    def project_of_key(self, key):
        """The project a key belongs to ("" when the key is unknown)."""
        if not key:
            return ""
        with self.lock:
            for workspace_id, entry in self._read_projects().items():
                if entry.get("key") == key:
                    return workspace_id
        return ""

    def projects(self):
        with self.lock:
            return dict(self._read_projects())

    def _read_projects(self):
        try:
            with open(self._projects_path(), encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                return loaded
        except (OSError, ValueError):
            pass
        return {}

    def _write_projects(self, projects):
        try:
            fd, tmp = tempfile.mkstemp(dir=self.root, prefix=".projects-")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(projects, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._projects_path())
        except OSError:
            pass

    # ---- idempotency ---------------------------------------------------
    def find_by_idempotency(self, key, workspace_id=None):
        """The task a repeated submission with the same key must return."""
        if not key:
            return None
        for task in self.list(workspace_id=workspace_id):
            if task.get("idempotency_key") == key:
                return task
        return None

    def create(self, task):
        with self.lock:
            self._write(task)
        return task

    def load(self, workspace_id, task_id):
        # One ledger, one directory: the task ids are unique, so the workspace a
        # task works in is a field of the record, not a directory.
        if not task_id or not isinstance(task_id, str):
            return None
        with self.lock:
            try:
                with open(self._path(task_id), encoding="utf-8") as f:
                    loaded = json.load(f)
            except (OSError, ValueError):
                return None
        if not isinstance(loaded, dict) or not loaded.get("id"):
            # projects.json and friends live in the same directory; they are not
            # tasks.
            return None
        return loaded

    def save(self, task):
        with self.lock:
            self._write(task)

    def _write(self, task):
        os.makedirs(self.root, exist_ok=True)
        path = self._path(task.get("id"))
        # Atomic: a reader (the API, the scheduler) never sees half a record.
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".task-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(task, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def update(self, workspace_id, task_id, fn):
        """Read-modify-write one task under the lock."""
        with self.lock:
            task = self.load(workspace_id, task_id)
            if task is None:
                return None
            fn(task)
            task["updated_at"] = now_iso()
            self._write(task)
            return task

    def list(self, workspace_id=None, status=None, created_from="", created_to="",
             tag="", business_id="", page=0, page_size=0):
        """The tasks, newest first, with the filters the history view needs.

        The filters beyond status are the ones a business side asks for: a time
        range, a tag, a business id - the tag and the business id live in the
        task's meta, where the submitter put them.
        """
        out = []
        with self.lock:
            try:
                names = os.listdir(self.root)
            except OSError:
                names = []
            for name in names:
                if not name.endswith(".json") or name.endswith(".events.jsonl"):
                    continue
                task = self.load(workspace_id, name[:-5])
                if task is None:
                    continue
                if not isinstance(task, dict) or not task.get("id"):
                    # Not a task record (projects.json lives here too).
                    continue
                if workspace_id and task.get("workspace_id") != workspace_id:
                    continue
                if status and task.get("status") != status:
                    continue
                created = task.get("created_at") or ""
                if created_from and created < created_from:
                    continue
                if created_to and created > created_to:
                    continue
                meta = task.get("meta") or {}
                if tag and tag not in (meta.get("tags") or []):
                    continue
                if business_id and str(meta.get("business_id") or "") != str(business_id):
                    continue
                out.append(task)
        # Newest first: the history view is a list of what just happened.
        out.sort(key=lambda t: (t.get("created_at") or "", t.get("id") or ""), reverse=True)
        total = len(out)
        if page_size and page_size > 0:
            page = max(1, int(page or 1))
            start = (page - 1) * page_size
            out = out[start:start + page_size]
        return out

    def delete(self, workspace_id, task_id):
        with self.lock:
            try:
                os.unlink(self._path(task_id))
                return True
            except OSError:
                return False

    def counts(self, workspace_id=None):
        counts = {QUEUED: 0, RUNNING: 0, WAITING: 0, DONE: 0, FAILED: 0, CANCELLED: 0}
        for task in self.list(workspace_id):
            status = task.get("status")
            if status in counts:
                counts[status] += 1
        return counts


class Events:
    """The queue's own event stream: who wants to know, gets told.

    Every event is also written to the task's log: a broadcast alone leaves
    nothing behind when the process restarts, and a task's history has to
    survive that.
    """

    def __init__(self, store=None):
        self.lock = threading.Lock()
        self.subscribers = []
        self.store = store

    def subscribe(self):
        queue = []
        state = {"alive": True}

        def listener(event):
            with self.lock:
                queue.append(event)

        with self.lock:
            self.subscribers.append(listener)
        return queue, state, listener

    def unsubscribe(self, listener):
        with self.lock:
            if listener in self.subscribers:
                self.subscribers.remove(listener)

    def publish(self, event):
        # Write it down first: the log is the history, the broadcast is only
        # for the clients that happen to be listening.
        if self.store is not None:
            task = event.get("task") or {}
            task_id = task.get("id") or ""
            if task_id:
                payload = {k: v for k, v in event.items() if k != "task"}
                self.store.record_event(task_id, event.get("type") or "event", payload)
        with self.lock:
            listeners = list(self.subscribers)
        for listener in listeners:
            try:
                listener(event)
            except Exception:
                pass


class Engine:
    """The scheduler: what to work on, and when."""

    def __init__(self, store, agent, events, args):
        self.store = store
        self.agent = agent
        self.events = events
        self.args = args
        self.running = {}     # task id -> {"session_id":..., "thread":...}
        self._quota = (None, None, 0.0)   # (percent, window, read_at)

    # ---- the quota gate ---------------------------------------------------
    def quota_left(self, provider, fresh=False):
        """(percent_left, window_name) - (None, None) when there is no quota.

        The queue polls every second now (so a task waits at most that long),
        and the quota endpoint is a real HTTP call to the provider: asking it
        every second would be rude and slow. The answer is therefore cached
        for --quota-interval seconds.
        """
        now = time.time()
        if not fresh and self._quota[2] and now - self._quota[2] < self.args.quota_interval:
            return self._quota[0], self._quota[1]
        try:
            quota = self.agent.usage(provider)
        except AgentError as exc:
            if exc.status == 400:
                log("provider %r does not report a quota (%s)" % (provider, exc.message))
            else:
                log("cannot read the quota of %r: %s" % (provider, exc))
            self._quota = (None, None, now)
            return None, None
        window = self.args.window
        block = quota.get(window) or quota.get("weekly") or {}
        value = block.get("remaining_percent")
        if value is None:
            self._quota = (None, None, now)
            return None, None
        try:
            percent = float(value)
        except (TypeError, ValueError):
            self._quota = (None, None, now)
            return None, None
        self._quota = (percent, window, now)
        return percent, window

    # ---- how many tasks may run at the same time --------------------------
    def running_tasks(self):
        """The tasks that are running right now - ours and everybody else's.

        Counted from the *records*, not from this process's threads: a task
        that is still running when the next round comes up must keep its slot.
        A record that says "running" but was not touched for a while is a
        leftover from a crashed process - it does not hold a slot for ever.
        """
        mine = set(self.running)
        out = []
        for task in self.store.list(status=RUNNING):
            if task.get("id") not in mine:
                stamp = parse_iso(task.get("updated_at") or task.get("started_at") or "")
                if stamp is not None and time.time() - stamp > self.args.stale_after:
                    continue
            out.append(task)
        return out

    def running_count(self, workspace_id=None):
        """How many tasks are running (in one project, or in all of them)."""
        tasks = self.running_tasks()
        if workspace_id:
            tasks = [t for t in tasks if t.get("workspace_id") == workspace_id]
        return len(tasks)

    # ---- one round --------------------------------------------------------
    def round(self):
        provider = self.args.provider
        queued = self.store.list(status=QUEUED)
        if not queued:
            # Nothing to do: do not call the provider either. The quota is
            # only interesting when there is work waiting for it.
            log("nothing queued")
            return 0

        # The quota is read fresh whenever there is work: the queue polls
        # every --interval seconds, and a task that waits for a quota change
        # must not be kept waiting by a cached answer.
        left, window = self.quota_left(provider, fresh=True)
        if left is None:
            if not self.args.allow_without_quota:
                log("no quota to look at - not starting anything "
                    "(use --allow-without-quota to work anyway)")
                return 0
            log("working anyway (--allow-without-quota)")
        else:
            if left < self.args.min_remaining:
                log("quota of %r: %s%% left (need %d%%) - not starting anything"
                    % (provider, left, self.args.min_remaining))
                return 0
            log("quota of %r: %s%% left - going to work" % (provider, left))

        # The queue is serial *per project*: a project (a workspace, which is
        # what an "agent" is here) works on one task at a time, while other
        # projects may run in parallel. --max-concurrent caps the total; 0
        # means "only the per-project rule".
        if self.args.max_concurrent and self.running_count() >= self.args.max_concurrent:
            log("%d task(s) already running - waiting" % self.running_count())
            return 0

        # Highest priority first, then the queue order (FIFO). A task that
        # asked for a different provider is left alone.
        queued.sort(key=lambda t: (-int(t.get("priority") or 0),
                                   t.get("created_at") or "", t.get("id") or ""))

        started = 0
        for task in queued:
            if started >= self.args.max_per_round:
                break
            if self.args.max_concurrent and self.running_count() >= self.args.max_concurrent:
                break
            # One task at a time per project.
            if self.running_count(task.get("workspace_id")):
                continue
            wanted = task.get("provider")
            if wanted and wanted != provider:
                log("  task %s wants provider %r, not %r - leaving it alone"
                    % (task.get("id"), wanted, provider))
                continue
            if self.start_task(task, provider):
                started += 1
        return started

    # ---- the tasks this process inherited --------------------------------
    def recover_stale_tasks(self, min_age=None):
        """A task that was running when this process started is not running any more.

        The worker thread died with the process, so the record would sit in
        "running" for ever: the scheduler only starts queued tasks, and
        POST .../start refuses anything else. Mark those tasks failed, with a
        reason that says what happened, so they can be queued again.

        Only tasks that have not been touched for a minute are recovered: another
        task server working on the same records would otherwise have its task
        pulled out from under it.
        """
        cutoff = time.time() - (self.args.stale_after if min_age is None else min_age)
        recovered = []
        for task in self.store.list(status=RUNNING):
            stamp = parse_iso(task.get("updated_at") or task.get("started_at") or "")
            if stamp is not None and stamp > cutoff:
                continue                    # somebody is still working on it
            workspace_id = task.get("workspace_id")
            task_id = task.get("id")
            self.store.update(workspace_id, task_id, lambda t: t.update({
                "status": FAILED,
                "finished_at": now_iso(),
                "error": "the task server restarted while this task was running; "
                         "queue it again if the work still has to be done",
            }))
            log("  task %s was running when the server started - marked failed" % task_id)
            recovered.append(self.store.load(workspace_id, task_id))
        for task in recovered:
            if task is not None:
                self.events.publish({"type": "task_failed", "task": task})
        return recovered

    # ---- running one task -------------------------------------------------
    def start_task(self, task, provider=None):
        """Run a task now: the turn goes through the agent, the record follows."""
        task_id = task.get("id")
        workspace_id = task.get("workspace_id")
        if task_id in self.running:
            log("  task %s is already running here", task_id)
            return False

        provider = provider or task.get("provider") or self.args.provider
        context = self.context_for(task)
        # A task that was waiting for an answer carries it with it: the answer
        # is the next thing said in the task's session.
        pending = task.get("pending_message") or ""
        self.store.update(workspace_id, task_id, lambda t: t.update({
            "status": RUNNING,
            "provider": provider,
            "started_at": now_iso(),
            "finished_at": "",
            "error": "",
            "needs_confirmation": False,
            "question": "",
            "note": "started",
            "pending_message": "",
        }))

        thread = threading.Thread(target=self._run,
                                  args=(workspace_id, task_id, provider, context, pending or None),
                                  daemon=True)
        self.running[task_id] = thread
        thread.start()
        log("  started task %s (%s)" % (task_id, task.get("title") or task.get("prompt", "")[:40]))
        return True

    @staticmethod
    def context_for(task):
        """What the agent is told about the task: id, title, request."""
        out = task.get("id") or ""
        if task.get("title"):
            out += " (%s)" % task["title"]
        if task.get("prompt"):
            out += ": " + task["prompt"]
        return out

    def _run(self, workspace_id, task_id, provider, context, message=None):
        try:
            self._consume(workspace_id, task_id, provider, context, message)
        except AgentError as exc:
            self.store.update(workspace_id, task_id, lambda t: t.update({
                "status": FAILED, "error": str(exc), "finished_at": now_iso()}))
            self.events.publish({"type": "task_failed", "task": self.store.load(workspace_id, task_id)})
        except Exception as exc:  # noqa: BLE001 - the queue must not die on one task
            self.store.update(workspace_id, task_id, lambda t: t.update({
                "status": FAILED, "error": "%s: %s" % (type(exc).__name__, exc),
                "finished_at": now_iso()}))
            self.events.publish({"type": "task_failed", "task": self.store.load(workspace_id, task_id)})
        finally:
            self.running.pop(task_id, None)

    def _consume(self, workspace_id, task_id, provider, context, message=None):
        task = self.store.load(workspace_id, task_id)
        if task is None:
            return
        # The answer to a question continues the work in the same session; the
        # task's own prompt is what the *first* turn says.
        message = message if message is not None else task.get("prompt", "")
        session_id = task.get("session_id") or ""
        report = {"progress": None, "note": "", "needs_confirmation": False, "question": ""}
        answer = ""
        allow_paths = task.get("allow_paths") or []
        for event in self.agent.chat(workspace_id, message, provider=provider,
                                     session_id=session_id, task_id=task_id,
                                     task_context=context, allow_paths=allow_paths):
            kind = event.get("type")
            if kind == "start":
                session_id = event.get("session_id") or session_id
                self.store.update(workspace_id, task_id,
                                  lambda t: t.update({"session_id": session_id}))
            elif kind == "tool_call" and event.get("name") == "toolkit_task":
                self._apply_report(workspace_id, task_id, event, report)
            elif kind == "artifacts":
                # The files this turn produced: the page shows them under the
                # finished task.
                items = event.get("items") or []
                if items:
                    self.store.update(workspace_id, task_id,
                                      lambda t: t.update({"artifacts": items}))
            elif kind == "content_delta":
                answer += event.get("text") or ""
            elif kind == "done":
                answer = event.get("content") or answer
                status = WAITING if report["needs_confirmation"] else DONE
                self.store.update(workspace_id, task_id, lambda t: t.update({
                    "status": status,
                    "progress": 100 if status == DONE else t.get("progress", 0),
                    "result": answer,
                    "finished_at": now_iso(),
                    "error": "",
                }))
                self.events.publish({"type": "task_done" if status == DONE else "task_waiting_confirmation",
                                      "task": self.store.load(workspace_id, task_id)})
                return
            elif kind == "error":
                self.store.update(workspace_id, task_id, lambda t: t.update({
                    "status": FAILED, "error": event.get("message") or "the turn failed",
                    "finished_at": now_iso()}))
                self.events.publish({"type": "task_failed", "task": self.store.load(workspace_id, task_id)})
                return
            elif kind == "stopped":
                self.store.update(workspace_id, task_id, lambda t: t.update({
                    "status": CANCELLED, "finished_at": now_iso()}))
                self.events.publish({"type": "task_cancelled", "task": self.store.load(workspace_id, task_id)})
                return
        # The stream ended without a verdict (the agent died, the connection broke).
        self.store.update(workspace_id, task_id, lambda t: t.update({
            "status": FAILED, "error": "the turn ended without an answer",
            "finished_at": now_iso()}))
        self.events.publish({"type": "task_failed", "task": self.store.load(workspace_id, task_id)})

    def _apply_report(self, workspace_id, task_id, event, report):
        """The agent's own report (toolkit_task): progress, note, a question."""
        raw = event.get("arguments")
        args = {}
        if isinstance(raw, str):
            try:
                args = json.loads(raw) if raw.strip() else {}
            except ValueError:
                args = {}
        elif isinstance(raw, dict):
            args = raw
        if not isinstance(args, dict):
            args = {}

        progress = args.get("progress")
        note = str(args.get("note") or "").strip()
        question = str(args.get("question") or "").strip()
        needs = bool(args.get("needs_confirmation"))

        report["needs_confirmation"] = needs
        if question:
            report["question"] = question
        if note:
            report["note"] = note

        def apply(t):
            if isinstance(progress, int) and 0 <= progress <= 100:
                t["progress"] = progress
            if note:
                t["note"] = note
                t.setdefault("steps", []).append(
                    {"at": now_iso(), "progress": t.get("progress", 0), "note": note})
            if needs:
                t["needs_confirmation"] = True
                t["question"] = question

        self.store.update(workspace_id, task_id, apply)
        self.events.publish({"type": "task_updated", "task": self.store.load(workspace_id, task_id)})

    # ---- the loop ---------------------------------------------------------
    def run_forever(self):
        while True:
            try:
                self.round()
            except AgentError as exc:
                log("round failed: %s" % exc)
            except Exception as exc:  # noqa: BLE001
                log("round failed: %s: %s" % (type(exc).__name__, exc))
            time.sleep(self.args.interval)


class Handler(BaseHTTPRequestHandler):
    """The task management interface."""

    server_version = "tea-task-server/1.0"
    protocol_version = "HTTP/1.1"

    # -- plumbing ------------------------------------------------------
    def log_message(self, *args):
        pass

    def _json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status, code, message):
        self._json(status, {"error": {"code": code, "message": message}})

    def _body(self):
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except ValueError:
            return None

    def _query(self):
        return urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)

    def _int(self, name, default=0):
        raw = self._one(name)
        try:
            return int(str(raw).strip())
        except (TypeError, ValueError):
            return default

    def _key(self):
        """The project key the caller presents ("" = the local operator)."""
        raw = self.headers.get("Authorization") or ""
        if raw.lower().startswith("bearer "):
            raw = raw[7:].strip()
        if not raw:
            raw = self.headers.get("X-Project-Key") or ""
        return raw.strip()

    def _project_of_caller(self):
        """The project the caller's key belongs to ("" = operator / no key)."""
        return self.app.store.project_of_key(self._key())

    def _forbid(self, what):
        return self._error(403, "forbidden",
                           "this key does not open %s" % what)

    def _check_project(self, task):
        """A task belongs to a project: a key of another project gets nothing."""
        if task is None:
            return True
        caller = self._project_of_caller()
        if not caller:
            return True              # the local operator
        if caller == (task.get("workspace_id") or ""):
            return True
        return False

    def _one(self, name, default=""):
        values = self._query().get(name) or []
        return values[0] if values else default

    @property
    def app(self):
        return self.server.app

    # -- routes ---------------------------------------------------------
    def do_OPTIONS(self):  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/ui", "/index.html"):
            return self._page()
        if path == "/api/health":
            return self._health()
        if path == "/api/tasks":
            return self._list()
        if path == "/api/events":
            return self._events()
        if path == "/api/audit":
            return self._audit()
        if path == "/api/projects":
            return self._projects()
        if path.startswith("/api/tasks/"):
            rest = path[len("/api/tasks/"):]
            if "/" in rest:
                task_id, action = rest.split("/", 1)
                if action == "events":
                    return self._task_events(task_id)
                if action == "export":
                    return self._export(task_id)
                return self._error(404, "not_found", "no such action: %s" % action)
            return self._get(rest)
        return self._error(404, "not_found", "no such endpoint: %s" % path)

    def do_POST(self):  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/tasks":
            return self._create()
        if path == "/api/projects":
            return self._project_key()
        if path.startswith("/api/tasks/"):
            rest = path[len("/api/tasks/"):]
            if "/" in rest:
                task_id, action = rest.split("/", 1)
                return self._action(task_id, action)
            return self._error(404, "not_found", "no such action")
        return self._error(404, "not_found", "no such endpoint: %s" % path)

    def do_DELETE(self):  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        if path.startswith("/api/tasks/"):
            return self._delete(path.rsplit("/", 1)[-1])
        return self._error(404, "not_found", "no such endpoint: %s" % path)

    # -- handlers -------------------------------------------------------
    def _page(self):
        """The task queue's own page: queued, running, finished."""
        html = self.app.page_html()
        if not html:
            return self._error(404, "not_found",
                               "the page file (task_ui.html) is missing next to this script")
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _health(self):
        agent = self.app.agent
        try:
            health = agent.health()
            agent_ok = True
        except AgentError as exc:
            health = {"error": str(exc)}
            agent_ok = False
        left, _ = self.app.engine.quota_left(self.app.args.provider)
        self._json(200, {
            "ok": True,
            "agent": agent.base,
            "agent_ok": agent_ok,
            "agent_health": health,
            "provider": self.app.args.provider,
            "quota_percent": left,
            "data_dir": self.app.store.root,
            "counts": self.app.store.counts(),
            "metrics": self.app.metrics(),
        })

    def _list(self):
        workspace_id = self._one("workspace_id")
        status = self._one("status")
        query = self._query()
        page = self._int("page", 0)
        page_size = self._int("page_size", 0)
        filters = {"workspace_id": workspace_id or None, "status": status or None,
                   "created_from": self._one("created_from"),
                   "created_to": self._one("created_to"),
                   "tag": self._one("tag"), "business_id": self._one("business_id")}
        caller = self._project_of_caller()
        if caller:
            # A key sees its own project and nothing else.
            filters["workspace_id"] = caller
        everything = self.app.store.list(**filters)
        total = len(everything)
        if page_size and page_size > 0:
            page = max(1, page)
            start = (page - 1) * page_size
            everything = everything[start:start + page_size]
        elif page and page > 0:
            start = (page - 1) * 50
            everything = everything[start:start + 50]
        self._json(200, {"tasks": [self.app.decorate(t) for t in everything],
                         "counts": self.app.store.counts(workspace_id or None),
                         "total": total, "page": page or 1,
                         "page_size": page_size or 0})

    def _get(self, task_id):
        workspace_id = self._one("workspace_id")
        task = self.app.store.load(workspace_id, task_id)
        if task is None:
            return self._error(404, "not_found", "no task %r" % task_id)
        if not self._check_project(task):
            return self._forbid("task %s" % task_id)
        full = self._one("include").lower() in ("full", "1", "true")
        self._json(200, self.app.decorate(task, full=full))

    def _projects(self):
        """The projects (workspaces) the queue knows about.

        The keys are not here: a caller that may see the list may not open
        somebody else's project.
        """
        known = self.app.store.projects()
        out = []
        try:
            workspaces = self.app.agent.workspaces() or []
        except AgentError:
            workspaces = []
        for ws in workspaces:
            workspace_id = ws.get("id") or ""
            if not workspace_id:
                continue
            out.append({"workspace_id": workspace_id,
                        "name": ws.get("name") or "",
                        "has_key": bool(known.get(workspace_id))})
        for workspace_id in known:
            if not any(p["workspace_id"] == workspace_id for p in out):
                out.append({"workspace_id": workspace_id, "name": "", "has_key": True})
        out.sort(key=lambda p: p["workspace_id"])
        self._json(200, {"projects": out})

    def _project_key(self):
        """The key of a project - for the local operator only.

        A caller that already carries a key may not ask for another one: that
        would make the key worthless.
        """
        if self._key():
            return self._forbid("another project's key")
        body = self._body()
        if body is None:
            return self._error(400, "bad_request", "the request body is not JSON")
        workspace_id = str(body.get("workspace_id") or "").strip()
        if not workspace_id:
            return self._error(400, "bad_request", "'workspace_id' is required")
        key = self.app.store.project_key(workspace_id)
        self.app.store.audit("project_key", "", {"workspace_id": workspace_id})
        self._json(200, {"workspace_id": workspace_id, "key": key})

    def _task_events(self, task_id):
        """The task's event log, optionally only what came after the last one
        the client saw."""
        workspace_id = self._one("workspace_id")
        task = self.app.store.load(workspace_id, task_id)
        if task is None:
            return self._error(404, "not_found", "no task %r" % task_id)
        if not self._check_project(task):
            return self._forbid("task %s" % task_id)
        since = self._int("since_event_id", 0)
        events = self.app.store.events(task_id, since)
        self._json(200, {"task_id": task_id, "events": events,
                         "last_event_id": events[-1]["event_id"] if events else since})

    def _export(self, task_id):
        """The result of a task, as one document or as a zip.

        The zip is what somebody wants six months later: the manifest, the
        final answer, the conversation, the events, the files. The layout is
        the one the requirement suggests.
        """
        import io
        import zipfile

        workspace_id = self._one("workspace_id")
        task = self.app.store.load(workspace_id, task_id)
        if task is None:
            return self._error(404, "not_found", "no task %r" % task_id)
        if not self._check_project(task):
            return self._forbid("task %s" % task_id)
        fmt = (self._one("format") or "json").lower()
        with_artifacts = self._one("include_artifacts").lower() not in ("0", "false", "no")
        session = self.app.agent.session(task.get("session_id") or "")
        messages = session.get("messages") or []
        events = self.app.store.events(task_id)
        artifacts = task.get("artifacts") or []
        manifest = {
            "task_id": task_id,
            "workspace_id": task.get("workspace_id") or "",
            "title": task.get("title") or "",
            "status": task.get("status") or "",
            "priority": task.get("priority") or 0,
            "attempt": task.get("attempt") or 1,
            "created_at": task.get("created_at") or "",
            "started_at": task.get("started_at") or "",
            "finished_at": task.get("finished_at") or "",
            "session_id": task.get("session_id") or "",
            "error": task.get("error") or "",
            "artifacts": artifacts,
            "exported_at": now_iso(),
        }
        if fmt == "zip":
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
                z.writestr("final_answer.md", (task.get("result") or "").encode("utf-8"))
                z.writestr("error.json", json.dumps(
                    {"error": task.get("error") or ""}, ensure_ascii=False, indent=2))
                z.writestr("conversation.jsonl", "".join(
                    json.dumps(m, ensure_ascii=False) + "\n" for m in messages).encode("utf-8"))
                z.writestr("events.jsonl", "".join(
                    json.dumps(e, ensure_ascii=False) + "\n" for e in events).encode("utf-8"))
                if with_artifacts:
                    for art in artifacts:
                        rel = str(art.get("path") or "")
                        if not rel:
                            continue
                        data = self.app.agent.download(task.get("workspace_id") or "", rel)
                        if not data:
                            continue
                        name = "artifacts/" + rel.lstrip("/")
                        z.writestr(name, data)
            body = buf.getvalue()
            self.app.store.audit("export", task_id, {"format": "zip", "bytes": len(body)})
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition",
                             'attachment; filename="task-%s.zip"' % task_id)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if fmt != "json":
            return self._error(400, "bad_request", "format must be json or zip")
        document = {"manifest": manifest, "final_answer": task.get("result") or "",
                    "events": events, "conversation": messages, "artifacts": artifacts}
        self.app.store.audit("export", task_id, {"format": "json"})
        self._json(200, document)

    def _audit(self):
        task_id = self._one("task_id")
        limit = self._int("limit", 200)
        self._json(200, {"entries": self.app.store.audit_log(task_id, max(1, min(limit, 1000)))})

    def _create(self):
        body = self._body()
        if body is None:
            return self._error(400, "bad_request", "the request body is not JSON")
        workspace_id = str(body.get("workspace_id") or "").strip()
        prompt = str(body.get("prompt") or "").strip()
        if not workspace_id:
            # The same default the scheduler uses: the first workspace.
            workspace_id = self.app.default_workspace()
        if not workspace_id:
            return self._error(400, "bad_request",
                               "'workspace_id' is required (the agent has no workspace)")
        if not prompt:
            return self._error(400, "bad_request", "'prompt' is required")
        # Extra directories the file tools may touch while this task runs. The
        # agent's boundary is the workspace; this is the task saying "and these
        # ones, for this task only".
        allow_paths = []
        raw_paths = body.get("allow_paths")
        if isinstance(raw_paths, str):
            raw_paths = raw_paths.replace(";", ",").split(",")
        if isinstance(raw_paths, list):
            for one in raw_paths:
                if not isinstance(one, str):
                    continue
                one = one.strip()
                if one:
                    allow_paths.append(os.path.abspath(os.path.expanduser(one)))
        caller = self._project_of_caller()
        if caller and workspace_id != caller:
            return self._forbid("project %s" % workspace_id)
        if body.get("workspace_id"):
            # A task that names a workspace nobody has would sit in the queue and
            # fail later, with a much less obvious reason. Check it now - unless
            # the agent is down, in which case the queue has to keep accepting.
            try:
                known = [w.get("id") for w in self.app.agent.workspaces()]
            except AgentError:
                known = []          # the agent is down: the queue keeps accepting
            if known and workspace_id not in known:
                return self._error(400, "bad_request",
                                   "no workspace %r (the agent knows: %s)"
                                   % (workspace_id, ", ".join(known)))
        # The idempotency key: the same key must not queue the same work twice
        # (a client that retries a submission, a bot that repeats a command).
        key = str(self.headers.get("Idempotency-Key") or body.get("idempotency_key") or "").strip()
        if key:
            existing = self.app.store.find_by_idempotency(key)
            if existing is not None:
                self._json(200, self.app.decorate(existing))
                return
        try:
            priority = int(body.get("priority") or 0)
        except (TypeError, ValueError):
            priority = 0
        task = {
            "id": "t_" + os.urandom(6).hex(),
            "workspace_id": workspace_id,
            "prompt": prompt,
            "title": str(body.get("title") or "").strip(),
            "provider": str(body.get("provider") or "").strip(),
            "model": str(body.get("model") or "").strip(),
            "allow_paths": allow_paths,
            "session_id": "",
            "status": QUEUED,
            "progress": 0,
            "note": "",
            "steps": [],
            "needs_confirmation": False,
            "question": "",
            "error": "",
            "result": "",
            "meta": body.get("meta") if isinstance(body.get("meta"), dict) else {},
            "priority": priority,
            "attempt": 1,
            "idempotency_key": key,
            "pending_message": "",
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "started_at": "",
            "finished_at": "",
        }
        self.app.store.create(task)
        self.app.publish("task_created", task)
        self.app.store.audit("submit", task["id"], {"prompt": prompt[:200], "key": key})
        self._json(200, self.app.decorate(task))

        self.app.publish("task_created", task)
        self._json(200, task)

    def _delete(self, task_id):
        workspace_id = self._one("workspace_id")
        task = self.app.store.load(workspace_id, task_id)
        if task is None:
            return self._error(404, "not_found", "no task %r" % task_id)
        # The record knows its own workspace - use it, the caller may not have
        # said which one.
        owner = task.get("workspace_id") or workspace_id
        if not self._check_project(task):
            return self._forbid("task %s" % task_id)
        if task.get("status") == RUNNING:
            self.app.cancel_task(owner, task_id)
        if not self.app.store.delete(owner, task_id):
            return self._error(404, "not_found", "no task %r" % task_id)
        self.app.publish("task_deleted", task)
        self._json(200, {"ok": True, "id": task_id})

    def _action(self, task_id, action):
        # The task id is unique, so the workspace is optional: look the task up
        # and use the workspace it says it belongs to.
        workspace_id = self._one("workspace_id")
        task = self.app.store.load(workspace_id, task_id)
        if task is None:
            return self._error(404, "not_found", "no task %r" % task_id)
        workspace_id = task.get("workspace_id") or workspace_id
        if not self._check_project(task):
            return self._forbid("task %s" % task_id)
        if action == "start":
            return self._start(workspace_id, task)
        if action == "cancel":
            return self._cancel(workspace_id, task)
        if action == "confirm":
            return self._confirm(workspace_id, task)
        if action == "retry":
            return self._retry(workspace_id, task)
        return self._error(404, "not_found", "no such action: %s" % action)

    def _start(self, workspace_id, task):
        if task.get("status") == RUNNING:
            return self._error(409, "task_running", "task %s is already running" % task.get("id"))
        provider = task.get("provider") or self.app.args.provider
        # The same gate the scheduler uses: the work starts while there is quota.
        left, _ = self.app.engine.quota_left(provider)
        if left is None and not self.app.args.allow_without_quota:
            return self._error(409, "no_quota",
                               "provider %r does not report a quota (use --allow-without-quota)"
                               % provider)
        if left is not None and left < self.app.args.min_remaining:
            return self._error(409, "no_quota",
                               "quota of %r: %s%% left (need %d%%)"
                               % (provider, left, self.app.args.min_remaining))
        self.app.engine.start_task(task, provider)
        self._json(200, self.app.store.load(workspace_id, task.get("id")) or task)

    def _cancel(self, workspace_id, task):
        if task.get("status") in TERMINAL:
            return self._error(409, "already_finished",
                               "task %s is %s - it cannot be cancelled"
                               % (task.get("id"), task.get("status")))
        self.app.cancel_task(workspace_id, task.get("id"))
        self.app.store.audit("cancel", task.get("id"))
        self._json(200, self.app.store.load(workspace_id, task.get("id")) or task)

    def _confirm(self, workspace_id, task):
        body = self._body()
        if body is None:
            return self._error(400, "bad_request", "the request body is not JSON")
        message = str(body.get("message") or "").strip()
        if not message:
            return self._error(400, "bad_request", "'message' is required")
        if not task.get("needs_confirmation"):
            # Replying twice to the same question must not queue the task
            # twice: the answer is already there, so this is a no-op.
            if task.get("pending_message"):
                self._json(200, self.app.decorate(task))
                return
            return self._error(409, "not_waiting",
                               "task %s is not waiting for an answer" % task.get("id"))
        # The answer goes back into the queue - at the end of the line, and
        # without interrupting whatever is running now. The task carries the
        # answer with it: the agent picks it up as the next turn in the same
        # session.
        self.app.store.update(workspace_id, task.get("id"), lambda t: t.update({
            "status": QUEUED,
            "needs_confirmation": False,
            "question": "",
            "pending_message": message,
            "note": "answered: " + message[:80],
        }))
        self.app.publish("task_answered", self.app.store.load(workspace_id, task.get("id")))
        self.app.store.audit("answer", task.get("id"), {"message": message[:200]})
        self._json(200, self.app.decorate(self.app.store.load(workspace_id, task.get("id")) or task))

    def _retry(self, workspace_id, task):
        """Try a failed task again: the same task, one attempt further."""
        if task.get("status") not in (FAILED, CANCELLED):
            return self._error(409, "not_failed",
                               "task %s is %s - only a failed or cancelled task is retried"
                               % (task.get("id"), task.get("status")))
        self.app.store.update(workspace_id, task.get("id"), lambda t: t.update({
            "status": QUEUED,
            "error": "",
            "finished_at": "",
            "progress": 0,
            "needs_confirmation": False,
            "question": "",
            "note": "attempt %d" % (int(t.get("attempt") or 1) + 1),
            "attempt": int(t.get("attempt") or 1) + 1,
        }))
        self.app.publish("task_retried", self.app.store.load(workspace_id, task.get("id")))
        self.app.store.audit("retry", task.get("id"))
        self._json(200, self.app.decorate(self.app.store.load(workspace_id, task.get("id")) or task))

    def _events(self):
        """The queue's event stream (SSE)."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        queue, state, listener = self.app.events.subscribe()
        try:
            while state["alive"]:
                if queue:
                    event = queue.pop(0)
                    frame = "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"
                    self.wfile.write(frame.encode("utf-8"))
                    self.wfile.flush()
                else:
                    time.sleep(0.3)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.app.events.unsubscribe(listener)


class App:
    """Everything the handler needs, in one place."""

    def __init__(self, args):
        self.args = args
        self._page = None
        self._task_ws_id = ""
        data_dir = args.data_dir
        if not data_dir:
            data_dir = os.path.join(os.path.abspath(os.path.expanduser(args.workspace_path)),
                                    "tasks")
        self.store = Store(data_dir)
        self.agent = Agent(args.agent)
        self.events = Events(self.store)
        self.engine = Engine(self.store, self.agent, self.events, args)

    def page_html(self):
        """The page, read once at startup (it is a static file next to us)."""
        if self._page is None:
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "task_ui.html")
            try:
                with open(path, encoding="utf-8") as f:
                    self._page = f.read()
            except OSError:
                self._page = ""
        return self._page

    def publish(self, event_type, task, **extra):
        """Tell everybody who is listening (and write the event down)."""
        event = {"type": event_type, "task": task}
        event.update(extra)
        self.events.publish(event)
        return event

    def decorate(self, task, full=False):
        """Add the bits a reader wants: where the record is, which workspace,
        the agent to reach, and where the task stands in the queue."""
        task = dict(task)
        task["record_path"] = self.store.record_path(task.get("workspace_id"), task.get("id"))
        task["agent_url"] = self.agent.base
        path = self.workspace_path(task.get("workspace_id"))
        if path:
            task["workspace_path"] = path
        # Where the task stands: only a queued task has a place in the line.
        task["queue_position"] = self.queue_position(task)
        task["current_step"] = task.get("note") or ""
        if full:
            task["events"] = self.store.events(task.get("id") or "")
            task["history"] = task.get("steps") or []
        return task

    def queue_position(self, task):
        """1 = next in line, 0 = not queued (nothing to wait for)."""
        if not task or task.get("status") != QUEUED:
            return 0
        queued = self.store.list(status=QUEUED)
        queued.sort(key=lambda t: (-int(t.get("priority") or 0),
                                   t.get("created_at") or "", t.get("id") or ""))
        for index, one in enumerate(queued):
            if one.get("id") == task.get("id"):
                return index + 1
        return 0

    def metrics(self):
        """What somebody watching the queue wants to see.

        The queue length and how long the work waits are the two numbers that
        say whether the queue is healthy; the failure rate says whether the
        work itself is.
        """
        tasks = self.store.list()
        counts = self.store.counts()
        waits = []
        durations = []
        failed = 0
        finished = 0
        for task in tasks:
            created = parse_iso(task.get("created_at") or "")
            started = parse_iso(task.get("started_at") or "")
            ended = parse_iso(task.get("finished_at") or "")
            if task.get("status") == QUEUED and created is not None:
                waits.append(max(0, time.time() - created))
            if started is not None and ended is not None:
                durations.append(max(0, ended - started))
            if task.get("status") in (DONE, FAILED):
                finished += 1
                if task.get("status") == FAILED:
                    failed += 1
        return {
            "queue_length": counts.get(QUEUED, 0),
            "running": counts.get(RUNNING, 0),
            "waiting_interaction": counts.get(WAITING, 0),
            "done": counts.get(DONE, 0),
            "failed": counts.get(FAILED, 0),
            "cancelled": counts.get(CANCELLED, 0),
            "avg_queue_wait_s": round(sum(waits) / len(waits), 1) if waits else 0.0,
            "avg_task_duration_s": (round(sum(durations) / len(durations), 1)
                                    if durations else 0.0),
            "failure_rate": (round(failed / float(finished), 3) if finished else 0.0),
        }

    def workspace_name(self, workspace_id):
        try:
            for ws in self.agent.workspaces():
                if ws.get("id") == workspace_id:
                    return ws.get("name") or ""
        except AgentError:
            pass
        return ""

    def workspace_path(self, workspace_id):
        if not workspace_id:
            return ""
        try:
            for ws in self.agent.workspaces():
                if ws.get("id") == workspace_id:
                    return ws.get("path", "")
        except AgentError:
            pass
        return ""

    def task_workspace_id(self):
        """The queue's OWN workspace: a directory of its own, registered with
        the agent so the agent can work there.

        The queue must not scribble into somebody's project: a background task
        that writes files belongs in a directory that is nobody's checkout. So
        the service keeps one workspace of its own (by default under the tea
        home), and every task that does not name a workspace works there.
        """
        if self.args.workspace:
            return self.args.workspace        # the operator named one
        if self._task_ws_id:
            return self._task_ws_id
        path = os.path.abspath(os.path.expanduser(self.args.workspace_path))
        try:
            os.makedirs(path, exist_ok=True)
            for ws in self.agent.workspaces():
                if os.path.abspath(ws.get("path", "")) == path:
                    self._task_ws_id = ws.get("id", "")
                    return self._task_ws_id
            created = self.agent.register_workspace(path, self.args.workspace_name)
            self._task_ws_id = created.get("id", "")
            log("task workspace: %s (%s)" % (path, self._task_ws_id or "?"))
        except (AgentError, OSError) as exc:
            log("cannot prepare the task workspace %s: %s" % (path, exc))
        return self._task_ws_id

    def default_workspace(self):
        """The workspace a task goes to when the caller does not name one."""
        own = self.task_workspace_id()
        if own:
            return own
        wanted = self.args.workspace
        try:
            workspaces = self.agent.workspaces()
        except AgentError:
            return wanted
        if not workspaces:
            return wanted
        if wanted:
            for ws in workspaces:
                if ws.get("id") == wanted:
                    return wanted
        return workspaces[0].get("id", "")

    def cancel_task(self, workspace_id, task_id):
        task = self.store.load(workspace_id, task_id)
        if task is None:
            return False
        if task.get("status") in TERMINAL:
            # A finished task is history: cancelling it would rewrite what
            # happened.
            return False
        session_id = task.get("session_id")
        if session_id:
            try:
                self.agent.stop(session_id)
            except AgentError as exc:
                log("could not stop the turn of %s: %s" % (task_id, exc))
        self.store.update(workspace_id, task_id, lambda t: t.update({
            "status": CANCELLED, "finished_at": now_iso()}))
        self.events.publish({"type": "task_cancelled", "task": self.store.load(workspace_id, task_id)})
        return True


def pick_workspace(agent, wanted):
    workspaces = agent.workspaces()
    if not workspaces:
        raise SystemExit("the agent has no workspaces; add one first")
    if wanted:
        for ws in workspaces:
            if ws.get("id") == wanted:
                return wanted
        raise SystemExit("no workspace with id %r (known: %s)"
                         % (wanted, ", ".join(w.get("id", "?") for w in workspaces)))
    return workspaces[0].get("id")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="The task queue: enqueue, run and watch tasks through the agent's API.")
    parser.add_argument("--agent", default=DEFAULT_AGENT,
                        help="the agent's HTTP API (default: %s)" % DEFAULT_AGENT)
    parser.add_argument("--host", default="127.0.0.1", help="where to serve the task API")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help="the port of the task API (default: %d)" % DEFAULT_PORT)
    parser.add_argument("--data-dir", default="",
                        help="where the task records live (default: <workspace-path>/tasks)")
    parser.add_argument("--workspace", default=os.environ.get("TEA_AGENT_WORKSPACE", ""),
                        help="the workspace whose queue is worked on (default: the queue's own)")
    parser.add_argument("--workspace-path", default=DEFAULT_WORKSPACE_PATH,
                        help="the queue's own workspace directory (created and registered with the agent)")
    parser.add_argument("--workspace-name", default=os.environ.get("TEA_TASK_WORKSPACE_NAME", "任务"),
                        help="the name the queue's workspace gets in the agent")
    parser.add_argument("--provider", default=DEFAULT_PROVIDER,
                        help="the provider whose quota gates the work (default: %s)" % DEFAULT_PROVIDER)
    parser.add_argument("--window", default="window", choices=["window", "weekly"],
                        help="which quota window to watch (default: the short one)")
    parser.add_argument("--min-remaining", type=float, default=5.0,
                        help="only work while at least this much of the window is left (%%)")
    parser.add_argument("--allow-without-quota", action="store_true",
                        help="work even when the provider does not report a quota")
    parser.add_argument("--max-concurrent", type=int, default=0,
                        help="how many tasks may run at the same time in total "
                             "(0 = only the per-project rule: one per project)")
    parser.add_argument("--quota-interval", type=float, default=60.0,
                        help="how long the provider's quota answer is reused (the queue polls "
                             "every --interval seconds; the quota endpoint is a real call)")
    parser.add_argument("--stale-after", type=float, default=300.0,
                        help="a task that was not touched for this many seconds does not hold "
                             "a slot any more")
    parser.add_argument("--interval", type=float, default=2.0,
                        help="seconds between rounds (default: 60)")
    parser.add_argument("--max-per-round", type=int, default=4,
                        help="how many queued tasks one round may start (the per-project rule still "
                             "holds: two tasks of the same project never run together)")
    parser.add_argument("--submit", metavar="PROMPT",
                        help="put a task in the queue and exit (no server needed)")
    parser.add_argument("--title", default="", help="title for --submit")
    parser.add_argument("--allow-path", action="append", default=[],
                        help="a directory the task may touch (repeatable; the file "
                             "tools are otherwise fenced to the workspace)")
    parser.add_argument("--no-serve", action="store_true",
                        help="do not serve; only run the scheduler (for a cron-style setup)")
    args = parser.parse_args(argv)

    app = App(args)

    if args.submit:
        agent = Agent(args.agent)
        try:
            workspace_id = pick_workspace(agent, args.workspace)
        except (AgentError, SystemExit) as exc:
            print("cannot find a workspace: %s" % exc, file=sys.stderr)
            return 2
        task = {
            "id": "t_" + os.urandom(6).hex(),
            "workspace_id": workspace_id,
            "prompt": args.submit,
            "title": args.title,
            "provider": args.provider,
            "model": "",
            "allow_paths": [os.path.abspath(os.path.expanduser(p)) for p in args.allow_path],
            "session_id": "",
            "status": QUEUED,
            "progress": 0,
            "note": "",
            "steps": [],
            "needs_confirmation": False,
            "question": "",
            "error": "",
            "result": "",
            "meta": {},
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "started_at": "",
            "finished_at": "",
        }
        app.store.create(task)
        log("queued task %s (%s)" % (task["id"], task["title"] or ""))
        return 0

    # The queue's own workspace: created (and registered with the agent) once.
    app.task_workspace_id()

    # The records of a previous run: a task that was running then is not
    # running now (its worker died with that process).
    app.engine.recover_stale_tasks()

    try:
        health = app.agent.health()
        log("agent up: %s (provider %s, model %s)"
            % (args.agent, health.get("provider"), health.get("model")))
    except AgentError as exc:
        # As a service, the agent may simply not be up yet (or be restarting).
        # Serving anyway and letting the scheduler retry every round is more
        # useful than exiting and letting systemd bounce us.
        log("the agent at %s is not reachable yet (%s) - serving anyway, retrying every round"
            % (args.agent, exc))
    log("task server: http://%s:%d  (records in %s)" % (args.host, args.port, app.store.root))

    if not args.no_serve:
        server = ThreadingHTTPServer((args.host, args.port), Handler)
        server.app = app
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()

    try:
        app.engine.run_forever()
    except KeyboardInterrupt:
        log("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
