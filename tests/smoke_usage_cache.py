#!/usr/bin/env python3
"""Token usage in streaming responses: asking for it, and surviving without it.

Two provider shapes, both OpenAI-compatible:

  * **MiniMax** (https://platform.minimax.cn/docs/api-reference/text-chat-openai):
    `stream_options.include_usage` defaults to **false** - "是否在流式响应中包含 token
    用量". Without the flag the stream carries `usage: null`, so the agent sees no
    token counts at all and the metrics strip falls back to local estimates. With
    the flag, the usage block carries `prompt_tokens_details.cached_tokens` (the
    prefix-cache hit count) - the OpenAI shape the agent already understands.
  * **A strict gateway** that does not know the field and answers 400. The agent
    has to drop the field, remember that for the provider, and still finish the
    turn - one wasted round trip, not a failed turn.

What this pins:
  * the request carries `stream_options.include_usage: true`;
  * the provider's usage arrives verbatim, including the cached-token count;
  * the metrics are marked as provider facts, not estimates;
  * a 400 about the field makes the agent retry without it and complete the turn;
  * the next turn for that provider does not send the field again.

Usage: python3 tests/smoke_usage_cache.py [path-to-tea_agent-binary]
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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHED_TOKENS = 42
PROMPT_TOKENS = 512

seen = {"mini": [], "strict": []}       # request bodies per provider
lock = threading.Lock()


class Mock(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            body = {}
        which = "strict" if "/strict/" in self.path else "mini"
        with lock:
            seen[which].append(body)

        if which == "strict" and "stream_options" in body:
            # A gateway that never heard of the field.
            payload = json.dumps({"error": {"message": "Unknown field: stream_options",
                                            "type": "invalid_request_error"}}).encode()
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        wants_usage = bool(body.get("stream_options", {}).get("include_usage"))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        def write(text):
            data = text.encode()
            self.wfile.write(("%x\r\n" % len(data)).encode() + data + b"\r\n")
            self.wfile.flush()

        def chunk(delta, finish=None):
            write("data: " + json.dumps({
                "id": "c1", "object": "chat.completion.chunk",
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }) + "\n\n")

        try:
            chunk({"content": "ok "})
            chunk({}, "stop")
            # MiniMax sends `usage: null` in the content chunks and puts the real
            # usage in the last one - but only when include_usage was requested.
            if wants_usage:
                write("data: " + json.dumps({
                    "id": "c1", "object": "chat.completion.chunk", "choices": [],
                    "usage": {"total_tokens": PROMPT_TOKENS + 2,
                              "prompt_tokens": PROMPT_TOKENS,
                              "completion_tokens": 2,
                              "prompt_tokens_details": {"cached_tokens": CACHED_TOKENS}},
                }) + "\n\n")
            else:
                write("data: " + json.dumps({
                    "id": "c1", "object": "chat.completion.chunk", "choices": [],
                    "usage": None,
                }) + "\n\n")
            write("data: [DONE]\n\n")
            write("")
        except OSError:
            pass


def free_port():
    while True:
        port = random.randint(20000, 60000)
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue


def call(port, method, path, body=None, timeout=30):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request(method, path, body=json.dumps(body) if body is not None else None,
                     headers={"Content-Type": "application/json"} if body is not None else {})
        resp = conn.getresponse()
        raw = resp.read()
        try:
            return resp.status, json.loads(raw or b"null")
        except ValueError:
            return resp.status, {"raw": raw.decode("utf-8", "replace")}
    finally:
        conn.close()


def turn(port, wid, provider, message):
    """One turn; returns the iteration_end usage/metrics and the event types."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    conn.request("POST", "/api/chat",
                 body=json.dumps({"workspace_id": wid, "provider": provider,
                                  "message": message}),
                 headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    usage = metrics = None
    types = []
    while True:
        line = resp.readline()
        if not line:
            break
        line = line.strip()
        if not line.startswith(b"data: "):
            continue
        payload = line[6:]
        if payload == b"[DONE]":
            break
        try:
            ev = json.loads(payload)
        except ValueError:
            continue
        types.append(ev.get("type"))
        if ev.get("type") == "iteration_end":
            usage = ev.get("usage")
            metrics = ev.get("metrics")
        if ev.get("type") == "error":
            print("   turn error:", ev.get("message"))
    conn.close()
    return usage, metrics, types


