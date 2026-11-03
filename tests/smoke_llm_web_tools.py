#!/usr/bin/env python3
"""The web tools from /api/llm/chat and /api/tools/call.

DeepSeek's search/fetch live at the provider's own endpoints (/v1/tools/search
and /v1/tools/fetch). A caller of /api/llm/chat could not reach them: the
route executes no tool at all. Now:

  * POST /api/tools/call calls one web tool - no model involved;
  * POST /api/llm/chat with "web_tools": true lets the model search, and the
    server runs the search and asks again.

What is pinned here:

  * /api/tools/call reaches the provider's search endpoint and returns its
    answer;
  * /api/tools/call refuses anything that is not a web tool;
  * /api/llm/chat with web_tools: true advertises the search tool, runs the
    call the model asked for and returns the final answer;
  * the trail says what the web tool was asked and what it answered;
  * without web_tools the route still executes nothing.

Usage: python3 tests/smoke_llm_web_tools.py [path-to-tea_agent-binary]
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

# What the provider was asked for, and what it answered.
SEEN = []
SEARCH_HITS = []


class MockProvider(BaseHTTPRequestHandler):
    """A chat endpoint plus a DeepSeek-style search endpoint."""

    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            body = {}
        SEEN.append((self.path, body))

        if self.path.endswith("/v1/tools/search"):
            query = body.get("query") or ""
            SEARCH_HITS.append(query)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "results": [{"title": "搜索结果", "url": "https://example.com/a",
                             "summary": "这是 %s 的摘要。" % query}]
            }).encode())
            return

        # The chat endpoint: the first turn asks for a search, the second
        # answers.
        messages = body.get("messages") or []
        already = any(m.get("role") == "tool" for m in messages)
        usage = {"prompt_tokens": 5, "completion_tokens": 4, "total_tokens": 9}

        if body.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            if already:
                # The tool result came back: the model answers.
                self._sse_chunk({"choices": [{"index": 0, "delta": {
                    "content": "我搜到了，这是答案。"}, "finish_reason": "stop"}],
                    "usage": usage})
            else:
                # The first turn: the model asks for a search.
                self._sse_chunk({"choices": [{"index": 0, "delta": {
                    "tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                                    "function": {"name": "toolkit_search",
                                                 "arguments": json.dumps({"query": "tea_a2"})}}]},
                    "finish_reason": None}]})
                self._sse_chunk({"choices": [{"index": 0, "delta": {},
                                              "finish_reason": "tool_calls"}]})
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            return

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        if already:
            out = {"id": "c", "object": "chat.completion",
                   "choices": [{"index": 0, "message": {
                       "role": "assistant", "content": "我搜到了，这是答案。"},
                       "finish_reason": "stop"}],
                   "usage": usage}
        else:
            out = {"id": "c", "object": "chat.completion",
                   "choices": [{"index": 0, "message": {
                       "role": "assistant", "content": None,
                       "tool_calls": [{"id": "call_1", "type": "function",
                                       "function": {"name": "toolkit_search",
                                                    "arguments": json.dumps({"query": "tea_a2"})}}]},
                       "finish_reason": "tool_calls"}],
                   "usage": usage}
        self.wfile.write(json.dumps(out).encode())

    def _sse_chunk(self, payload):
        frame = "data: " + json.dumps(dict({"id": "c", "object": "chat.completion.chunk"},
                                           **payload)) + "\n\n"
        self.wfile.write(frame.encode())
        self.wfile.flush()


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


def call(port, method, path, body=None, timeout=60):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    headers = {"Content-Type": "application/json"} if body is not None else {}
    conn.request(method, path, body=json.dumps(body) if body is not None else None,
                 headers=headers)
    resp = conn.getresponse()
    raw = resp.read()
    try:
        return resp.status, json.loads(raw or b"null")
    except ValueError:
        return resp.status, {"raw": raw.decode("utf-8", "replace")}


def main():
    binary = (sys.argv[1] if len(sys.argv) > 1
              else os.path.join(REPO_ROOT, "build", "bin", "tea_agent"))
    if not os.path.exists(binary):
        print("binary not found:", binary)
        return 1

    del SEEN[:]
    del SEARCH_HITS[:]
    mock = ThreadingHTTPServer(("127.0.0.1", 0), MockProvider)
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    mock_port = mock.server_address[1]

    tmp = tempfile.mkdtemp(prefix="tea_webtools_")
    port = free_port()
    ws_dir = os.path.join(tmp, "ws")
    os.makedirs(ws_dir)
    config = os.path.join(tmp, "provider.yaml")
    with open(config, "w", encoding="utf-8") as f:
        f.write("""version: 1
providers:
  mock:
    api_url: http://127.0.0.1:%d
    api_key: sk-test
    default_model: m
    supports_search: true
    models:
      m:
        max_context_tokens: 32768
        supports_tools: true
