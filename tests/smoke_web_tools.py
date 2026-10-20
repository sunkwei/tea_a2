#!/usr/bin/env python3
"""Smoke test for the provider-scoped web tools (toolkit_search / toolkit_fetch).

Starts a mock OpenAI-compatible provider that also serves DeepSeek-style
/v1/tools/{search,fetch} endpoints, points a temporary tea_agent config at it,
drives one conversation through the server's /api/chat SSE API and asserts:

  * a provider with supports_search/supports_fetch gets the tools advertised
    (the mock records the `tools` array of every chat request) and the tools
    hit the right URLs with Bearer auth;
  * a provider without the flags never sees the tools advertised, and a forced
    call fails with a clear capability error instead of charging the provider;
  * search results are formatted (null fields handled) and fetch returns the
    Markdown body;
  * a stalled endpoint times out on the web-tool budget instead of holding the
    turn for the 600s LLM read timeout, and stop aborts a transfer in flight.

Usage: python3 tests/smoke_web_tools.py [path-to-tea_agent-binary]
"""

import http.client
import json
import os
import random
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SEARCH_RESPONSE = {
    "results": [
        {
            "rerank_score": 2.4,
            "title": "DeepSeek V4 model released",
            "url": "https://example.com/deepseek-v4",
            "snippet": "Short snippet.",
            "summary": "DeepSeek has released the V4 model with tool calling.",
            "content": "Full page text A",
            "site_name": "Example News",
            "site_icon": "https://example.com/favicon.ico",
            "date": "2026-06-01T00:00:00+00:00",
            "authority": 3,
        },
        {
            # Nulls everywhere: exercises the null-safe field accessors.
            "rerank_score": 1.7,
            "title": "Second hit",
            "url": "https://other.example.org/x",
            "snippet": "Snippet B.",
            "summary": "Summary B.",
            "content": "Full page text B",
            "site_name": None,
            "site_icon": None,
            "date": None,
            "authority": -1,
        },
    ]
}

# The live endpoint backslash-escapes all Markdown punctuation; the tool must
# undo that outside code fences while keeping escapes inside them.
FETCH_RESPONSE = {
    "title": "Example article",
    "content": (
        "\\# Example article\n\n"
        "Fetched body\\, with escaped punctuation\\.\n\n"
        "```\n"
        "regex \\. matches a dot\n"
        "```\n"
    ),
}