def main():
    binary = (sys.argv[1] if len(sys.argv) > 1
              else os.path.join(REPO_ROOT, "build", "bin", "tea_agent"))
    if not os.path.exists(binary):
        print("binary not found:", binary)
        return 1

    mock = ThreadingHTTPServer(("127.0.0.1", 0), Mock)
    mock.daemon_threads = True
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    mock_port = mock.server_address[1]

    tmp = tempfile.mkdtemp(prefix="tea_usage_")
    ws_dir = os.path.join(tmp, "ws")
    os.makedirs(ws_dir)
    config_path = os.path.join(tmp, "provider.yaml")
    with open(config_path, "w", encoding="utf-8") as f:
        f.write("""version: 1
providers:
  minilike:
    api_url: http://127.0.0.1:%d/mini/v1
    api_key: sk-test
    models:
      m: {max_context_tokens: 32768, max_output_tokens: 256}
  strict:
    api_url: http://127.0.0.1:%d/strict/v1
    api_key: sk-test
    models:
      m: {max_context_tokens: 32768, max_output_tokens: 256}
""" % (mock_port, mock_port))

    port = free_port()
    proc = subprocess.Popen([
        binary, "--config", config_path, "--tea-home", os.path.join(tmp, "home"),
        "--host", "127.0.0.1", "--port", str(port), "--no-console",
        "--log-file", os.path.join(tmp, "tea_agent.log"), "--workspace", ws_dir,
    ])

    failures = []
    checks = []

    def check(name, ok, detail=""):
        checks.append(name)
        print(("PASS " if ok else "FAIL ") + name + ((("  [" + str(detail) + "]")) if not ok else ""))
        if not ok:
            failures.append(name)

    try:
        for _ in range(100):
            try:
                if call(port, "GET", "/api/health", timeout=2)[0] == 200:
                    break
            except OSError:
                time.sleep(0.2)
        wid = call(port, "GET", "/api/workspaces")[1]["workspaces"][0]["id"]

        # ---- 1. a MiniMax-shaped provider gets the usage ------------------
        usage, metrics, types = turn(port, wid, "minilike", "hello")
        check("the turn finished", "done" in types, types)
        check("the request asked for usage in the stream",
              seen["mini"] and seen["mini"][0].get("stream_options", {}).get("include_usage") is True,
              seen["mini"][0] if seen["mini"] else "no request")
        check("the provider's usage arrives", bool(usage) and usage.get("prompt_tokens") == PROMPT_TOKENS,
              usage)
        check("the cached-token count is read from prompt_tokens_details",
              bool(usage) and usage.get("cached_tokens") == CACHED_TOKENS, usage)
        check("the metrics are provider facts, not estimates",
              bool(metrics) and metrics.get("estimated") is False, metrics)

        # ---- 2. a gateway that rejects the field --------------------------
        usage, metrics, types = turn(port, wid, "strict", "hello")
        check("the turn still finishes after the 400", "done" in types, types)
        check("the request was retried without stream_options",
              len(seen["strict"]) >= 2 and "stream_options" not in seen["strict"][-1],
              [sorted(b.keys()) for b in seen["strict"]])
        check("the retry produced a normal answer",
              bool(usage) and usage.get("prompt_tokens") in (0, PROMPT_TOKENS), usage)

        # ---- 3. and the field is not sent again ---------------------------
        before = len(seen["strict"])
        turn(port, wid, "strict", "again")
        check("the next turn does not send the rejected field again",
              len(seen["strict"]) == before + 1 and
              "stream_options" not in seen["strict"][-1],
              [sorted(b.keys()) for b in seen["strict"]])
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
