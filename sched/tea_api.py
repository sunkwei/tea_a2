#!/usr/bin/env python3
"""A client for tea_agent's primitive API.

The point of the primitive layer is that a *policy* - when to work, what to work
on, when to stop, who to tell - lives in a script, not in the server. This module
is the vocabulary those scripts speak: one model call, the task records, the
provider catalogue, the quota, and the event stream.

  from tea_api import Tea
  tea = Tea("http://127.0.0.1:8282")

  quota = tea.usage("minimax")            # how much is left
  task = tea.create_task(ws, "do the thing")
  tea.start_task(ws, task["id"])
  for event in tea.events(workspace_id=ws):
      if event["type"] == "task_done":
          notify(event["task"])

Standard library only: these scripts run on the device next to the agent.
"""

import json
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_BASE = "http://127.0.0.1:8282"


class ApiError(Exception):
    """The server answered, but not with what we asked for."""

    def __init__(self, status, message, body=""):
        super().__init__("%s: %s" % (status, message))
        self.status = status
        self.message = message
        self.body = body


class Tea:
    """The primitive API of one tea_agent."""

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

    def _sse(self, stream):
        """Parse a text/event-stream into (event, data) dictionaries."""
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

    def events(self, workspace_id=None):
        """The lifecycle events, as they happen.

        Yields the parsed frames of GET /api/events. This is how a script
        *reacts* instead of polling: "tell the user when the task is done" is a
        subscription, not a loop with sleep(10).
        """
        query = self._query(workspace_id=workspace_id)
        url = self.base + "/api/events" + ("?" + query if query else "")
        req = urllib.request.Request(url, method="GET")
        try:
            resp = urllib.request.urlopen(req, timeout=None)
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            raise ApiError(exc.code, raw, raw) from None
        with resp:
            for frame in self._sse(resp):
                yield frame

    # ---- tasks ------------------------------------------------------------
    def tasks(self, workspace_id, status=None):
        """The task list (and the counts per status)."""
        query = self._query(workspace_id=workspace_id, status=status)
        return self._request("GET", "/api/tasks?" + query)

    def task(self, workspace_id, task_id):
        query = self._query(workspace_id=workspace_id)
        return self._request("GET", "/api/tasks/%s?%s" % (task_id, query))

    def create_task(self, workspace_id, prompt, title=None, provider=None, session_id=None,
                    meta=None):
        body = {"workspace_id": workspace_id, "prompt": prompt}
        if title:
            body["title"] = title
        if provider:
            body["provider"] = provider
        if session_id:
            body["session_id"] = session_id
        if meta:
            body["meta"] = meta
        return self._request("POST", "/api/tasks", body)

    def start_task(self, workspace_id, task_id, provider=None):
        """Start a queued task now."""
        query = self._query(workspace_id=workspace_id)
        body = {}
        if provider:
            body["provider"] = provider
        return self._request("POST", "/api/tasks/%s/start?%s" % (task_id, query), body)

    def cancel_task(self, workspace_id, task_id):
        query = self._query(workspace_id=workspace_id)
        return self._request("POST", "/api/tasks/%s/cancel?%s" % (task_id, query), {})

    def confirm_task(self, workspace_id, task_id, message):
        """Answer a task that waits for a human."""
        query = self._query(workspace_id=workspace_id)
        return self._request("POST", "/api/tasks/%s/confirm?%s" % (task_id, query),
                             {"message": message})

    def patch_task(self, workspace_id, task_id, **fields):
        """Write any field of a task record - the escape hatch for a script that
        keeps its own state machine on top of the same records."""
        query = self._query(workspace_id=workspace_id)
        return self._request("PATCH", "/api/tasks/%s?%s" % (task_id, query), fields)
