#!/usr/bin/env python3
"""Context compaction: a turn must not die with "context window exceeds limit".

Two failure modes are pinned here, both of them real:

1. **The compaction never ran.** The pressure check only happened at the start
   of a turn, so a turn with many tool calls grew the context until the
   provider rejected the request. The overflow recovery exists, but it is a
   recovery - the turn is already lost by then.

2. **The compaction ran and failed.** MiniMax-M3.1-Flash (and every other
   thinking model) spends the completion budget on `reasoning_content` first.
   With the 800-token summary budget the model answered with 4 KB of
   reasoning and an *empty* content, the compaction was reported as failed,
   the history was never folded, and the turn ended with the provider's
   original 400.

The mock provider therefore

  * rejects a too-large request the way MiniMax does
    ("invalid params, context window exceeds limit (2013)"), and
  * answers a summarisation request with reasoning only, the way a thinking
    model does when the completion budget is too small.

What this pins:

  * a turn that hits the provider's window limit is compacted and retried, and
    it finishes;
  * the summary request gets a budget a thinking model can work with, and a
    model that answers with reasoning only is asked again;
  * the compaction happens *during* a long turn, not only before it.

Usage: python3 tests/smoke_compaction_overflow.py [path-to-tea_agent-binary]
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

# The mock's own limit, in characters of conversation. The agent's window is
# 20000 tokens, so its budget is ~39k characters and its pressure trigger
# ~31k: a limit of 30k is "the provider is stricter than we think", which is
# exactly the situation the recovery path exists for.
OVERFLOW_LIMIT = 30000
# The limit of the "grow" provider: above the agent's trigger, so a long turn
# has to compact *before* the request goes out.
GROW_LIMIT = 45000
# How many tool calls the "grow" provider asks for before it answers.
GROW_TOOL_CALLS = 4
# The size of the file the tool call reads. Four of them have to fit into the
# window for the context to cross the pressure trigger in one turn.
BIG_FILE_CHARS = 9000

seen = {"overflow": [], "grow": []}
summary_calls = {"overflow": 0, "grow": 0}
# How often the provider had to reject a request as "too long".
rejected = {"overflow": 0, "grow": 0}
lock = threading.Lock()


def message_chars(messages):
    """How much conversation a request carries (the system prompt excluded)."""
    total = 0
    for m in messages:
        if m.get("role") == "system":
            continue
        content = m.get("content")
        if isinstance(content, str):
            total += len(content)
    return total


def is_summary_request(messages):
    for m in messages:
        if m.get("role") == "system" and isinstance(m.get("content"), str):
            if "compress conversation history" in m["content"]:
                return True
    return False


class Mock(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    # --- helpers ---------------------------------------------------------
    def _json(self, status, payload):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _sse(self, chunks):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        def write(text):
            data = text.encode()
            try:
                self.wfile.write(("%x\r\n" % len(data)).encode() + data + b"\r\n")
                self.wfile.flush()
            except OSError:
                pass

        for chunk in chunks:
            write("data: " + json.dumps({
                "id": "c1", "object": "chat.completion.chunk",
                "choices": [{"index": 0, "delta": chunk}],
            }) + "\n\n")
        write("data: " + json.dumps({
            "id": "c1", "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }) + "\n\n")
        write("data: [DONE]\n\n")
        write("")

    def _tool_call(self, name, arguments):
        return [
            {"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                             "function": {"name": name, "arguments": ""}}]},
            {"tool_calls": [{"index": 0,
                             "function": {"arguments": json.dumps(arguments)}}]},
        ]

    # --- the route -------------------------------------------------------
    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            body = {}
        messages = body.get("messages") or []
        which = "grow" if "/grow/" in self.path else "overflow"
        with lock:
            seen[which].append(body)

        # --- the summarisation request -----------------------------------
        if is_summary_request(messages):
            with lock:
                summary_calls[which] += 1
                nth = summary_calls[which]
            if which == "overflow" and nth == 1:
                # A thinking model whose completion budget went into the
                # reasoning: no summary, only thoughts.
                print("   [mock] summary request #%d (max_tokens=%s) -> reasoning only"
                      % (nth, body.get("max_tokens")))
                self._json(200, {
                    "id": "chatcmpl-summary",
                    "object": "chat.completion",
                    "model": body.get("model"),
                    "choices": [{
                        "index": 0,
                        "message": {"role": "assistant", "content": "",
                                    "reasoning_content": "thinking about the summary " * 40},
                        "finish_reason": "length",
                    }],
                })
                return
            print("   [mock] summary request #%d (max_tokens=%s) -> summary"
                  % (nth, body.get("max_tokens")))
            self._json(200, {
                "id": "chatcmpl-summary",
                "object": "chat.completion",
                "model": body.get("model"),
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant",
                                "content": "COMPACTED SUMMARY: the earlier turns were folded."},
                    "finish_reason": "stop",
                }],
            })
            return

        # --- an ordinary turn --------------------------------------------
        chars = message_chars(messages)
        limit = GROW_LIMIT if which == "grow" else OVERFLOW_LIMIT
        if chars > limit:
            with lock:
                rejected[which] += 1
            print("   [mock] %s request with %d chars > %d -> 400 context window"
                  % (which, chars, limit))
            self._json(400, {
                "type": "error",
                "error": {
                    "type": "bad_request_error",
                    "message": "invalid params, context window exceeds limit (2013)",
                    "http_code": "400",
                },
            })
            return

        # The tool calls of the *current* turn: everything after the last
        # user message. "WORK" marks the turn that has to grow the context.
        last_user = -1
        for i, m in enumerate(messages):
            if m.get("role") == "user":
                last_user = i
        turn_text = ""
        if last_user >= 0 and isinstance(messages[last_user].get("content"), str):
            turn_text = messages[last_user]["content"]
        turn_tools = [m for m in messages[last_user + 1:] if m.get("role") == "tool"]
        if which == "grow" and "WORK" in turn_text and len(turn_tools) < GROW_TOOL_CALLS:
            chunks = self._tool_call("toolkit_file", {"op": "read", "path": "big.txt"})
            chunks.append({"content": "read big.txt"})
            self._sse(chunks)
            return

        self._sse([{"content": "ok"}])


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


def turn(port, wid, session, provider, message):
    """One turn; returns the list of event types (in order)."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=120)
    conn.request("POST", "/api/chat",
                 body=json.dumps({"workspace_id": wid, "session_id": session,
                                  "provider": provider, "message": message}),
                 headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
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
        if ev.get("type") == "error":
            print("   turn error:", ev.get("message"))
    conn.close()
    return types


def main():
    binary = (sys.argv[1] if len(sys.argv) > 1
              else os.path.join(REPO_ROOT, "build-mingw", "bin", "tea_agent.exe"))
    if not os.path.exists(binary):
        print("binary not found:", binary)
        return 1

    mock = ThreadingHTTPServer(("127.0.0.1", 0), Mock)
    mock.daemon_threads = True
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    mock_port = mock.server_address[1]

    tmp = tempfile.mkdtemp(prefix="tea_compact_")
    ws_dir = os.path.join(tmp, "ws")
    os.makedirs(ws_dir)
    with open(os.path.join(ws_dir, "big.txt"), "w", encoding="utf-8") as f:
        f.write("x" * BIG_FILE_CHARS)

    config_path = os.path.join(tmp, "provider.yaml")
    with open(config_path, "w", encoding="utf-8") as f:
        f.write("""version: 1
providers:
  overflow:
    api_url: http://127.0.0.1:%d/overflow/v1
    api_key: sk-test
    models:
      m:
        max_context_tokens: 20000
        max_output_tokens: 1000
        supports_tools: true
        supports_reasoning: true
  grow:
    api_url: http://127.0.0.1:%d/grow/v1
    api_key: sk-test
    models:
      m:
        max_context_tokens: 20000
        max_output_tokens: 1000
        supports_tools: true
        supports_reasoning: true
settings:
  max_iterations: 12
  max_tool_output: 200000
  stream: true
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
        print(("PASS " if ok else "FAIL ") + name +
              ((("  [" + str(detail) + "]")) if not ok else ""))
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

        # ---- 1. the provider rejects an over-long request -----------------
        session = call(port, "POST", "/api/workspaces/%s/sessions" % wid,
                       {"title": "overflow"})[1]["id"]
        pad = "p" * 5000
        types = []
        all_types = []
        for i in range(8):
            types = turn(port, wid, session, "overflow", "turn %d %s" % (i, pad))
            all_types.extend(types)
            if "error" in types:
                break
        check("the turn that hit the window limit still finished",
              "done" in types and "error" not in types, types)
        # The mock answers a too-large request with a 400; the agent has to
        # fold the history and send a smaller one.
        check("the provider did reject an over-long request",
              rejected["overflow"] >= 1, rejected)
        check("a compaction happened", "compaction" in all_types, all_types)
        sess_file = os.path.join(ws_dir, ".tea_agent_run", "sessions", "%s.json" % session)
        summary = ""
        summary_upto = 0
        if os.path.exists(sess_file):
            with open(sess_file, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            summary = data.get("summary") or ""
            summary_upto = data.get("summary_upto") or 0
        check("the session stores a summary", bool(summary.strip()), summary[:80])
        check("the summary covers the folded messages", summary_upto > 0, summary_upto)
        # The thinking model answered the first summary request with reasoning
        # only: the agent has to ask again (and with a bigger budget).
        check("the summary request was retried after an empty answer",
              summary_calls["overflow"] >= 2, summary_calls)
        summary_budgets = [b.get("max_tokens") for b in seen["overflow"]
                           if is_summary_request(b.get("messages") or [])]
        check("the summary request got a budget a thinking model can use",
              bool(summary_budgets) and (summary_budgets[0] or 0) > 1024, summary_budgets)

        # ---- 2. a long turn compacts while it runs -------------------------
        session2 = call(port, "POST", "/api/workspaces/%s/sessions" % wid,
                        {"title": "grow"})[1]["id"]
        for i in range(3):
            turn(port, wid, session2, "grow", "history %d %s" % (i, "h" * 6000))
        types2 = turn(port, wid, session2, "grow", "WORK now do some work")
        check("the long turn finished", "done" in types2 and "error" not in types2, types2)
        check("the compaction happened during the turn, not only before it",
              "compaction" in types2 and
              types2.index("compaction") > types2.index("iteration_end"),
              types2)
        check("the provider never had to reject a request",
              rejected["grow"] == 0, rejected)
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
