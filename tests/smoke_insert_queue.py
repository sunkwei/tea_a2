#!/usr/bin/env python3
"""Smoke test for the two-stage interjection ("两段式插话").

A message typed while a turn is still running does not start a second turn. It
goes into the **session's** queue, and it takes effect in one of two ways:

  * "after" (the default): the running turn is not disturbed at all. When it
    ends, the message starts a turn of its own (the hand-over: the old stream
    says `turn_handover`, the new turn starts with `queue_id`).
  * "now": the running turn adopts the message before its next model request,
    so the tool calls the model decides next already know about it.

The mock provider drives the scenarios:

  * tool-model: the first request returns a slow tool call, so the test has
    time to queue a message while the turn is busy;
  * slow-model: a long stream, used to test that stopping a turn hands the
    queued messages back instead of running them.

Usage: python3 tests/smoke_insert_queue.py [path-to-tea_agent-binary]
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
                                    "function": {"name": name, "arguments": json.dumps(args)},
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
                # Long enough for the test to queue a message while the tool runs.
                self._tool_call("toolkit_exec", {"command": "sleep 1.5"}, "call_exec_1")
            else:
                self._content("tool path done")
            return
        if model == "slow-model":
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

    def __init__(self, port, workspace_id, provider, message, session_id=None):
        self.port = port
        self.events = []
        self.session_id = session_id
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.args = (workspace_id, provider, message)
        self.thread.start()

    def _run(self):
        workspace_id, provider, message = self.args
        body = {"message": message, "workspace_id": workspace_id, "provider": provider}
        if self.session_id:
            body["session_id"] = self.session_id
        try:
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=90)
            conn.request(
                "POST",
                "/api/chat",
                body=json.dumps(body),
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

    def wait_type(self, name, timeout=30.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            found = self.by_type(name)
            if found:
                return found[0]
            time.sleep(0.05)
        return None


def user_texts(record):
    """The user messages of one recorded model request."""
    out = []
    for m in record.get("messages") or []:
        if m.get("role") == "user":
            content = m.get("content")
            if isinstance(content, str):
                out.append(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        out.append(part.get("text") or "")
    return out


def wait_for_request_with(needle, timeout=30.0, since=0):
    """Wait until a model request carries `needle`; return its index."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for i in range(since, len(MockProvider.records)):
            if any(needle in t for t in user_texts(MockProvider.records[i])):
                return i
        time.sleep(0.1)
    return -1


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

    tmp = tempfile.mkdtemp(prefix="tea_insert_")
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
"""
            % (mock_port, mock_port)
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

        # ---- 1) the default: the message waits for the turn to end --------
        turn = RunningTurn(agent_port, workspace_id, "mock_tool", "first question")
        check("the first turn announces its session", turn.wait_start())
        time.sleep(0.4)  # the mock's sleep 1.5 keeps this turn busy
        status, queued = post_json(
            agent_port,
            "/api/chat/queue",
            {"session_id": turn.session_id, "message": "AFTER-MESSAGE"},
        )
        check(
            "the queue accepts the message while the turn runs",
            status == 200 and queued.get("ok"),
            queued,
        )
        check(
            "the queued message waits for the turn to end (insert=after)",
            (queued.get("queued") or {}).get("insert") == "after",
            queued,
        )
        status, listing = get_json(agent_port, "/api/chat/queue?session_id=" + turn.session_id)
        check(
            "the queue endpoint lists the message with its timing",
            status == 200
            and listing.get("pending") == 1
            and (listing.get("items") or [{}])[0].get("insert") == "after",
            listing,
        )

        handover = turn.wait_type("turn_handover", timeout=40)
        check("the turn hands the session over to the next turn", handover is not None,
              [e.get("type") for e in turn.events][-6:])
        check(
            "the hand-over names the queued message",
            bool(handover) and handover.get("queue_id") == (queued.get("queued") or {}).get("id"),
            handover,
        )
        check(
            "the running turn did not adopt the message",
            not any(e.get("type") == "queue_adopted" for e in turn.events),
            [e.get("type") for e in turn.events],
        )
        turn.join(40)

        index = wait_for_request_with("AFTER-MESSAGE", timeout=30)
        check("the next turn asks the model about the queued message", index >= 0,
              [user_texts(r) for r in MockProvider.records])
        first_turn_requests = [r for r in MockProvider.records[:index]]
        check(
            "the first turn never saw the queued message",
            all(
                not any("AFTER-MESSAGE" in t for t in user_texts(r))
                for r in first_turn_requests
            ),
            [user_texts(r) for r in first_turn_requests],
        )
        # The message is in the session: it is a turn of its own now.
        status, session = get_json(agent_port, "/api/sessions/" + turn.session_id)
        texts = [m.get("content") for m in (session.get("messages") or [])
                 if m.get("role") == "user"]
        check("the session holds the queued message", "AFTER-MESSAGE" in texts, texts)

        # ---- 2) "insert": "now" is adopted by the running turn ------------
        MockProvider.records = []
        turn2 = RunningTurn(agent_port, workspace_id, "mock_tool", "second question")
        check("the second turn announces its session", turn2.wait_start())
        time.sleep(0.4)
        status, queued2 = post_json(
            agent_port,
            "/api/chat/queue",
            {"session_id": turn2.session_id, "message": "NOW-MESSAGE", "insert": "now"},
        )
        check(
            "the queue accepts an immediate message",
            status == 200 and (queued2.get("queued") or {}).get("insert") == "now",
            queued2,
        )
        adopted = turn2.wait_type("queue_adopted", timeout=30)
        check("the running turn adopts the immediate message", adopted is not None,
              [e.get("type") for e in turn2.events][-6:])
        check(
            "the adopted message is the one that was queued",
            bool(adopted) and adopted.get("content") == "NOW-MESSAGE",
            adopted,
        )
        index = wait_for_request_with("NOW-MESSAGE", timeout=20)
        check("the model sees the immediate message in the next request", index >= 0,
              [user_texts(r) for r in MockProvider.records])
        turn2.join(40)

        # ---- 3) cancelling a queued message -------------------------------
        turn3 = RunningTurn(agent_port, workspace_id, "mock_tool", "third question")
        check("the third turn announces its session", turn3.wait_start())
        time.sleep(0.4)
        status, queued3 = post_json(
            agent_port,
            "/api/chat/queue",
            {"session_id": turn3.session_id, "message": "CANCELLED-MESSAGE"},
        )
        queue_id = (queued3.get("queued") or {}).get("id")
        status, cancelled = post_json(
            agent_port,
            "/api/chat/queue/cancel",
            {"session_id": turn3.session_id, "id": queue_id},
        )
        check(
            "a queued message can be cancelled",
            status == 200 and cancelled.get("cancelled") == 1,
            cancelled,
        )
        turn3.join(40)
        index = wait_for_request_with("CANCELLED-MESSAGE", timeout=3)
        check("the cancelled message never runs", index < 0)

        # ---- 4) stopping the turn hands the messages back ------------------
        turn4 = RunningTurn(agent_port, workspace_id, "mock_slow", "slow question")
        check("the slow turn announces its session", turn4.wait_start())
        time.sleep(0.5)
        status, queued4 = post_json(
            agent_port,
            "/api/chat/queue",
            {"session_id": turn4.session_id, "message": "STOPPED-MESSAGE"},
        )
        check("the message is queued while the slow turn runs", status == 200, queued4)
        post_json(agent_port, "/api/chat/stop", {"session_id": turn4.session_id})
        handed_back = turn4.wait_type("queue_unconsumed", timeout=30)
        check("stopping the turn hands the queued message back", handed_back is not None,
              [e.get("type") for e in turn4.events][-6:])
        check(
            "the handed back message is the one that was queued",
            bool(handed_back)
            and any(i.get("content") == "STOPPED-MESSAGE" for i in (handed_back.get("items") or [])),
            handed_back,
        )
        check(
            "the stopped turn did not hand the session over",
            not turn4.by_type("turn_handover"),
            [e.get("type") for e in turn4.events],
        )
        turn4.join(20)

        # ---- 5) the queue route starts a turn when none is running ---------
        status, answer = post_json(
            agent_port,
            "/api/chat/queue",
            {"session_id": turn.session_id, "message": "NO-TURN-MESSAGE"},
        )
        check(
            "queueing without a running turn starts one",
            status == 200 and answer.get("turn_running") is True and answer.get("turn_id"),
            (status, answer),
        )
        index = wait_for_request_with("NO-TURN-MESSAGE", timeout=20)
        check("the message that started the turn reaches the model", index >= 0,
              [user_texts(r) for r in MockProvider.records])

        # ---- 6) a misspelled timing is an error ---------------------------
        turn5 = RunningTurn(agent_port, workspace_id, "mock_tool", "fifth question")
        turn5.wait_start()
        time.sleep(0.3)
        status, body = post_json(
            agent_port,
            "/api/chat/queue",
            {"session_id": turn5.session_id, "message": "BAD-MODE", "insert": "soon"},
        )
        check("a misspelled timing is refused", status == 400, (status, body))
        turn5.join(40)

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    print("\n%d checks, %d failed" % (len(checks), len(failures)))
    if failures:
        for name in failures:
            print("  FAILED:", name)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
