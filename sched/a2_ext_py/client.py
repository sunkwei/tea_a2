"""The HTTP client for a2 - the kernel's whole API, in one place.

Before this module there were three copies of the same client
(``tea_api.Tea``, ``channel.Agent``, ``task_server.Agent``). They had drifted:
different error types, different argument names, one of them missing the
knobs the others had. Every interface change meant three edits, and a missed
edit is a silent semantic difference - exactly what a mechanism layer must
not have.

One client, one error type, the union of what the three did.
"""

import json
import socket
import urllib.error
import urllib.parse
import urllib.request

from .errors import ApiError

DEFAULT_BASE = "http://127.0.0.1:8282"
DEFAULT_AGENT = DEFAULT_BASE


class Client:
    """a2's HTTP API: the workspaces, the model calls, the turns."""

    def __init__(self, base=DEFAULT_BASE, timeout=120, chat_timeout=3600):
        self.base = base.rstrip("/")
        self.timeout = timeout
        # A turn may take hours; but not for ever: without a timeout a turn
        # whose answer never comes holds its slot (and the project) for good.
        self.chat_timeout = chat_timeout

    # ---- plumbing ---------------------------------------------------------
    def _request(self, method, path, body=None, timeout=None, headers=None):
        url = self.base + path
        data = None
        head = {}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            head["Content-Type"] = "application/json"
        head.update(headers or {})
        req = urllib.request.Request(url, data=data, headers=head, method=method)
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
            raise ApiError(exc.code, message or str(exc.reason), raw) from None
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

    # ---- the machine -------------------------------------------------------
    def capabilities(self):
        """What this agent can do: the features a caller may rely on.

        The features are the contract between the layers: a caller reads them
        once and picks its way (no `async_turns` means the old way of holding
        a stream open). An agent that is too old has no such endpoint - the
        caller falls back to its version then.
        """
        return self._request("GET", "/api/capabilities")

    def error_catalog(self):
        """Every error the API can answer with, and what it means.

        A list of {code, http, retryable, retry_after, meaning, hint}: a
        caller looks a code up instead of parsing an English sentence to find
        out whether it may try again.
        """
        return self._request("GET", "/api/errors").get("errors") or []

    def health(self):
        """Is the agent up, and what is it configured with.

        The answer carries ``limits`` - the budget of one turn. A caller that
        hands work over (the task queue) reads it and stays inside it.
        """
        return self._request("GET", "/api/health")

    def status(self):
        """The machine's snapshot (the fleet channel's view of it)."""
        return self._request("GET", "/api/status")

    def providers(self):
        """The provider catalogue: names, endpoints, models, capabilities."""
        return self._request("GET", "/api/providers")

    def provider_models(self, provider):
        """The models a provider offers right now."""
        return self._request("GET", "/api/providers/"
                             + urllib.parse.quote(provider) + "/models")

    def usage(self, provider):
        """How much of the provider's subscription is left.

        Raises ApiError(400) when the provider does not report a quota at all.
        """
        return self._request("GET", "/api/providers/" + urllib.parse.quote(provider) + "/usage")

    def limits(self):
        """The budget of one turn (the ceilings this machine imposes).

        Empty when the agent is down or too old to say.
        """
        try:
            body = self._request("GET", "/api/health", timeout=10)
        except ApiError:
            return {}
        limits = body.get("limits")
        return dict(limits) if isinstance(limits, dict) else {}

    def primary_model(self, provider):
        """The model the work of this provider will use (its first model)."""
        try:
            body = self.provider_models(provider)
        except ApiError:
            return ""
        if not isinstance(body, dict):
            return ""
        for one in body.get("current") or []:
            if isinstance(one, dict) and one.get("id"):
                return one["id"]
        for one in body.get("models") or []:
            if isinstance(one, dict) and one.get("id"):
                return one["id"]
        return ""

    # ---- the workspaces ----------------------------------------------------
    def workspaces(self):
        return self._request("GET", "/api/workspaces").get("workspaces") or []

    def register_workspace(self, path, name="", create=False):
        """Add a workspace to the agent's index (it is the agent's registry)."""
        body = {"path": path, "name": name}
        if create:
            body["create"] = True
        return self._request("POST", "/api/workspaces", body)

    def session(self, session_id):
        """The conversation behind a task (its messages, artifacts, todos)."""
        if not session_id:
            return {}
        try:
            return self._request("GET", "/api/sessions/%s" % urllib.parse.quote(session_id))
        except ApiError:
            return {}

    def download(self, workspace_id, path):
        """One file the work produced. Returns the bytes."""
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

    # ---- the work ----------------------------------------------------------
    def llm_chat(self, prompt=None, messages=None, system=None, provider=None, model=None,
                 tools=None, temperature=None, top_p=None, top_k=None,
                 frequency_penalty=None, presence_penalty=None, seed=None,
                 reasoning_effort=None, max_tokens=None, stream=False, timeout=None):
        """One model call. No agent loop, no tool is executed.

        With `tools` you hand over OpenAI-style tool schemas; the answer
        carries the tool calls the model asked for and the caller decides
        what to do with them. That is the difference to /api/chat, which runs
        the loop itself.

        The knobs say how the call is made: which model, how hard it should
        think, how random it should be. A translation plugin, say, wants a
        cheap model that does not think at all:
        `llm_chat(prompt=..., model="cheap", reasoning_effort="off",
        temperature=0.2)`.

        `reasoning_effort` is "auto" (do not send the field at all - the
        provider decides), "off" (no thinking), or one of "minimal" / "low" /
        "medium" / "high" / "xhigh" / "max". A knob left at None is not sent
        either: the device's own configuration decides, and a gateway that
        does not know a field answers 400 when it sees it.

        The answer carries a "knobs" object: what the provider was actually
        told, so a caller can check that its request had an effect.
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
        if top_p is not None:
            body["top_p"] = top_p
        if top_k is not None:
            body["top_k"] = top_k
        if frequency_penalty is not None:
            body["frequency_penalty"] = frequency_penalty
        if presence_penalty is not None:
            body["presence_penalty"] = presence_penalty
        if seed is not None:
            body["seed"] = seed
        if reasoning_effort is not None:
            body["reasoning_effort"] = reasoning_effort
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        body["stream"] = bool(stream)
        if stream:
            return self.llm_chat_stream(body)
        return self._request("POST", "/api/llm/chat", body, timeout=timeout)

    def llm_chat_stream(self, body):
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
            for frame in self.sse(resp):
                yield frame

    def _chat_body(self, workspace_id, message, provider=None, session_id=None,
                   task_id=None, task_context=None, allow_paths=None, model=None,
                   params=None, limits=None, policy=None, tools=None,
                   system_prompt=None, idempotency_key=None):
        """The body of a turn. Shared by `chat()` and `submit()`.

        One place builds it, so the two ways of starting a turn cannot drift
        apart (the knobs of a turn are the knobs of a turn).
        """
        body = {"workspace_id": workspace_id, "message": message}
        if provider:
            body["provider"] = provider
        if model:
            # The model of this turn. The agent falls back to the provider's
            # first model when the name is empty or unknown.
            body["model"] = model
        if params:
            # The model knobs of this turn (temperature, max_tokens, ...):
            # they go on the wire for this turn and for this turn only.
            for key, value in params.items():
                if value is not None:
                    body[key] = value
        if system_prompt:
            body["system_prompt"] = system_prompt
        if session_id:
            body["session_id"] = session_id
        if task_id:
            body["task_id"] = task_id
        if task_context:
            body["task_context"] = task_context
        if allow_paths:
            body["allow_paths"] = list(allow_paths)
        if limits:
            body["limits"] = dict(limits)
        allowed = list(tools or []) or list((policy or {}).get("tools") or [])
        if allowed:
            body["tools"] = allowed
        if idempotency_key:
            # The key says "this is one request, however often it is sent".
            # It goes in the header (the usual place) and in the body (the
            # task queue's convention) - the agent takes either.
            body["idempotency_key"] = str(idempotency_key)
        return body

    def chat(self, workspace_id, message, provider=None, session_id=None,
             task_id=None, task_context=None, allow_paths=None, model=None,
             params=None, limits=None, policy=None, tools=None, system_prompt=None,
             idempotency_key=None):
        """Run one turn and yield the agent's events as they happen.

        This is the whole contract between a caller that keeps its own policy
        and the kernel: the agent announces the session, reports on the task
        (toolkit_task) and finishes (done / error / stopped). The caller reads
        the progress out of the stream - it never has to ask where it is.

        `limits` is the budget of this turn (the machine's ceiling, tightened
        by the caller's own - see `GET /api/health`'s `limits`); `policy` and
        `tools` are the permissions (which tools the model may use).

        This holds one connection open for the whole turn. A caller that may
        be interrupted (a queue, a supervisor, a phone) wants `submit()`
        instead: it gets a handle and comes back later.
        """
        body = self._chat_body(
            workspace_id, message, provider=provider, session_id=session_id,
            task_id=task_id, task_context=task_context, allow_paths=allow_paths,
            model=model, params=params, limits=limits, policy=policy, tools=tools,
            system_prompt=system_prompt, idempotency_key=idempotency_key)
        headers = {"Content-Type": "application/json"}
        if idempotency_key:
            headers["Idempotency-Key"] = str(idempotency_key)
        req = urllib.request.Request(
            self.base + "/api/chat", data=json.dumps(body).encode("utf-8"),
            headers=headers, method="POST")
        try:
            resp = urllib.request.urlopen(req, timeout=self.chat_timeout or None)
        except (socket.timeout, TimeoutError):
            raise ApiError(0, "the agent did not answer within %d seconds"
                           % self.chat_timeout) from None
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            message = raw
            try:
                message = (json.loads(raw).get("error") or {}).get("message") or raw
            except ValueError:
                pass
            raise ApiError(exc.code, message, raw) from None
        except urllib.error.URLError as exc:
            raise ApiError(0, "cannot reach %s: %s" % (self.base, exc.reason)) from None
        with resp:
            for frame in self.sse(resp):
                yield frame

    def stop(self, session_id):
        """Stop the turn that is running in this session."""
        return self._request("POST", "/api/chat/stop", {"session_id": session_id})

    # ---- the turn handle: submit now, look it up later -------------------
    #
    # A turn that is started with `chat()` lives as long as the connection:
    # if the caller goes away, the work goes on but nobody can ask how it
    # went. `submit()` hands back a **handle** instead, and the handle is
    # what the caller keeps.

    def submit(self, workspace_id, message, provider=None, session_id=None,
               task_id=None, task_context=None, allow_paths=None, model=None,
               params=None, limits=None, policy=None, tools=None,
               system_prompt=None, idempotency_key=None):
        """Submit a turn and walk away. Returns the handle.

        The answer is {"ok": true, "turn_id": "t_…", "session_id": "s_…",
        "state": "running", "status_url": …, "events_url": …}. The turn keeps
        working with nobody watching; the caller comes back to
        `turn_status(turn_id)`, `turn_events(turn_id)` or `stop_turn(turn_id)`.
        """
        body = self._chat_body(
            workspace_id, message, provider=provider, session_id=session_id,
            task_id=task_id, task_context=task_context, allow_paths=allow_paths,
            model=model, params=params, limits=limits, policy=policy, tools=tools,
            system_prompt=system_prompt, idempotency_key=idempotency_key)
        body["async"] = True
        headers = {}
        if idempotency_key:
            headers["Idempotency-Key"] = str(idempotency_key)
        return self._request("POST", "/api/chat", body, headers=headers)

    def turn_status(self, turn_id):
        """How did the turn with this handle go?

        {"state": "running|done|failed|stopped|budget", "result": …,
        "usage": {…}, "iterations": n, "stop_reason": …, "elapsed_ms": n, …}
        """
        return self._request("GET", "/api/turns/" + self._turn_path(turn_id))

    def turn_events(self, turn_id, since=0):
        """The frames of one turn: the replay first, then the live stream.

        Same frames as `chat()` yields, so a caller can switch from one to
        the other without changing how it reads the events.
        """
        path = "/api/turns/" + self._turn_path(turn_id) + "/events"
        if since:
            path += "?since=" + str(int(since))
        req = urllib.request.Request(self.base + path,
                                     headers={"Accept": "text/event-stream"})
        try:
            resp = urllib.request.urlopen(req, timeout=self.chat_timeout or None)
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            message = raw
            try:
                message = (json.loads(raw).get("error") or {}).get("message") or raw
            except ValueError:
                pass
            raise ApiError(exc.code, message, raw) from None
        except urllib.error.URLError as exc:
            raise ApiError(0, "cannot reach %s: %s" % (self.base, exc.reason)) from None
        with resp:
            for frame in self.sse(resp):
                yield frame

    def stop_turn(self, turn_id):
        """Stop exactly this turn (by its handle, not by the session)."""
        return self._request("POST", "/api/turns/" + self._turn_path(turn_id) + "/stop", {})

    @staticmethod
    def _turn_path(turn_id):
        """A handle as a path segment. It is an id, not a path."""
        return urllib.parse.quote(str(turn_id), safe="")

    def fleet_ask(self, fleet_url, token, prompt, groups=None, names=None, labels=None,
                  timeout_s=300, timeout=360):
        """Ask the devices' own agents (the fleet service - another process).

        The multi-level path: the server does not have to know how a device is
        put right, the device does. The caller only collects the answers.
        """
        base = (fleet_url or "").rstrip("/")
        if not base:
            raise ApiError(0, "no fleet service is configured")
        body = {"prompt": prompt, "timeout_s": int(timeout_s or 300)}
        if groups:
            body["groups"] = list(groups)
        if names:
            body["names"] = list(names)
        if labels:
            body["labels"] = dict(labels)
        req = urllib.request.Request(
            base + "/api/fleet/ask", data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        if token:
            req.add_header("Authorization", "Bearer " + token)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            message = raw
            try:
                message = (json.loads(raw).get("message") or raw)
            except ValueError:
                pass
            raise ApiError(exc.code, message[:300], raw) from None
        except urllib.error.URLError as exc:
            raise ApiError(0, "cannot reach the fleet service %s: %s" % (base, exc.reason)) from None
        try:
            data = json.loads(raw)
        except ValueError:
            raise ApiError(0, "the fleet service did not answer with JSON") from None
        if isinstance(data, dict) and "data" in data and "code" in data:
            if data.get("code"):
                raise ApiError(0, str(data.get("message") or "the fleet service refused")[:300], raw)
            data = data.get("data") or {}
        return data

    # ---- the stream --------------------------------------------------------
    @staticmethod
    def sse(stream):
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
