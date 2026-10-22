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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_AGENT = os.environ.get("TEA_AGENT_URL", "http://127.0.0.1:8282")
DEFAULT_PORT = int(os.environ.get("TEA_TASK_PORT", "8283"))
DEFAULT_DATA_DIR = os.path.expanduser(os.environ.get(
    "TEA_TASK_DATA", os.path.join("~", ".tea_agent", "tasks")))
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

    def usage(self, provider):
        """How much of the provider's subscription is left.

        Raises AgentError(400) when the provider does not report a quota at all.
        """
        return self._request("GET", "/api/providers/" + urllib.parse.quote(provider) + "/usage")

    def stop(self, session_id):
        return self._request("POST", "/api/chat/stop", {"session_id": session_id})

    def chat(self, workspace_id, message, provider=None, session_id=None,
             task_id=None, task_context=None):
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

    def _dir(self, workspace_id):
        return os.path.join(self.root, workspace_id or "_global")

    def _path(self, workspace_id, task_id):
        return os.path.join(self._dir(workspace_id), task_id + ".json")

    def record_path(self, workspace_id, task_id):
        """Where a task's record lives - the page shows it."""
        return self._path(workspace_id, task_id)

    def create(self, task):
        with self.lock:
            os.makedirs(self._dir(task.get("workspace_id")), exist_ok=True)
            self._write(task)
        return task

    def load(self, workspace_id, task_id):
        with self.lock:
            try:
                with open(self._path(workspace_id, task_id), encoding="utf-8") as f:
                    return json.load(f)
            except (OSError, ValueError):
                pass
            if workspace_id:
                return None
            # No workspace given: look in every one of them. The task ids are
            # unique, so the first hit is the right one.
            for task in self.list():
                if task.get("id") == task_id:
                    return task
            return None

    def save(self, task):
        with self.lock:
            self._write(task)

    def _write(self, task):
        os.makedirs(self._dir(task.get("workspace_id")), exist_ok=True)
        path = self._path(task.get("workspace_id"), task.get("id"))
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

    def list(self, workspace_id=None, status=None):
        out = []
        with self.lock:
            roots = [self._dir(workspace_id)] if workspace_id else []
            if not roots:
                try:
                    roots = [os.path.join(self.root, d) for d in os.listdir(self.root)]
                except OSError:
                    roots = []
            for root in roots:
                if not os.path.isdir(root):
                    continue
                try:
                    names = os.listdir(root)
                except OSError:
                    continue
                for name in names:
                    if not name.endswith(".json"):
                        continue
                    task = self.load(os.path.basename(root), name[:-5])
                    if task is None:
                        continue
                    if status and task.get("status") != status:
                        continue
                    out.append(task)
        out.sort(key=lambda t: (t.get("created_at") or "", t.get("id") or ""))
        return out

    def delete(self, workspace_id, task_id):
        with self.lock:
            path = self._path(workspace_id, task_id)
            try:
                os.unlink(path)
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
    """The queue's own event stream: who wants to know, gets told."""

    def __init__(self):
        self.lock = threading.Lock()
        self.subscribers = []

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

    # ---- the quota gate ---------------------------------------------------
    def quota_left(self, provider):
        """(percent_left, window_name) - (None, None) when there is no quota."""
        try:
            quota = self.agent.usage(provider)
        except AgentError as exc:
            if exc.status == 400:
                log("provider %r does not report a quota (%s)" % (provider, exc.message))
            else:
                log("cannot read the quota of %r: %s" % (provider, exc))
            return None, None
        window = self.args.window
        block = quota.get(window) or quota.get("weekly") or {}
        value = block.get("remaining_percent")
        if value is None:
            return None, None
        try:
            return float(value), window
        except (TypeError, ValueError):
            return None, None

    # ---- one round --------------------------------------------------------
    def round(self):
        provider = self.args.provider
        left, window = self.quota_left(provider)
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

        queued = self.store.list(status=QUEUED)
        if not queued:
            log("nothing queued")
            return 0

        started = 0
        for task in queued:
            if started >= self.args.max_per_round:
                break
            wanted = task.get("provider")
            if wanted and wanted != provider:
                log("  task %s wants provider %r, not %r - leaving it alone"
                    % (task.get("id"), wanted, provider))
                continue
            if self.start_task(task, provider):
                started += 1
        return started

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
        self.store.update(workspace_id, task_id, lambda t: t.update({
            "status": RUNNING,
            "provider": provider,
            "started_at": now_iso(),
            "finished_at": "",
            "error": "",
            "needs_confirmation": False,
            "question": "",
            "note": "started",
        }))

        thread = threading.Thread(target=self._run, args=(workspace_id, task_id, provider, context),
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
        for event in self.agent.chat(workspace_id, message, provider=provider,
                                     session_id=session_id, task_id=task_id,
                                     task_context=context):
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
                self.events.publish({"type": "task_failed",
                                     "task": self.store.load(workspace_id, task_id)})
                return
            elif kind == "stopped":
                self.store.update(workspace_id, task_id, lambda t: t.update({
                    "status": CANCELLED, "finished_at": now_iso()}))
                self.events.publish({"type": "task_cancelled",
                                     "task": self.store.load(workspace_id, task_id)})
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
        if path.startswith("/api/tasks/"):
            return self._get(path.rsplit("/", 1)[-1])
        if path == "/api/events":
            return self._events()
        return self._error(404, "not_found", "no such endpoint: %s" % path)

    def do_POST(self):  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/tasks":
            return self._create()
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
        })

    def _list(self):
        workspace_id = self._one("workspace_id")
        status = self._one("status")
        tasks = self.app.store.list(workspace_id or None, status or None)
        self._json(200, {"tasks": [self.app.decorate(t) for t in tasks],
                         "counts": self.app.store.counts(workspace_id or None)})

    def _get(self, task_id):
        workspace_id = self._one("workspace_id")
        task = self.app.store.load(workspace_id, task_id)
        if task is None:
            return self._error(404, "not_found", "no task %r" % task_id)
        self._json(200, self.app.decorate(task))

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
        task = {
            "id": "t_" + os.urandom(6).hex(),
            "workspace_id": workspace_id,
            "prompt": prompt,
            "title": str(body.get("title") or "").strip(),
            "provider": str(body.get("provider") or "").strip(),
            "model": str(body.get("model") or "").strip(),
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
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "started_at": "",
            "finished_at": "",
        }
        self.app.store.create(task)
        self.app.events.publish({"type": "task_created", "task": task})
        self._json(200, task)

    def _delete(self, task_id):
        workspace_id = self._one("workspace_id")
        task = self.app.store.load(workspace_id, task_id)
        if task is None:
            return self._error(404, "not_found", "no task %r" % task_id)
        if task.get("status") == RUNNING:
            self.app.cancel_task(workspace_id, task_id)
        if not self.app.store.delete(workspace_id, task_id):
            return self._error(404, "not_found", "no task %r" % task_id)
        self.app.events.publish({"type": "task_deleted", "task": task})
        self._json(200, {"ok": True, "id": task_id})

    def _action(self, task_id, action):
        workspace_id = self._one("workspace_id")
        if not workspace_id:
            return self._error(400, "bad_request", "'workspace_id' is required")
        task = self.app.store.load(workspace_id, task_id)
        if task is None:
            return self._error(404, "not_found", "no task %r" % task_id)
        if action == "start":
            return self._start(workspace_id, task)
        if action == "cancel":
            return self._cancel(workspace_id, task)
        if action == "confirm":
            return self._confirm(workspace_id, task)
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
        self.app.cancel_task(workspace_id, task.get("id"))
        self._json(200, self.app.store.load(workspace_id, task.get("id")) or task)

    def _confirm(self, workspace_id, task):
        body = self._body()
        if body is None:
            return self._error(400, "bad_request", "the request body is not JSON")
        message = str(body.get("message") or "").strip()
        if not message:
            return self._error(400, "bad_request", "'message' is required")
        if not task.get("needs_confirmation"):
            return self._error(409, "not_waiting",
                               "task %s is not waiting for an answer" % task.get("id"))
        # The answer continues the work in the same session - the agent picks it
        # up as the next turn.
        self.app.store.update(workspace_id, task.get("id"), lambda t: t.update({
            "status": RUNNING,
            "needs_confirmation": False,
            "question": "",
            "note": "user confirmed: " + message,
        }))
        provider = task.get("provider") or self.app.args.provider
        context = self.app.engine.context_for(task)
        threading.Thread(target=self.app.engine._run,
                         args=(workspace_id, task.get("id"), provider, context, message),
                         daemon=True).start()
        self._json(200, self.app.store.load(workspace_id, task.get("id")) or task)

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
        self.store = Store(args.data_dir)
        self.agent = Agent(args.agent)
        self.events = Events()
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

    def decorate(self, task):
        """Add the bits a reader wants: where the record is, which workspace,
        and the agent to reach."""
        task = dict(task)
        task["record_path"] = self.store.record_path(task.get("workspace_id"), task.get("id"))
        task["agent_url"] = self.agent.base
        path = self.workspace_path(task.get("workspace_id"))
        if path:
            task["workspace_path"] = path
        return task

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

    def default_workspace(self):
        """The workspace a task goes to when the caller does not name one."""
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
    parser.add_argument("--data-dir", default=DEFAULT_DATA_DIR,
                        help="where the task records live (default: %s)" % DEFAULT_DATA_DIR)
    parser.add_argument("--workspace", default=os.environ.get("TEA_AGENT_WORKSPACE", ""),
                        help="the workspace whose queue is worked on (default: the first one)")
    parser.add_argument("--provider", default=DEFAULT_PROVIDER,
                        help="the provider whose quota gates the work (default: %s)" % DEFAULT_PROVIDER)
    parser.add_argument("--window", default="window", choices=["window", "weekly"],
                        help="which quota window to watch (default: the short one)")
    parser.add_argument("--min-remaining", type=float, default=5.0,
                        help="only work while at least this much of the window is left (%%)")
    parser.add_argument("--allow-without-quota", action="store_true",
                        help="work even when the provider does not report a quota")
    parser.add_argument("--interval", type=float, default=60.0,
                        help="seconds between rounds (default: 60)")
    parser.add_argument("--max-per-round", type=int, default=1,
                        help="how many queued tasks to start in one round (default: 1)")
    parser.add_argument("--submit", metavar="PROMPT",
                        help="put a task in the queue and exit (no server needed)")
    parser.add_argument("--title", default="", help="title for --submit")
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
