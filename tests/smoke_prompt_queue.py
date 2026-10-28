#!/usr/bin/env python3
"""Smoke test for queued prompts ("消息插队").

While a turn is running the composer stays usable: Enter enqueues the text
instead of starting a second turn, and the agent loop adopts exactly one queued
prompt per iteration. Before adoption every prompt can be cancelled. Prompts that
are never adopted (the turn was stopped) are handed back on the SSE stream so the
UI can restore them instead of dropping the user's text.

The mock provider drives three scenarios:

  * tool-model: first request returns a slow tool call, the queued prompt must be
    adopted at the top of the next iteration and appear in that request;
  * slow-model: first request streams for ~2s and then finishes, the queued
    prompt must *extend* the turn (the loop continues instead of ending) and
    show up in the follow-up model request;
  * stop-model: a long stream used to test cancellation and the unconsumed
    hand-back.

Usage: python3 tests/smoke_prompt_queue.py [path-to-tea_agent-binary]
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


class MockProvider(BaseHTTPRequestHandler):
    records = []

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
        MockProvider.records.append(payload)
        model = payload.get("model")
        messages = payload.get("messages") or []
        tool_msgs = [m for m in messages if m.get("role") == "tool"]
        last_user = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                last_user = m.get("content") or ""
                break

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        if model == "tool-model":
            if not tool_msgs:
                # Long enough for the test to enqueue while the tool runs.
                self._tool_call("toolkit_exec", {"command": "sleep 1.5"}, "call_exec_1")
            else:
                self._content("tool path done")
            return
        if model == "slow-model":
            if "QUEUED-SLOW" in last_user:
                self._content("slow path adopted")
                return
            # ~2s of streamed output: the prompt is queued after the request
            # started but before the model finished, so the loop has to extend
            # the turn rather than end it.
            for _ in range(10):
                if not self._delta("字"):
                    return
                time.sleep(0.2)
            self._finish("stop")
            return
        if model == "stop-model":
            for _ in range(60):
                if not self._delta("y"):
                    return
                time.sleep(0.15)
            self._finish("stop")
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


def post_json(port, path, body, attempts=3):
    # free_port() has an unavoidable race: another process can grab the port in
    # the window between the probe and the spawn, and a leftover listener from an
    # earlier run would answer instead. A couple of retries turn that into a slow
    # test instead of a red one.
    for attempt in range(attempts):
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            conn.request(
                "POST", path, body=json.dumps(body), headers={"Content-Type": "application/json"}
            )
            resp = conn.getresponse()
            raw = resp.read()
            try:
                return resp.status, json.loads(raw or b"null")
            except ValueError:
                return resp.status, {"raw": raw.decode("utf-8", "replace")}
        except OSError:
            if attempt + 1 == attempts:
                raise
            time.sleep(0.5)


def get_json(port, path, attempts=3):
    for attempt in range(attempts):
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            conn.request("GET", path)
            resp = conn.getresponse()
            raw = resp.read()
            try:
                return resp.status, json.loads(raw or b"null")
            except ValueError:
                return resp.status, {"raw": raw.decode("utf-8", "replace")}
        except OSError:
            if attempt + 1 == attempts:
                raise
            time.sleep(0.5)


class RunningTurn:
    """POST /api/chat on a background thread and keep the SSE events."""

    def __init__(self, port, workspace_id, provider, message):
        self.port = port
        self.events = []
        self.session_id = None
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.args = (workspace_id, provider, message)
        self.thread.start()

    def _run(self):
        workspace_id, provider, message = self.args
        try:
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=90)
            conn.request(
                "POST",
                "/api/chat",
                body=json.dumps(
                    {"message": message, "workspace_id": workspace_id, "provider": provider}
                ),
                headers={"Content-Type": "application/json"},
            )
            resp = conn.getresponse()
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
                    event = json.loads(data)
                except ValueError:
                    continue
                if event.get("type") == "start" and event.get("session_id"):
                    self.session_id = event["session_id"]
                self.events.append(event)
        except OSError:
            pass

    def wait_start(self, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.session_id:
                return True
            time.sleep(0.02)
        return False

    def join(self, timeout=60.0):
        self.thread.join(timeout)
        return not self.thread.is_alive()

    def by_type(self, name):
        return [e for e in self.events if e.get("type") == name]


def main():
    binary = (
        sys.argv[1]
        if len(sys.argv) > 1
        else os.path.join(REPO_ROOT, "build", "bin", "tea_agent")
    )
    if not os.path.exists(binary):
        print("binary not found:", binary)
        return 1

    MockProvider.records = []
    mock = ThreadingHTTPServer(("127.0.0.1", 0), MockProvider)
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    mock_port = mock.server_address[1]

    tmp = tempfile.mkdtemp(prefix="tea_queue_")
    agent_port = free_port()
    ws_dir = os.path.join(tmp, "ws")
    os.makedirs(ws_dir)
    config_path = os.path.join(tmp, "provider.yaml")
    with open(config_path, "w", encoding="utf-8") as f:
        f.write(
            """version: 1
