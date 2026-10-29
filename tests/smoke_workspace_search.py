#!/usr/bin/env python3
"""Smoke test for the workspace search (files + content, literal string match).

The search is deliberately simple: the query is a **plain string**, compared
byte for byte. No regular expression, no glob, no fuzzy matching. `exact=1`
switches to whole-string equality (完全匹配): the path (or the file name) has
to equal the query, or the whole line has to equal it.

What this pins:

  * GET /api/workspaces/{id}/search finds files by name and by content, and
    reports the line numbers of the content hits;
  * scope=name / scope=content narrow the search down;
  * exact=1 only matches the whole string (完全字符串匹配);
  * the query is literal: a query that looks like a regex matches itself;
  * the search never leaves the workspace;
  * the agent can do the same thing through the toolkit_workspace_search tool,
    and the tool is advertised to the model.

Usage: python3 tests/smoke_workspace_search.py [path-to-tea_agent-binary]
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

    def _done(self):
        try:
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except OSError:
            pass

    def _tool_call(self, name, args, call_id):
        self._chunk({
            "id": "c1",
            "object": "chat.completion.chunk",
            "choices": [{
                "index": 0,
                "delta": {
                    "role": "assistant",
                    "tool_calls": [{
                        "index": 0, "id": call_id, "type": "function",
                        "function": {"name": name, "arguments": json.dumps(args)},
                    }],
                },
                "finish_reason": None,
            }],
        })
        self._chunk({"id": "c1", "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]})
        self._done()

    def _content(self, text):
        self._chunk({"id": "c1", "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]})
        self._chunk({"id": "c1", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
        self._done()

    def do_POST(self):
        if self.path != "/v1/chat/completions":
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        payload = self._read_json()
        MockProvider.records.append(payload)
        tool_msgs = [m for m in (payload.get("messages") or []) if m.get("role") == "tool"]

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()

        # The first request of the turn asks for the search tool; the follow-up
        # (which sees the tool result) answers with text.
        if tool_msgs:
            self._content("done")
            return
        last_user = ""
        for m in reversed(payload.get("messages") or []):
            if m.get("role") == "user":
                last_user = m.get("content") or ""
                break
        if "SCENARIO=search" in last_user:
            self._tool_call("toolkit_workspace_search",
                            {"query": "MAGICTOKEN", "scope": "content"},
                            "call_search")
        else:
            self._content("plain answer")


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


def call(port, method, path, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    headers = {"Content-Type": "application/json"} if body is not None else {}
    conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
    resp = conn.getresponse()
    raw = resp.read()
    try:
        return resp.status, json.loads(raw or b"null")
    except ValueError:
        return resp.status, {"raw": raw.decode("utf-8", "replace")}


def run_turn(port, workspace_id, scenario):
    """One full SSE turn; returns the events."""
    body = {"workspace_id": workspace_id, "message": "SCENARIO=" + scenario}
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=120)
    conn.request("POST", "/api/chat", body=json.dumps(body),
                 headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    events = []
    while True:
        line = resp.readline()
        if not line:
            break
        line = line.strip()
        if not line.startswith(b"data: "):
            continue
        data = line[6:]
        if data == b"[DONE]":
            break
        try:
            events.append(json.loads(data))
        except ValueError:
            continue
    return events


def tool_results(events):
    return {e.get("name"): e for e in events if e.get("type") == "tool_result"}


def paths_of(payload):
    return [r.get("path") for r in payload.get("results") or []]


def main():
    binary = (sys.argv[1] if len(sys.argv) > 1
              else os.path.join(REPO_ROOT, "build", "bin", "tea_agent"))
    if not os.path.exists(binary):
        print("binary not found:", binary)
        return 1

    MockProvider.records = []
    mock = ThreadingHTTPServer(("127.0.0.1", 0), MockProvider)
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    mock_port = mock.server_address[1]

    tmp = tempfile.mkdtemp(prefix="tea_search_")
    agent_port = free_port()
    ws_dir = os.path.join(tmp, "ws")
    os.makedirs(os.path.join(ws_dir, "src"))
    os.makedirs(os.path.join(ws_dir, "docs"))
    with open(os.path.join(ws_dir, "src", "main.cpp"), "w", encoding="utf-8") as f:
        f.write("int main() {\n  // MAGICTOKEN lives here\n  return 0;\n}\n")
    with open(os.path.join(ws_dir, "docs", "guide.md"), "w", encoding="utf-8") as f:
        f.write("# Guide\n\nnothing to see here\n")
    with open(os.path.join(ws_dir, "notes.txt"), "w", encoding="utf-8") as f:
        f.write("a line with MAGICTOKEN inside\n")
    # A file outside the workspace: the search must never reach it.
    with open(os.path.join(tmp, "outside.txt"), "w", encoding="utf-8") as f:
        f.write("MAGICTOKEN outside the workspace\n")

    config_path = os.path.join(tmp, "provider.yaml")
    with open(config_path, "w", encoding="utf-8") as f:
        f.write("""version: 1
providers:
  mock:
    api_url: http://127.0.0.1:%d
    api_key: sk-test
    default_model: m
    models:
      m:
        max_context_tokens: 32768
        max_output_tokens: 4096
        supports_tools: true
