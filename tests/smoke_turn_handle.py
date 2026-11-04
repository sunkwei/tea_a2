#!/usr/bin/env python3
"""Smoke test for the turn handle ("回合句柄") and the asynchronous submit.

A caller that submits a turn asynchronously gets a **handle** back and walks
away: the turn keeps working, and the handle is how it finds out how it went.
That is the whole point of the mechanism layer for a scheduler (a2_task): the
queue must not have to hold an SSE connection open for the whole turn.

What is checked here:

  * POST /api/chat {"async": true} answers **202** with a handle, and the
    answer is JSON - not a stream;
  * the handle is the id the audit trail uses: GET /api/turns/<id>/trajectory
    finds exactly this turn;
  * GET /api/turns/<id> says "running" and then "done", with the result, the
    usage and the iterations - the answer to "how did it go?" without
    replaying the stream;
  * GET /api/turns/<id>/events replays the frames and ends;
  * POST /api/turns/<id>/stop stops exactly this turn;
  * a turn that hits a ceiling ends as "budget", not as a failure;
  * the old way still works: without "async" the route is an SSE stream;
  * a second async submit on a busy session queues the prompt and hands out
    the handle of the turn that is already running.

Usage: python3 tests/smoke_turn_handle.py [path-to-tea_agent-binary]
"""

import http.client
import json
import os
import random
import socket
import subprocess
import sys
import tempfile
import threading
import time
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

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "sched"))

TERMINAL = ("done", "failed", "stopped", "budget")


class MockProvider(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length) or b"{}")

    def _chunk(self, payload):
        try:
            self.wfile.write(("data: " + json.dumps(payload) + "\n\n").encode())
            self.wfile.flush()
            return True
        except OSError:
            return False

    def _delta(self, text):
        return self._chunk(
            {
                "id": "c1",
                "object": "chat.completion.chunk",
                "choices": [
                    {"index": 0, "delta": {"content": text}, "finish_reason": None}
                ],
            }
        )

    def _finish(self, reason):
        self._chunk(
            {
                "id": "c1",
                "object": "chat.completion.chunk",
                "choices": [{"index": 0, "delta": {}, "finish_reason": reason}],
            }
        )
        try:
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except OSError:
            pass

    def _tool_call(self, name, args, call_id):
        self._chunk(
            {
                "id": "c1",
                "object": "chat.completion.chunk",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": call_id,
                                    "type": "function",
                                    "function": {
                                        "name": name,
                                        "arguments": json.dumps(args),
                                    },
                                }
                            ],
                        },
                        "finish_reason": None,
                    }
                ],
            }
        )
        self._finish("tool_calls")

    def _content(self, text):
        self._delta(text)
        self._finish("stop")

    def do_POST(self):
        if self.path != "/v1/chat/completions":
            body = json.dumps({"error": {"message": "unknown path " + self.path}}).encode()
            self.send_response(404)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        payload = self._read_json()
        model = payload.get("model")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        if model == "quick-model":
            self._content("hello from the handle")
            return
        if model == "slow-model":
            # Long enough to be stopped in the middle.
            for _ in range(80):
                if not self._delta("y"):
                    return
                time.sleep(0.15)
            self._finish("stop")
            return
        if model == "tool-model":
            messages = payload.get("messages") or []
            if not any(m.get("role") == "tool" for m in messages):
                self._tool_call("toolkit_exec", {"command": "echo handle"}, "call_exec_1")
            else:
                self._content("tool path done")
            return
        self._content("unknown model")


def free_port():
    while True:
        port = random.randint(20000, 60000)
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue


def wait_health(port, timeout=30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            conn.request("GET", "/api/health")
            if conn.getresponse().status == 200:
                return True
        except OSError:
            pass
        time.sleep(0.2)
    return False


def request(port, method, path, body=None, timeout=20):
    """One request. Returns (status, headers, parsed-json-or-None)."""
    for attempt in range(3):
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
            headers = {}
            payload = None
            if body is not None:
                payload = json.dumps(body)
                headers["Content-Type"] = "application/json"
            conn.request(method, path, body=payload, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
            try:
                parsed = json.loads(raw or b"null")
            except ValueError:
                parsed = None
            return resp.status, dict(resp.getheaders()), parsed
        except OSError:
            if attempt == 2:
                raise
            time.sleep(0.5)
    return 0, {}, None


def get_json(port, path):
    status, _, parsed = request(port, "GET", path)
    return status, parsed


def post_json(port, path, body):
    status, _, parsed = request(port, "POST", path, body)
    return status, parsed


def read_events(port, path, timeout=30.0):
    """Read an SSE stream until the server ends it.

    Returns (status, content-type, events): a stream that never started is a
    failure the caller has to see, not an empty list of events.
    """
    events = []
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    conn.request("GET", path)
    resp = conn.getresponse()
    status = resp.status
    ctype = resp.getheader("Content-Type") or ""
    try:
        while True:
            line = resp.readline()
            if not line:
                break
            line = line.strip()
            if not line.startswith(b"data: "):
                continue
            data = line[len(b"data: "):]
            if data == b"[DONE]":
                break
            try:
                events.append(json.loads(data))
            except ValueError:
                continue
    except OSError:
        pass
    finally:
        conn.close()
    return status, ctype, events


def wait_state(port, turn_id, timeout=30.0):
    """Poll the handle until the turn reaches a final state."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        status, body = get_json(port, "/api/turns/" + turn_id)
        if status != 200 or not body:
            time.sleep(0.1)
            continue
        last = body
        if body.get("state") in TERMINAL:
            return body
        time.sleep(0.1)
    return last


def main():
    binary = (
        sys.argv[1]
        if len(sys.argv) > 1
        else os.path.join(REPO_ROOT, "build", "bin", "tea_agent")
    )
    if not os.path.exists(binary):
        print("binary not found:", binary)
        return 1

    mock = ThreadingHTTPServer(("127.0.0.1", 0), MockProvider)
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    mock_port = mock.server_address[1]

    tmp = tempfile.mkdtemp(prefix="tea_handle_")
    agent_port = free_port()
    ws_dir = os.path.join(tmp, "ws")
    os.makedirs(ws_dir)
    config_path = os.path.join(tmp, "provider.yaml")
    with open(config_path, "w", encoding="utf-8") as f:
        f.write(
            """version: 1
providers:
  mock:
    api_url: http://127.0.0.1:%d
    api_key: sk-test
    default_model: quick-model
    models:
      quick-model:
        max_context_tokens: 32768
        max_output_tokens: 4096
      slow-model:
        max_context_tokens: 32768
        max_output_tokens: 4096
      tool-model:
        max_context_tokens: 32768
        max_output_tokens: 4096
        supports_tools: true
"""
            % (mock_port,)
        )

    proc = subprocess.Popen(
        [
            binary,
            "--config",
            config_path,
            "--tea-home",
            os.path.join(tmp, "home"),
            "--host",
            "127.0.0.1",
            "--port",
            str(agent_port),
            "--no-console",
            "--log-file",
            os.path.join(tmp, "tea_agent.log"),
            "--workspace",
            ws_dir,
        ]
    )
    failures = []
    checks = []

    def check(name, ok, detail=""):
        checks.append(name)
        print(("PASS " if ok else "FAIL ") + name + (("  " + str(detail)) if not ok else ""))
        if not ok:
            failures.append(name)

    try:
        if not wait_health(agent_port):
            print("server did not become healthy")
            return 1
        _, listing = get_json(agent_port, "/api/workspaces")
        workspace_id = listing["workspaces"][0]["id"]

        # ---- 1) an async submit answers 202 with a handle -----------------
        status, headers, body = request(
            agent_port,
            "POST",
            "/api/chat",
            {
                "message": "handle please",
                "workspace_id": workspace_id,
                "provider": "mock",
                "model": "quick-model",
                "async": True,
            },
        )
        check("async submit answers 202", status == 202, (status, body))
        check(
            "the answer is JSON, not a stream",
            "application/json" in (headers.get("Content-Type") or ""),
            headers.get("Content-Type"),
        )
        check(
            "the answer carries a handle",
            bool(body) and body.get("ok") and body.get("turn_id"),
            body,
        )
        turn_id = body.get("turn_id") if body else ""
        session_id = body.get("session_id") if body else ""
        check(
            "the answer says where to look",
            body.get("status_url") == "/api/turns/" + turn_id
            and body.get("events_url") == "/api/turns/" + turn_id + "/events",
            body,
        )

        # ---- 2) the handle answers "how did it go?" -----------------------
        final = wait_state(agent_port, turn_id)
        check("the turn reaches a final state", final.get("state") == "done", final)
        check("the state is reported", final.get("state") == "done", final)
        check(
            "the result is the model's answer",
            "hello from the handle" in (final.get("result") or ""),
            final.get("result"),
        )
        check("the session is reported", final.get("session_id") == session_id, final)
        check("the usage is reported", isinstance(final.get("usage"), dict), final.get("usage"))
        check("the iterations are reported", final.get("iterations", 0) >= 1, final)
        check(
            "the handle is the audit id",
            final.get("trajectory_id") == turn_id,
            (final.get("trajectory_id"), turn_id),
        )

        # ---- 3) the audit trail is reachable through the handle ----------
        status, traj = get_json(agent_port, "/api/turns/" + turn_id + "/trajectory")
        check(
            "the trajectory of the turn is found by the handle",
            status == 200 and traj.get("turn_id") == turn_id,
            (status, traj),
        )
        check(
            "the trajectory remembers the session",
            traj.get("session_id") == session_id,
            traj,
        )

        # ---- 4) the events can be replayed --------------------------------
        status, ctype, events = read_events(
            agent_port, "/api/turns/" + turn_id + "/events?since=0"
        )
        types = [e.get("type") for e in events]
        check(
            "the event stream is a stream",
            status == 200 and "text/event-stream" in ctype,
            (status, ctype),
        )
        check("the event stream replays the frames", "done" in types, types)
        check("the frames carry their sequence number", all("seq" in e for e in events), events[:2])
        seqs = [e["seq"] for e in events]
        check("the sequence numbers grow", seqs == sorted(seqs), seqs)
        check(
            "the replayed stream ends with the answer",
            any(
                e.get("type") == "done" and "hello from the handle" in (e.get("content") or "")
                for e in events
            ),
            events[-3:],
        )

        # ---- 5) the recent handles are listed -----------------------------
        status, listing = get_json(agent_port, "/api/turns")
        ids = [t.get("turn_id") for t in (listing.get("turns") or [])]
        check("the handle shows up in the list", turn_id in ids, ids)
        check("the list is newest first", ids and ids[0] == turn_id, ids)

        # ---- 6) an unknown handle is a 404, a broken one a 400 -----------
        status, err = get_json(agent_port, "/api/turns/t_does_not_exist")
        check(
            "an unknown handle is refused",
            status == 404 and (err.get("error") or {}).get("code") == "unknown_turn",
            (status, err),
        )
        status, err = get_json(agent_port, "/api/turns/..%2F..%2Fetc")
        check("a handle that is not an id is refused", status in (400, 404), (status, err))

        # ---- 7) stopping exactly this turn --------------------------------
        status, _, body = request(
            agent_port,
            "POST",
            "/api/chat",
            {
                "message": "slow please",
                "workspace_id": workspace_id,
                "provider": "mock",
                "model": "slow-model",
                "async": True,
            },
        )
        check("the slow turn is submitted", status == 202, (status, body))
        slow_id = body.get("turn_id")
        time.sleep(0.5)
        status, stopped = post_json(agent_port, "/api/turns/" + slow_id + "/stop", {})
        check(
            "the stop is accepted",
            status == 200 and stopped.get("cancelled") is True,
            (status, stopped),
        )
        final = wait_state(agent_port, slow_id)
        check("the stopped turn ends as stopped", final.get("state") == "stopped", final)
        status, ctype, events = read_events(
            agent_port, "/api/turns/" + slow_id + "/events?since=0"
        )
        types = [e.get("type") for e in events]
        check(
            "the stream says the turn was stopped",
            status == 200 and "stopped" in types,
            (status, ctype, types[-3:]),
        )

        # ---- 8) a turn that hits a ceiling is not a failure ---------------
        status, _, body = request(
            agent_port,
            "POST",
            "/api/chat",
            {
                "message": "tool please",
                "workspace_id": workspace_id,
                "provider": "mock",
                "model": "tool-model",
                "async": True,
                "limits": {"max_iterations": 1},
            },
        )
        check("the budgeted turn is submitted", status == 202, (status, body))
        budget_id = body.get("turn_id")
        final = wait_state(agent_port, budget_id)
        check("a turn that ran out of room ends as budget", final.get("state") == "budget", final)
        check(
            "the budget says which ceiling it was",
            (final.get("stop_reason") == "budget")
            or (final.get("budget_used") or {}).get("limit") == "iterations",
            final,
        )

        # ---- 9) the old way still works ----------------------------------
        status, headers, _ = request(
            agent_port,
            "POST",
            "/api/chat",
            {
                "message": "stream please",
                "workspace_id": workspace_id,
                "provider": "mock",
                "model": "quick-model",
            },
        )
        check(
            "without 'async' the route is still a stream",
            "text/event-stream" in (headers.get("Content-Type") or ""),
            headers.get("Content-Type"),
        )

        # ---- 10) an async submit on a busy session queues the prompt ------
        status, _, body = request(
            agent_port,
            "POST",
            "/api/chat",
            {
                "message": "slow please",
                "workspace_id": workspace_id,
                "provider": "mock",
                "model": "slow-model",
                "async": True,
            },
        )
        busy_id = body.get("turn_id")
        busy_session = body.get("session_id")
        time.sleep(0.4)
        status, _, queued = request(
            agent_port,
            "POST",
            "/api/chat",
            {
                "message": "second question",
                "workspace_id": workspace_id,
                "provider": "mock",
                "model": "quick-model",
                "session_id": busy_session,
                "async": True,
            },
        )
        check(
            "a second async submit on a busy session is queued",
            status == 202 and queued.get("queued") and queued.get("turn_running"),
            (status, queued),
        )
        check(
            "the queued submit hands out the handle of the running turn",
            queued.get("turn_id") == busy_id,
            (queued.get("turn_id"), busy_id),
        )
        post_json(agent_port, "/api/turns/" + busy_id + "/stop", {})
        wait_state(agent_port, busy_id)
        # ---- 11) the client knows the handle too -------------------------
        from a2_ext_py.client import Client

        client = Client(base="http://127.0.0.1:%d" % agent_port, timeout=20)
        handle = client.submit(workspace_id, "client please", provider="mock",
                               model="quick-model")
        check(
            "the client submits and gets a handle",
            bool(handle.get("turn_id")) and handle.get("state") == "running",
            handle,
        )
        client_id = handle.get("turn_id")
        status = client.turn_status(client_id)
        check("the client reads the status", status.get("turn_id") == client_id, status)
        deadline = time.time() + 20
        while time.time() < deadline:
            status = client.turn_status(client_id)
            if status.get("state") in TERMINAL:
                break
            time.sleep(0.1)
        check(
            "the client waits for the turn to finish",
            status.get("state") == "done"
            and "hello from the handle" in (status.get("result") or ""),
            status,
        )
        frames = list(client.turn_events(client_id))
        check(
            "the client can follow the frames of a finished turn",
            any(f.get("type") == "done" for f in frames),
            [f.get("type") for f in frames],
        )
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        mock.shutdown()

    print()
    print("%d/%d checks passed" % (len(checks) - len(failures), len(checks)))
    if failures:
        print("FAILED:", ", ".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