""" % mock_port)

    proc = subprocess.Popen([
        binary, "--config", config, "--tea-home", os.path.join(tmp, "home"),
        "--host", "127.0.0.1", "--port", str(port), "--no-console",
        "--log-file", os.path.join(tmp, "tea.log"), "--workspace", ws_dir,
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    failures = []
    checks = []

    def check(name, ok, detail=""):
        checks.append(name)
        print(("PASS " if ok else "FAIL ") + name + (("  " + str(detail)) if not ok else ""))
        if not ok:
            failures.append(name)

    try:
        if not wait_health(port):
            print("server did not become healthy")
            return 1

        # ---- 1) one web tool, no model ------------------------------------
        status, resp = call(port, "POST", "/api/tools/call", {
            "tool": "toolkit_search", "arguments": {"query": "tea_a2"}})
        check("the web tool call answers", status == 200, (status, resp))
        check("the provider's search endpoint was called",
              any(p.endswith("/v1/tools/search") for p, _ in SEEN), [p for p, _ in SEEN])
        check("the query reached the provider", "tea_a2" in SEARCH_HITS, SEARCH_HITS)
        check("the answer comes back", "example.com" in json.dumps(resp, ensure_ascii=False),
              resp)

        status, resp = call(port, "POST", "/api/tools/call", {
            "tool": "toolkit_exec", "arguments": {"command": "rm -rf /"}})
        check("a tool that is not a web tool is refused", status == 400, (status, resp))

        # ---- 2) the model may search ---------------------------------------
        del SEEN[:]
        del SEARCH_HITS[:]
        status, resp = call(port, "POST", "/api/llm/chat", {
            "prompt": "搜一下 tea_a2", "web_tools": True})
        check("the call answers", status == 200, (status, resp))
        check("the final answer is the model's",
              "我搜到了" in json.dumps(resp, ensure_ascii=False), resp)
        check("the search really happened", "tea_a2" in SEARCH_HITS, SEARCH_HITS)
        trail = resp.get("web_tool_calls") or []
        check("the trail says what the web tool did", bool(trail), resp.get("web_tool_calls"))
        if trail:
            check("the trail names the tool", trail[0].get("tool") == "toolkit_search", trail[0])
            check("the trail carries the answer",
                  "example.com" in json.dumps(trail[0], ensure_ascii=False), trail[0])
        # The model was offered the search tool, and the tool result went back.
        chat_calls = [b for p, b in SEEN if not p.endswith("/v1/tools/search")]
        check("the model was offered the search tool",
              any(any(t.get("function", {}).get("name") == "toolkit_search"
                      for t in (b.get("tools") or [])) for b in chat_calls),
              [b.get("tools") for b in chat_calls])
        check("the tool result went back to the model",
              any(any(m.get("role") == "tool" for m in (b.get("messages") or []))
                  for b in chat_calls),
              [b.get("messages") for b in chat_calls])

        # ---- 2b) the streaming path does the same --------------------------
        del SEEN[:]
        del SEARCH_HITS[:]
        status, resp = call(port, "POST", "/api/llm/chat", {
            "prompt": "搜一下 tea_a2", "web_tools": True, "stream": True})
        check("the streaming call answers", status == 200, status)
        check("the search really happened (streamed)", "tea_a2" in SEARCH_HITS, SEARCH_HITS)
        check("the stream carries the final answer",
              "我搜到了" in json.dumps(resp, ensure_ascii=False), str(resp)[:200])
        check("the stream carries the web trail",
              "web_tool_calls" in json.dumps(resp, ensure_ascii=False), str(resp)[:300])

        # ---- 2c) a streaming call asks the model exactly once --------------
        #
        # Regression: the streaming provider never told httplib that it was
        # done, so httplib called it again and again - and every call asked the
        # model the same question again (the caller paid for each one).
        del SEEN[:]
        del SEARCH_HITS[:]
        status, resp = call(port, "POST", "/api/llm/chat", {
            "prompt": "搜一下 tea_a2", "web_tools": True, "stream": True})
        chat_calls = [b for p, b in SEEN if not p.endswith("/v1/tools/search")]
        check("the streamed call asked the model twice (once, then after the search)",
              len(chat_calls) == 2, len(chat_calls))

        # ---- 3) without the flag, nothing is executed ----------------------
        del SEEN[:]
        del SEARCH_HITS[:]
        status, resp = call(port, "POST", "/api/llm/chat", {"prompt": "搜一下 tea_a2"})
        check("the plain call answers", status == 200, (status, resp))
        check("the plain call executed no web tool", not SEARCH_HITS, SEARCH_HITS)
        check("the plain call returns the tool call to the caller",
              bool(resp.get("tool_calls")), resp.get("tool_calls"))
        check("the plain call has no web trail", not resp.get("web_tool_calls"), resp)
    except Exception as e:  # noqa: BLE001 - a harness error is a failure too
        print("harness error:", e)
        failures.append("harness")
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            proc.kill()
        mock.shutdown()

    print()
    print("%d checks, %d failed" % (len(checks), len(failures)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