""" % mock_port)

    proc = subprocess.Popen([
        binary, "--config", config_path, "--tea-home", os.path.join(tmp, "home"),
        "--host", "127.0.0.1", "--port", str(agent_port), "--no-console",
        "--log-file", os.path.join(tmp, "tea_agent.log"), "--workspace", ws_dir,
    ])
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
        _, listing = call(agent_port, "GET", "/api/workspaces")
        wid = listing["workspaces"][0]["id"]

        # ---- the search finds files by name --------------------------------
        status, r = call(agent_port, "GET",
                         "/api/workspaces/%s/search?q=main.cpp" % wid)
        check("GET search answers 200", status == 200, (status, r))
        check("the file name matches", "src/main.cpp" in paths_of(r), paths_of(r))
        check("the hit is marked as a name hit",
              any(x.get("name_hit") for x in r.get("results") or []), r.get("results"))
        check("the response says what it looked for",
              r.get("query") == "main.cpp" and r.get("scope") == "both", r)

        # ---- and by content, with the line numbers --------------------------
        status, r = call(agent_port, "GET",
                         "/api/workspaces/%s/search?q=MAGICTOKEN&scope=content" % wid)
        found = paths_of(r)
        check("the content hit is found", "src/main.cpp" in found, found)
        check("the second file is found too", "notes.txt" in found, found)
        hit = [x for x in r.get("results") or [] if x.get("path") == "src/main.cpp"]
        check("the line number of the hit is reported",
              hit and hit[0].get("lines") == [2], hit)
        check("the matching line is quoted back",
              hit and "MAGICTOKEN" in (hit[0].get("preview") or ""), hit)

        # ---- scope narrows it down -----------------------------------------
        status, r = call(agent_port, "GET",
                         "/api/workspaces/%s/search?q=MAGICTOKEN&scope=name" % wid)
        check("scope=name ignores the content", not paths_of(r), paths_of(r))
        status, r = call(agent_port, "GET",
                         "/api/workspaces/%s/search?q=main.cpp&scope=content" % wid)
        check("scope=content ignores the file name", not paths_of(r), paths_of(r))

        # ---- 完全字符串匹配 --------------------------------------------------
        status, r = call(agent_port, "GET",
                         "/api/workspaces/%s/search?q=main.cpp&scope=name&exact=1" % wid)
        check("exact finds the file by its name", "src/main.cpp" in paths_of(r), paths_of(r))
        status, r = call(agent_port, "GET",
                         "/api/workspaces/%s/search?q=main&scope=name&exact=1" % wid)
        check("exact does not match a part of the name", not paths_of(r), paths_of(r))
        status, r = call(agent_port, "GET",
                         "/api/workspaces/%s/search?q=src/main.cpp&scope=name&exact=1" % wid)
        check("exact matches the whole relative path",
              "src/main.cpp" in paths_of(r), paths_of(r))
        status, r = call(agent_port, "GET",
                         "/api/workspaces/" + wid + "/search?q=a%20line%20with%20MAGICTOKEN%20inside"
                         "&scope=content&exact=1")
        check("exact matches a whole line",
              "notes.txt" in paths_of(r), paths_of(r))
        status, r = call(agent_port, "GET",
                         "/api/workspaces/" + wid + "/search?q=MAGICTOKEN&scope=content&exact=1")
        check("exact does not match part of a line", not paths_of(r), paths_of(r))

        # ---- the query is literal, not a pattern -----------------------------
        status, r = call(agent_port, "GET",
                         "/api/workspaces/%s/search?q=.*&scope=content" % wid)
        check("a regex-looking query is taken literally", not paths_of(r), paths_of(r))
        status, r = call(agent_port, "GET",
                         "/api/workspaces/" + wid + "/search?q=Ma%5BGT%5D*&scope=content")
        check("a glob-looking query is taken literally", not paths_of(r), paths_of(r))

        # ---- the search stays inside the workspace ---------------------------
        status, r = call(agent_port, "GET",
                         "/api/workspaces/%s/search?q=MAGICTOKEN&scope=content" % wid)
        check("a file outside the workspace is not found",
              not any("outside" in (p or "") for p in paths_of(r)), paths_of(r))
        status, r = call(agent_port, "GET",
                         "/api/workspaces/%s/search?q=outside&scope=name" % wid)
        check("a file name outside the workspace is not found",
              not any("outside" in (p or "") for p in paths_of(r)), paths_of(r))

        # ---- a subdirectory can be searched on its own -----------------------
        status, r = call(agent_port, "GET",
                         "/api/workspaces/%s/search?q=MAGICTOKEN&scope=content&path=docs" % wid)
        check("path= limits the search to that directory", not paths_of(r), paths_of(r))
        status, r = call(agent_port, "GET",
                         "/api/workspaces/%s/search?q=MAGICTOKEN&scope=content&path=src" % wid)
        check("path= finds what is inside it", "src/main.cpp" in paths_of(r), paths_of(r))

        # ---- bad requests are refused ---------------------------------------
        status, r = call(agent_port, "GET", "/api/workspaces/%s/search" % wid)
        check("a missing query is refused", status == 400, (status, r))
        status, r = call(agent_port, "GET", "/api/workspaces/%s/search?q=x&scope=nope" % wid)
        check("an unknown scope is refused", status == 400, (status, r))
        status, r = call(agent_port, "GET", "/api/workspaces/ws_nope/search?q=x")
        check("an unknown workspace is a 404", status == 404, (status, r))

        # ---- the agent can search too ---------------------------------------
        events = run_turn(agent_port, wid, "search")
        res = tool_results(events).get("toolkit_workspace_search")
        check("the tool runs", res is not None and res.get("ok"), res)
        output = (res or {}).get("output", "")
        check("the tool reports the hits",
              "src/main.cpp" in output and "notes.txt" in output, output[:400])
        check("the tool reports the line numbers",
              "2: " in output, output[:400])

        tools = [t.get("function", {}).get("name")
                 for t in (MockProvider.records[0].get("tools") or [])]
        check("the tool is advertised to the model",
              "toolkit_workspace_search" in tools, tools)
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