class MockProvider(BaseHTTPRequestHandler):
    # Shared recording, keyed by endpoint.
    records = {"search": [], "fetch": [], "chat": []}

    def log_message(self, *args):
        pass

    def _read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length) or b"{}")

    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path == "/stall/tools/search":
            # Silent endpoint: holds the connection without sending a byte.
            self._read_json()
            time.sleep(60)
            return
        if self.path == "/trickle/tools/search":
            # Sends a body slowly so a stop can be exercised mid-transfer.
            self._read_json()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "100000")
            self.end_headers()
            try:
                for _ in range(200):
                    self.wfile.write(b" ")
                    self.wfile.flush()
                    time.sleep(0.1)
            except OSError:
                pass
            return
        if self.path == "/v1/tools/search":
            payload = self._read_json()
            MockProvider.records["search"].append(
                {"payload": payload, "auth": self.headers.get("Authorization")}
            )
            self._send_json(SEARCH_RESPONSE)
            return
        if self.path == "/v1/tools/fetch":
            payload = self._read_json()
            MockProvider.records["fetch"].append(
                {"payload": payload, "auth": self.headers.get("Authorization")}
            )
            self._send_json(FETCH_RESPONSE)
            return
        if self.path == "/v1/chat/completions":
            payload = self._read_json()
            MockProvider.records["chat"].append(payload)
            self._send_chat(self._chat_body(payload))
            return
        self._send_json({"error": {"message": "unknown path " + self.path}}, 404)

    def _send_chat(self, body):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self.wfile.write(body.encode())

    @staticmethod
    def _chat_body(payload):
        """SSE body on purpose: exercises the streaming path end to end."""
        tool_msgs = [m for m in payload.get("messages", []) if m.get("role") == "tool"]

        def chunk(obj):
            return "data: %s\n\n" % json.dumps(obj)

        def tool_call_chunk(name, args, call_id):
            return chunk(
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

        def finish(finish_reason, content=None):
            text = ""
            if content is not None:
                text += chunk(
                    {
                        "id": "c1",
                        "choices": [
                            {"index": 0, "delta": {"content": content}, "finish_reason": None}
                        ],
                    }
                )
            text += chunk(
                {
                    "id": "c1",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
                }
            )
            return text + "data: [DONE]\n\n"

        if not tool_msgs:
            # The query doubles as the scenario selector for the mock.
            last_user = ""
            for m in reversed(payload.get("messages", [])):
                if m.get("role") == "user":
                    last_user = m.get("content") or ""
                    break
            query = "DeepSeek V4 release"
            args = {"query": query, "count": 5}
            if "stall" in last_user:
                query = "stall"
                args = {"query": query, "count": 5}
            elif "trickle" in last_user:
                query = "trickle"
                args = {"query": query, "count": 5}
            elif "localization_bad" in last_user:
                # Exactly what a model does when it reads the parameter as a
                # language filter: the provider would answer 400, so the tool must
                # refuse locally instead of spending a call.
                args = {"query": query, "count": 5, "localization": "en"}
            elif "localization_ip" in last_user:
                args = {"query": query, "count": 5, "localization": "203.0.113.5"}
            return tool_call_chunk("toolkit_search", args, "call_search") + finish("tool_calls")
        if len(tool_msgs) == 1:
            return tool_call_chunk(
                "toolkit_fetch", {"url": "https://example.com/doc"}, "call_fetch"
            ) + finish("tool_calls")
        return finish("stop", content="smoke test turn finished")


def free_port():
    while True:
        port = random.randint(20000, 60000)
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue


def wait_health(port, timeout=10.0):
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


def drive_turn(port, workspace_id, provider, message="smoke", stop_after=None):
    """POST /api/chat and collect the SSE events of the whole turn.

    With stop_after=N, POST /api/chat/stop N seconds into the stream, which is
    how the UI's stop button behaves.
    """
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    body = json.dumps(
        {"message": message, "workspace_id": workspace_id, "provider": provider}
    )
    conn.request("POST", "/api/chat", body=body, headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    state = {"session_id": None}
    if stop_after is not None:
        def stopper():
            deadline = time.time() + 10
            while state["session_id"] is None and time.time() < deadline:
                time.sleep(0.05)
            if state["session_id"] is None:
                return
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            c.request(
                "POST",
                "/api/chat/stop",
                body=json.dumps({"session_id": state["session_id"]}),
                headers={"Content-Type": "application/json"},
            )
            c.getresponse().read()

        threading.Timer(stop_after, stopper).start()
    events = []
    # readline(), not read(n): read(n) blocks until n bytes are buffered, which
    # would hold every event back until the turn ends.
    while True:
        line = resp.readline()
        if not line:
            break
        line = line.strip()
        if not line.startswith(b"data: "):
            continue
        data = line[len(b"data: ") :]
        if data == b"[DONE]":
            return events
        try:
            event = json.loads(data)
        except ValueError:
            continue
        if event.get("type") == "start" and event.get("session_id"):
            state["session_id"] = event["session_id"]
        events.append(event)
    return events


def main():
    binary = (
        sys.argv[1] if len(sys.argv) > 1 else os.path.join(REPO_ROOT, "build", "bin", "tea_agent")
    )
    if not os.path.exists(binary):
        print("binary not found:", binary)
        return 1

    mock = ThreadingHTTPServer(("127.0.0.1", 0), MockProvider)
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    mock_port = mock.server_address[1]

    tmp = tempfile.mkdtemp(prefix="tea_webtools_")
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
    default_model: mock-chat
    supports_search: true
    supports_fetch: true
    models:
      mock-chat:
        max_context_tokens: 32768
        max_output_tokens: 4096
        supports_tools: true
  mock_stall:
    api_url: http://127.0.0.1:%d
    api_key: sk-test
    default_model: mock-chat
    supports_search: true
    search_path: /stall/tools/search
    models:
      mock-chat:
        supports_tools: true
  mock_trickle:
    api_url: http://127.0.0.1:%d
    api_key: sk-test
    default_model: mock-chat
    supports_search: true
    search_path: /trickle/tools/search
    models:
      mock-chat:
        supports_tools: true
  mock_noflag:
    api_url: http://127.0.0.1:%d
    api_key: sk-test
    default_model: mock-chat
    models:
      mock-chat:
        max_context_tokens: 32768
        max_output_tokens: 4096
        supports_tools: true
  mock_modelflag:
    api_url: http://127.0.0.1:%d
    api_key: sk-test
    default_model: mock-chat
    models:
      mock-chat:
        max_context_tokens: 32768
        max_output_tokens: 4096
        supports_tools: true
        supports_search: true
        supports_fetch: true
settings:
  stream: true
  web_tool_timeout_ms: 3000
  web_tool_connect_timeout_ms: 2000
"""
            % (mock_port, mock_port, mock_port, mock_port, mock_port)
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
    try:
        if not wait_health(agent_port):
            print("server did not become healthy")
            return 1

        conn = http.client.HTTPConnection("127.0.0.1", agent_port, timeout=5)
        conn.request("GET", "/api/workspaces")
        workspaces = json.loads(conn.getresponse().read())["workspaces"]
        workspace_id = workspaces[0]["id"]

        # ---- turn 1: provider that declares the tools ---------------------
        events = drive_turn(agent_port, workspace_id, "mock")
        tool_results = {e["name"]: e for e in events if e.get("type") == "tool_result"}
        if "toolkit_search" not in tool_results or "toolkit_fetch" not in tool_results:
            failures.append("turn 1: expected both web tool calls, got %s" % list(tool_results))
        else:
            sr = tool_results["toolkit_search"]
            if not sr["ok"] or "2 result(s)" not in sr["output"] or "score: 2.40" not in sr["output"]:
                failures.append("turn 1: search output unexpected: %r" % sr["output"][:200])
            else:
                tail = sr["output"].split("Second hit", 1)[1]
                if "date:" in tail.split("summary:", 1)[0]:
                    failures.append("turn 1: null date should be omitted")
            fr = tool_results["toolkit_fetch"]
            if not fr["ok"] or "# Example article" not in fr["output"]:
                failures.append("turn 1: fetch output unexpected: %r" % fr["output"][:200])
            elif "Fetched body, with escaped punctuation." not in fr["output"]:
                failures.append("turn 1: markdown escapes not undone: %r" % fr["output"][:300])
            elif "regex \\. matches a dot" not in fr["output"]:
                failures.append("turn 1: escapes inside code fence were stripped: %r" % fr["output"][:300])

        # ---- turn 2: provider without the flags ---------------------------
        noflag_from = len(MockProvider.records["chat"])
        events = drive_turn(agent_port, workspace_id, "mock_noflag")
        noflag_to = len(MockProvider.records["chat"])
        tool_results = {e["name"]: e for e in events if e.get("type") == "tool_result"}
        for name in ("toolkit_search", "toolkit_fetch"):
            r = tool_results.get(name)
            if r is None or r["ok"] or "does not support" not in r["output"]:
                failures.append(
                    "turn 2: %s should fail the capability check, got %r"
                    % (name, (r or {}).get("output", "<missing>")[:160])
                )

        # ---- turn 3: a stalled endpoint must time out, not stall the turn --
        started = time.time()
        events = drive_turn(agent_port, workspace_id, "mock_stall", message="stall please")
        elapsed = time.time() - started
        tool_results = {e["name"]: e for e in events if e.get("type") == "tool_result"}
        r = tool_results.get("toolkit_search")
        if r is None or r["ok"]:
            failures.append("turn 3: stalled search should fail, got %r" % (r or {})[:1])
        elif "timed out" not in r["output"]:
            failures.append("turn 3: expected a timeout error, got %r" % r["output"][:160])
        if elapsed > 20:
            failures.append("turn 3: stalled endpoint held the turn for %.1fs" % elapsed)

        # ---- turn 4: stop must abort a slow transfer ----------------------
        started = time.time()
        events = drive_turn(
            agent_port, workspace_id, "mock_trickle", message="trickle please", stop_after=1.0
        )
        elapsed = time.time() - started
        tool_results = {e["name"]: e for e in events if e.get("type") == "tool_result"}
        r = tool_results.get("toolkit_search")
        if r is None or r["ok"] or "cancelled" not in r["output"]:
            failures.append("turn 4: stop did not cancel the transfer, got %r" % (r or {}))
        if elapsed > 10:
            failures.append("turn 4: stop took %.1fs to take effect" % elapsed)

        # ---- localization is geolocation, not a language ------------------
        # A model that fills in "en" here would make the provider answer 400, so
        # the tool refuses locally: no request may reach the search endpoint.
        before = len(MockProvider.records["search"])
        events = drive_turn(agent_port, workspace_id, "mock", message="localization_bad please")
        r = next((e for e in events if e.get("type") == "tool_result"
                  and e.get("name") == "toolkit_search"), None)
        if r is None or r["ok"]:
            failures.append("turn 5: a language tag was not refused, got %r" % (r or {}))
        elif "must be an IP address" not in r["output"] or "lang:zh" not in r["output"]:
            failures.append("turn 5: refusal does not explain the fix: %r" % r["output"][:200])
        if len(MockProvider.records["search"]) != before:
            failures.append("turn 5: a billed search request was sent anyway")

        events = drive_turn(agent_port, workspace_id, "mock", message="localization_ip please")
        r = next((e for e in events if e.get("type") == "tool_result"
                  and e.get("name") == "toolkit_search"), None)
        if r is None or not r["ok"]:
            failures.append("turn 6: a real IP literal was refused: %r" % (r or {}))
        else:
            sent = MockProvider.records["search"][-1]["payload"]
            if sent.get("localization") != "203.0.113.5":
                failures.append("turn 6: localization not forwarded: %r" % sent)

        # ---- flags declared on a model still enable the provider tools -----
        before = len(MockProvider.records["chat"])
        events = drive_turn(agent_port, workspace_id, "mock_modelflag", message="smoke")
        modelflag_chats = MockProvider.records["chat"][before:]
        if not modelflag_chats:
            failures.append("no chat requests recorded for provider 'mock_modelflag'")
        for chat in modelflag_chats:
            offered = {t["function"]["name"] for t in chat.get("tools", [])}
            for name in ("toolkit_search", "toolkit_fetch"):
                if name not in offered:
                    failures.append("model-level %s was not promoted to the provider" % name)
        log_text = open(os.path.join(tmp, "tea_agent.log"), encoding="utf-8",
                        errors="replace").read()
        if "declared on a model" not in log_text:
            failures.append("the promotion was not reported in the log")

        # ---- advertisement checks from the mock's point of view -----------
        chats = MockProvider.records["chat"]
        if not chats:
            failures.append("mock saw no chat requests")
        else:
            first_tools = {t["function"]["name"] for t in chats[0].get("tools", [])}
            for name in ("toolkit_search", "toolkit_fetch"):
                if name not in first_tools:
                    failures.append("provider 'mock' was not offered %s" % name)
            noflag_chats = chats[noflag_from:noflag_to]
            if not noflag_chats:
                failures.append("no chat requests recorded for provider 'mock_noflag'")
            for chat in noflag_chats:
                offered = {t["function"]["name"] for t in chat.get("tools", [])}
                for name in ("toolkit_search", "toolkit_fetch"):
                    if name in offered:
                        failures.append("provider 'mock_noflag' was wrongly offered %s" % name)

        # ---- the endpoints were hit the way the docs describe -------------
        if not MockProvider.records["search"]:
            failures.append("mock /v1/tools/search was never called")
        else:
            call = MockProvider.records["search"][0]
            if call["auth"] != "Bearer sk-test":
                failures.append("search auth header wrong: %r" % call["auth"])
            if call["payload"].get("query") != "DeepSeek V4 release":
                failures.append("search payload wrong: %r" % call["payload"])
        if not MockProvider.records["fetch"]:
            failures.append("mock /v1/tools/fetch was never called")
        else:
            call = MockProvider.records["fetch"][0]
            if call["payload"].get("url") != "https://example.com/doc":
                failures.append("fetch payload wrong: %r" % call["payload"])
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        mock.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)

    if failures:
        print("FAIL")
        for f in failures:
            print(" -", f)
        return 1
    print("PASS: toolkit_search/toolkit_fetch advertise, execute and refuse correctly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
