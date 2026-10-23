#!/usr/bin/env python3
"""Clients for tea_agent's primitive API and for the task queue.

The point of the primitive layer is that a *policy* - when to work, what to work
on, when to stop, who to tell - lives in a script, not in the server. This module
is the vocabulary those scripts speak: one model call, the task records, the
provider catalogue, the quota, and the event stream.

There are **two servers** behind that vocabulary, and they are not the same
thing:

  tea_agent (C++)      the workspace channel: the model call, the providers,
                        the quota, the workspaces.  -> class `Tea`
  task_server (Python) the queue: the task records, the events. -> class `Queue`

The task routes used to live on the agent; they moved to the Python side with
the queue, so a script that wants the tasks talks to `Queue`, not to `Tea`.

  from tea_api import Tea, Queue

  tea = Tea("http://127.0.0.1:8282")
  quota = tea.usage("minimax")            # how much is left

  queue = Queue("http://127.0.0.1:8283")
  task = queue.create_task(ws, "do the thing")
  queue.start_task(ws, task["id"])
  for event in queue.events():
      if event["type"] == "task_done":
          notify(event["task"])

Standard library only: these scripts run on the device next to the agent.
"""

import json
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_BASE = "http://127.0.0.1:8282"
DEFAULT_QUEUE_BASE = "http://127.0.0.1:8283"


class ApiError(Exception):
    """The server answered, but not with what we asked for."""

    def __init__(self, status, message, body=""):
        super().__init__("%s: %s" % (status, message))
        self.status = status
        self.message = message
        self.body = body