providers:
  mock_tool:
    api_url: http://127.0.0.1:%d
    api_key: sk-test
    default_model: tool-model
    models:
      tool-model:
        max_context_tokens: 32768
        max_output_tokens: 4096
        supports_tools: true
  mock_slow:
    api_url: http://127.0.0.1:%d
    api_key: sk-test
    default_model: slow-model
    models:
      slow-model:
        max_context_tokens: 32768
        max_output_tokens: 4096
  mock_stop:
    api_url: http://127.0.0.1:%d
    api_key: sk-test
    default_model: stop-model
    models:
      stop-model:
        max_context_tokens: 32768
        max_output_tokens: 4096
"""
            % (mock_port, mock_port, mock_port)
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

        # ---- 1) adopted at the next iteration after a tool call ------------
        turn = RunningTurn(agent_port, workspace_id, "mock_tool", "tool please")
        check("tool turn announces its session", turn.wait_start())
        time.sleep(0.4)  # the mock's sleep 1.5 keeps this turn busy
        prompt_ids = []
        for text in ("QUEUED-TOOL-1", "QUEUED-TOOL-2"):
            status, queued = post_json(
                agent_port,
                "/api/chat/queue",
                {"session_id": turn.session_id, "message": text},
            )
            check(
                "queue accepts " + text + " during a tool call",
                status == 200 and queued.get("ok"),
                queued,
            )
            prompt_ids.append((queued.get("queued") or {}).get("id"))

        status, listing = get_json(
            agent_port, "/api/chat/queue?session_id=" + turn.session_id
        )
        check(
            "GET queue lists both prompts in FIFO order",
            status == 200
            and listing.get("pending") == 2
            and [i.get("content") for i in listing.get("items", [])]
            == ["QUEUED-TOOL-1", "QUEUED-TOOL-2"],
            listing,
        )

        check("tool turn finished", turn.join())
        adopted = turn.by_type("queue_adopted")
        check(
            "one prompt is adopted per iteration, oldest first",
            [e.get("content") for e in adopted] == ["QUEUED-TOOL-1", "QUEUED-TOOL-2"]
            and [e.get("id") for e in adopted] == prompt_ids
            and [e.get("iteration") for e in adopted] == [1, 2],
            adopted,
        )
        check("tool turn ended with done", len(turn.by_type("done")) == 1, turn.events[-3:])
        session = get_json(agent_port, "/api/sessions/" + turn.session_id)[1]
        user_texts = [m.get("content") for m in session.get("messages", []) if m.get("role") == "user"]
        check(
            "adopted prompts are persisted as user messages",
            user_texts == ["tool please", "QUEUED-TOOL-1", "QUEUED-TOOL-2"],
            user_texts,
        )
        tool_model_requests = [r for r in MockProvider.records if r.get("model") == "tool-model"]
        check(
            "each adopted prompt got its own model request",
            len(tool_model_requests) == 3,
            len(tool_model_requests),
        )
        request_users = [
            [m.get("content") for m in (r.get("messages") or []) if m.get("role") == "user"]
            for r in tool_model_requests
        ]
        check(
            "model requests see the prompts appear one at a time",
            request_users
            == [
                ["tool please"],
                ["tool please", "QUEUED-TOOL-1"],
                ["tool please", "QUEUED-TOOL-1", "QUEUED-TOOL-2"],
            ],
            request_users,
        )

        status, listing = get_json(
            agent_port, "/api/chat/queue?session_id=" + turn.session_id
        )
        check("queue is empty after adoption", listing.get("pending") == 0, listing)

        # ---- 2) adopted even when the model already produced its answer ----
        turn = RunningTurn(agent_port, workspace_id, "mock_slow", "slow please")
        check("slow turn announces its session", turn.wait_start())
        time.sleep(0.6)  # the mock is still streaming its first answer
        status, queued = post_json(
            agent_port,
            "/api/chat/queue",
            {"session_id": turn.session_id, "message": "QUEUED-SLOW"},
        )
        check("queue accepts a prompt while the model streams", status == 200, queued)
        check("slow turn finished", turn.join())
        adopted = turn.by_type("queue_adopted")
        check(
            "a queued prompt extends the turn instead of ending it",
            len(adopted) == 1 and adopted[0].get("content") == "QUEUED-SLOW",
            adopted,
        )
        slow_requests = [r for r in MockProvider.records if r.get("model") == "slow-model"]
        check("the extended turn made a second model request", len(slow_requests) >= 2, len(slow_requests))
        if len(slow_requests) >= 2:
            follow_up = [
                m.get("content")
                for m in (slow_requests[-1].get("messages") or [])
                if m.get("role") == "user"
            ]
            check(
                "second model request contains the adopted prompt",
                follow_up == ["slow please", "QUEUED-SLOW"],
                follow_up,
            )

        # ---- 3) cancel before adoption ------------------------------------
        turn = RunningTurn(agent_port, workspace_id, "mock_stop", "stop please")
        check("stop turn announces its session", turn.wait_start())
        time.sleep(0.4)
        ids = []
        for text in ("CANCEL-A", "CANCEL-B"):
            status, queued = post_json(
                agent_port,
                "/api/chat/queue",
                {"session_id": turn.session_id, "message": text},
            )
            check("queue accepts " + text, status == 200, queued)
            ids.append((queued.get("queued") or {}).get("id"))
        status, listing = get_json(
            agent_port, "/api/chat/queue?session_id=" + turn.session_id
        )
        check("two prompts are pending", listing.get("pending") == 2, listing)

        status, cancelled = post_json(
            agent_port,
            "/api/chat/queue/cancel",
            {"session_id": turn.session_id, "id": ids[0]},
        )
        check(
            "cancel removes exactly one prompt",
            status == 200 and cancelled.get("cancelled") == 1 and cancelled.get("pending") == 1,
            cancelled,
        )
        status, listing = get_json(
            agent_port, "/api/chat/queue?session_id=" + turn.session_id
        )
        check(
            "the remaining prompt is the second one",
            listing.get("pending") == 1
            and (listing.get("items") or [{}])[0].get("content") == "CANCEL-B",
            listing,
        )
        status, cancelled = post_json(
            agent_port, "/api/chat/queue/cancel", {"session_id": turn.session_id}
        )
        check(
            "cancel-all empties the queue",
            status == 200 and cancelled.get("cancelled") == 1 and cancelled.get("pending") == 0,
            cancelled,
        )

        # ---- 4) a prompt left at turn end is handed back, never adopted ----
        status, queued = post_json(
            agent_port,
            "/api/chat/queue",
            {"session_id": turn.session_id, "message": "LEFTOVER"},
        )
        check("queue accepts LEFTOVER", status == 200, queued)
        status, stopped = post_json(
            agent_port, "/api/chat/stop", {"session_id": turn.session_id}
        )
        check("stop is accepted", status == 200 and stopped.get("cancelled"), stopped)
        check("stopped turn finished", turn.join())
        leftovers = turn.by_type("queue_unconsumed")
        check(
            "the unadopted prompt is handed back on the stream",
            len(leftovers) == 1
            and [i.get("content") for i in leftovers[0].get("items", [])] == ["LEFTOVER"],
            leftovers,
        )
        check("the stopped turn reports stopped", len(turn.by_type("stopped")) == 1, turn.events[-4:])
        session = get_json(agent_port, "/api/sessions/" + turn.session_id)[1]
        texts = [m.get("content") for m in session.get("messages", []) if m.get("role") == "user"]
        check(
            "cancelled and leftover prompts never enter the conversation",
            texts == ["stop please"],
            texts,
        )

        # ---- 5) refusal cases ---------------------------------------------
        status, err = post_json(
            agent_port,
            "/api/chat/queue",
            {"session_id": turn.session_id, "message": "too late"},
        )
        check(
            "queueing without a running turn is refused with 409",
            status == 409 and (err.get("error") or {}).get("code") == "no_active_turn",
            (status, err),
        )

        turn = RunningTurn(agent_port, workspace_id, "mock_stop", "again please")
        check("second stop turn announces its session", turn.wait_start())
        status, err = post_json(
            agent_port,
            "/api/chat/queue",
            {"session_id": turn.session_id, "message": "with image", "images": ["data:image/png;base64,AA=="]},
        )
        check(
            "queued prompts refuse images explicitly",
            status == 400 and (err.get("error") or {}).get("code") == "images_not_queued",
            (status, err),
        )
        status, err = post_json(
            agent_port, "/api/chat/queue", {"session_id": turn.session_id}
        )
        check("an empty queued prompt is refused", status == 400, (status, err))
        post_json(agent_port, "/api/chat/stop", {"session_id": turn.session_id})
        turn.join()
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
