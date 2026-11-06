"""The client for the task queue (a2_task).

The queue answers in one shape - ``{code, message, request_id, data}`` - so
this class unwraps it: the caller gets the payload, and a non-zero code
becomes an ApiError. The project key (a bearer token) restricts the caller to
one project; without one the caller is the local operator.

Before this module there were two copies of this client (``tea_api.Queue``
and ``channel.Queue``); they had drifted in their argument names and in what
they could do. One class now, with both sets of names.
"""

import json
import urllib.error
import urllib.parse
import urllib.request

from .errors import ApiError

DEFAULT_QUEUE_BASE = "http://127.0.0.1:8283"


def _version_at_least(version, want):
    """Is this version at least that one? ("0.21.0" vs (0, 21, 0))"""
    try:
        parts = []
        for piece in str(version or "").split(".")[:3]:
            digits = ""
            for ch in piece:
                if not ch.isdigit():
                    break
                digits += ch
            parts.append(int(digits or 0))
        while len(parts) < 3:
            parts.append(0)
        return tuple(parts) >= tuple(want)
    except (TypeError, ValueError):
        return False


class Queue:
    """The task queue's API: the task records and the event stream."""

    def __init__(self, base=DEFAULT_QUEUE_BASE, timeout=60, key=""):
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.key = key or ""

    # ---- plumbing ---------------------------------------------------------
    def _request(self, method, path, body=None, timeout=None, headers=None):
        url = self.base + path
        data = None
        head = {}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            head["Content-Type"] = "application/json"
        if self.key:
            head["Authorization"] = "Bearer " + self.key
        head.update(headers or {})
        req = urllib.request.Request(url, data=data, headers=head, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            message = raw
            try:
                parsed = json.loads(raw)
                message = ((parsed.get("data") or {}).get("error") or {}).get("message") \
                    or parsed.get("message") or raw
            except ValueError:
                pass
            raise ApiError(exc.code, message or exc.reason, raw) from None
        except urllib.error.URLError as exc:
            raise ApiError(0, "cannot reach %s: %s" % (url, exc.reason)) from None
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except ValueError:
            raise ApiError(0, "the answer is not JSON: " + raw[:200]) from None
        # The envelope: the payload is in "data", and a non-zero code is a failure.
        if isinstance(parsed, dict) and "code" in parsed and "data" in parsed:
            if parsed.get("code"):
                detail = ((parsed.get("data") or {}).get("error") or {}).get("message") \
                    or parsed.get("message") or "failed"
                raise ApiError(parsed.get("code"), detail, raw)
            return parsed.get("data") or {}
        return parsed

    @staticmethod
    def _query(**params):
        clean = {k: v for k, v in params.items() if v not in (None, "")}
        return urllib.parse.urlencode(clean)

    # ---- the queue --------------------------------------------------------
    def _tasks_prefix(self):
        """The path of the task routes.

        "/v1/agent-tasks" when the queue knows it (0.21.0 and later),
        "/api/tasks" when it does not. A newer client has to work against an
        older queue, so the question is asked **once** and the answer is
        remembered.
        """
        prefix = getattr(self, "_prefix", "")
        if prefix:
            return prefix
        prefix = "/api/tasks"
        try:
            health = self.health()
            if _version_at_least(health.get("version"), (0, 21, 0)):
                prefix = "/v1/agent-tasks"
        except Exception:
            pass
        self._prefix = prefix
        return prefix

    def health(self):
        """The queue's own state and the agent's health behind it."""
        return self._request("GET", "/api/health", timeout=10)

    def _projects_prefix(self):
        """The path of the project routes (the version segment again)."""
        prefix = getattr(self, "_projects", "")
        if prefix:
            return prefix
        prefix = "/api/projects"
        try:
            health = self.health()
            if _version_at_least(health.get("version"), (0, 21, 0)):
                prefix = "/v1/projects"
        except Exception:
            pass
        self._projects = prefix
        return prefix

    def project_key(self, workspace_id, tenant_id="", identity=""):
        """The key of one project. Only the local operator may ask for one."""
        answer = self._request("POST", self._projects_prefix(), {
            "workspace_id": workspace_id,
            "tenant_id": tenant_id,
            "identity": identity,
        })
        key = str(answer.get("key") or "")
        if key:
            self.key = key
        return key

    def tasks(self, workspace_id=None, status=None, page=None, page_size=None):
        """The task list (and the counts per status)."""
        query = self._query(workspace_id=workspace_id, status=status,
                            page=page, page_size=page_size)
        return self._request("GET", self._tasks_prefix() + "?" + query)

    def list(self, workspace_id=None, status=None):
        """The task list (the name the chat front ends use)."""
        return self.tasks(workspace_id=workspace_id, status=status)

    def task(self, task_id, workspace_id=None, full=False):
        """One task. `full=True` brings the events and the history with it."""
        query = self._query(workspace_id=workspace_id,
                             include="full" if full else None)
        return self._request("GET", self._tasks_prefix() + "/%s?%s" % (urllib.parse.quote(task_id), query))

    def get(self, task_id, workspace_id=None):
        """One task (the name the chat front ends use)."""
        return self.task(task_id, workspace_id=workspace_id)

    def create_task(self, prompt, workspace_id=None, title=None, provider=None,
                    allow_paths=None, meta=None, priority=None, idempotency_key=None,
                    not_before=None, model=None, model_params=None, extra=None):
        """Put a task in the queue. It waits until the provider has quota.

        `not_before` is the "do it later" knob: "+5" (five hours from now),
        "23:30" (today at half past eleven), "+30m", "+2d" or an absolute
        "2026-10-24T09:00:00Z". The task waits until then - and it does not
        hold up the tasks behind it.
        """
        body = {"prompt": prompt}
        if workspace_id:
            body["workspace_id"] = workspace_id
        if title:
            body["title"] = title
        if provider:
            body["provider"] = provider
        if allow_paths:
            body["allow_paths"] = list(allow_paths)
        if meta:
            body["meta"] = meta
        if priority is not None:
            body["priority"] = priority
        if idempotency_key:
            body["idempotency_key"] = idempotency_key
        if not_before:
            body["not_before"] = not_before
        if model:
            body["model"] = model
        if model_params:
            body["model_params"] = dict(model_params)
        for key_name, value in (extra or {}).items():
            if value not in (None, "", [], {}):
                body[key_name] = value
        # The same key means the same task: a retried submission must not queue
        # the work twice.
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        return self._request("POST", self._tasks_prefix(), body, headers=headers)

    def create(self, prompt, workspace_id=None, title="", provider="", allow_paths=None,
               meta=None, key="", not_before="", model="", priority=0, extra=None,
               model_params=None):
        """Put a task in the queue (the name the chat front ends use)."""
        return self.create_task(prompt, workspace_id=workspace_id, title=title,
                                provider=provider, allow_paths=allow_paths, meta=meta,
                                priority=priority, idempotency_key=key,
                                not_before=not_before, model=model,
                                model_params=model_params, extra=extra)

    def trigger(self, source, text, workspace_id=None, dedup_key="", title="", meta=None,
                thread_key="", not_before=""):
        """One outside event through the trigger entry.

        The trigger entry decides whether it is work at all (the rules
        first, then the model) and only an accepted trigger becomes a task.
        The answer carries the trigger, so the caller can say what happened.
        """
        body = {"source": source, "text": text}
        if workspace_id:
            body["workspace_id"] = workspace_id
        if dedup_key:
            body["dedup_key"] = dedup_key
        if title:
            body["title"] = title
        if meta:
            body["meta"] = meta
        if thread_key:
            body["thread_key"] = thread_key
        if not_before:
            body["not_before"] = not_before
        return self._request("POST", "/api/triggers", body)

    def start_task(self, task_id, workspace_id=None, provider=None):
        """Start a queued task now (the quota gate still applies)."""
        query = self._query(workspace_id=workspace_id)
        body = {}
        if provider:
            body["provider"] = provider
        return self._request("POST", self._tasks_prefix() + "/%s/start?%s"
                             % (urllib.parse.quote(task_id), query), body)

    def cancel_task(self, task_id, workspace_id=None):
        query = self._query(workspace_id=workspace_id)
        return self._request("POST", self._tasks_prefix() + "/%s/cancel?%s"
                             % (urllib.parse.quote(task_id), query), {})

    def confirm_task(self, task_id, message, workspace_id=None):
        """Answer a task that waits for a human.

        The answer goes back into the queue: the task returns to the end of
        the line and carries the answer with it.
        """
        query = self._query(workspace_id=workspace_id)
        return self._request("POST", self._tasks_prefix() + "/%s/confirm?%s"
                             % (urllib.parse.quote(task_id), query), {"message": message})

    def retry_task(self, task_id, workspace_id=None):
        """Try a failed task again: the same task, one attempt further."""
        query = self._query(workspace_id=workspace_id)
        return self._request("POST", self._tasks_prefix() + "/%s/retry?%s"
                             % (urllib.parse.quote(task_id), query), {})

    def retry(self, task_id, workspace_id=None):
        """Try a failed task again (the name the chat front ends use)."""
        return self.retry_task(task_id, workspace_id=workspace_id)

    def delete_task(self, task_id, workspace_id=None):
        """Forget a task (a running one is stopped first)."""
        query = self._query(workspace_id=workspace_id)
        return self._request("DELETE", self._tasks_prefix() + "/%s?%s"
                             % (urllib.parse.quote(task_id), query))

    def delete(self, task_id, workspace_id=None):
        """Forget a task (the name the chat front ends use)."""
        return self.delete_task(task_id, workspace_id=workspace_id)

    def action(self, task_id, action, body=None, workspace_id=None):
        """One of the task's actions (retry, fail, confirm, ...)."""
        query = self._query(workspace_id=workspace_id)
        return self._request("POST", self._tasks_prefix() + "/%s/%s?%s"
                             % (urllib.parse.quote(task_id), action, query), body or {})

    def task_events(self, task_id, since_event_id=0, workspace_id=None):
        """What happened to one task - the log survives a restart."""
        query = self._query(workspace_id=workspace_id, since_event_id=since_event_id)
        return self._request("GET", self._tasks_prefix() + "/%s/events?%s"
                             % (urllib.parse.quote(task_id), query))

    def audit(self, task_id=None, limit=200):
        """Who did what, and when."""
        query = self._query(task_id=task_id, limit=limit)
        return self._request("GET", "/api/audit?" + query)

    def events(self, workspace_id=None, timeout=3600):
        """The lifecycle events, as they happen.

        Yields the parsed frames of GET /api/events. This is how a script
        *reacts* instead of polling: "tell the user when the task is done" is
        a subscription, not a loop with sleep(10).
        """
        query = self._query(workspace_id=workspace_id)
        url = self.base + "/api/events" + ("?" + query if query else "")
        head = {"Authorization": "Bearer " + self.key} if self.key else {}
        req = urllib.request.Request(url, headers=head, method="GET")
        try:
            resp = urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            raise ApiError(exc.code, raw, raw) from None
        with resp:
            for frame in _sse(resp):
                yield frame


def _sse(stream):
    """Parse a text/event-stream into the parsed frames."""
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