class Tea:
    """The primitive API of one tea_agent: the model call, the providers, the quota."""

    def __init__(self, base=DEFAULT_BASE, timeout=120):
        self.base = base.rstrip("/")
        self.timeout = timeout

    # ---- plumbing ---------------------------------------------------------
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

    @staticmethod
    def _query(**params):
        clean = {k: v for k, v in params.items() if v not in (None, "")}
        return urllib.parse.urlencode(clean)

    # ---- the primitives ---------------------------------------------------
    def health(self):
        """Is the agent up, and what is it configured with."""
        return self._request("GET", "/api/health")

    def providers(self):
        """The provider catalogue: names, endpoints, models, capabilities."""
        return self._request("GET", "/api/providers")

    def usage(self, provider):
        """How much of the provider's subscription is left.

        Raises ApiError(400) when the provider does not report a quota at all.
        """
        return self._request("GET", "/api/providers/" + urllib.parse.quote(provider) + "/usage")

    def workspaces(self):
        return self._request("GET", "/api/workspaces").get("workspaces") or []

    def llm_chat(self, prompt=None, messages=None, system=None, provider=None, model=None,
                 tools=None, temperature=None, max_tokens=None, stream=False):
        """One model call. No agent loop, no tool is executed.

        With `tools` you hand over OpenAI-style tool schemas; the answer carries
        the tool calls the model asked for and the caller decides what to do with
        them. That is the difference to /api/chat, which runs the loop itself.
        """
        body = {}
        if prompt is not None:
            body["prompt"] = prompt
        if messages is not None:
            body["messages"] = messages
        if system is not None:
            body["system"] = system
        if provider is not None:
            body["provider"] = provider
        if model is not None:
            body["model"] = model
        if tools is not None:
            body["tools"] = tools
        if temperature is not None:
            body["temperature"] = temperature
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        body["stream"] = bool(stream)
        if stream:
            return self._llm_chat_stream(body)
        return self._request("POST", "/api/llm/chat", body)

    def _llm_chat_stream(self, body):
        """The same call, streamed: yields the parsed SSE frames."""
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            self.base + "/api/llm/chat", data=data,
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            resp = urllib.request.urlopen(req, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            raise ApiError(exc.code, raw, raw) from None
        with resp:
            for frame in self._sse(resp):
                yield frame

    @staticmethod
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


class Queue:
    """The task queue's API: the task records and the event stream.

    The queue answers in one shape - ``{code, message, request_id, data}`` - so
    this class unwraps it: the caller gets the payload, and a non-zero code
    becomes an ApiError. The project key (a bearer token) restricts the caller
    to one project; without one the caller is the local operator.
    """

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
    def health(self):
        """The queue's own state and the agent's health behind it."""
        return self._request("GET", "/api/health", timeout=10)

    def project_key(self, workspace_id):
        """The key of one project. Only the local operator may ask for one."""
        answer = self._request("POST", "/api/projects", {"workspace_id": workspace_id})
        key = str(answer.get("key") or "")
        if key:
            self.key = key
        return key

    def tasks(self, workspace_id=None, status=None, page=None, page_size=None):
        """The task list (and the counts per status)."""
        query = self._query(workspace_id=workspace_id, status=status,
                            page=page, page_size=page_size)
        return self._request("GET", "/api/tasks?" + query)

    def task(self, task_id, workspace_id=None, full=False):
        """One task. `full=True` brings the events and the history with it."""
        query = self._query(workspace_id=workspace_id,
                            include="full" if full else None)
        return self._request("GET", "/api/tasks/%s?%s" % (urllib.parse.quote(task_id), query))

    def create_task(self, prompt, workspace_id=None, title=None, provider=None,
                    allow_paths=None, meta=None, priority=None, idempotency_key=None):
        """Put a task in the queue. It waits until the provider has quota."""
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
        # The same key means the same task: a retried submission must not queue
        # the work twice.
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        return self._request("POST", "/api/tasks", body, headers=headers)

    def start_task(self, task_id, workspace_id=None, provider=None):
        """Start a queued task now (the quota gate still applies)."""
        query = self._query(workspace_id=workspace_id)
        body = {}
        if provider:
            body["provider"] = provider
        return self._request("POST", "/api/tasks/%s/start?%s"
                             % (urllib.parse.quote(task_id), query), body)

    def cancel_task(self, task_id, workspace_id=None):
        query = self._query(workspace_id=workspace_id)
        return self._request("POST", "/api/tasks/%s/cancel?%s"
                             % (urllib.parse.quote(task_id), query), {})

    def confirm_task(self, task_id, message, workspace_id=None):
        """Answer a task that waits for a human.

        The answer goes back into the queue: the task returns to the end of the
        line and carries the answer with it.
        """
        query = self._query(workspace_id=workspace_id)
        return self._request("POST", "/api/tasks/%s/confirm?%s"
                             % (urllib.parse.quote(task_id), query), {"message": message})

    def retry_task(self, task_id, workspace_id=None):
        """Try a failed task again: the same task, one attempt further."""
        query = self._query(workspace_id=workspace_id)
        return self._request("POST", "/api/tasks/%s/retry?%s"
                             % (urllib.parse.quote(task_id), query), {})

    def delete_task(self, task_id, workspace_id=None):
        """Forget a task (a running one is stopped first)."""
        query = self._query(workspace_id=workspace_id)
        return self._request("DELETE", "/api/tasks/%s?%s"
                             % (urllib.parse.quote(task_id), query))

    def task_events(self, task_id, since_event_id=0, workspace_id=None):
        """What happened to one task - the log survives a restart."""
        query = self._query(workspace_id=workspace_id, since_event_id=since_event_id)
        return self._request("GET", "/api/tasks/%s/events?%s"
                             % (urllib.parse.quote(task_id), query))

    def audit(self, task_id=None, limit=200):
        """Who did what, and when."""
        query = self._query(task_id=task_id, limit=limit)
        return self._request("GET", "/api/audit?" + query)

    def events(self, workspace_id=None, timeout=3600):
        """The lifecycle events, as they happen.

        Yields the parsed frames of GET /api/events. This is how a script
        *reacts* instead of polling: "tell the user when the task is done" is a
        subscription, not a loop with sleep(10).
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
            for frame in Tea._sse(resp):
                yield frame
