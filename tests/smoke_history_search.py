#!/usr/bin/env python3
"""Smoke test for the workspace **history** search (conversations + trajectory).

"我上次说的那句话在哪？" - the answer is a search over what happened in this
workspace: the session titles, what was said, what was thought, what was
called - and the trajectory, which is where a command's output lives.

The matching rule is the same as for the file search: the query is a plain
string compared byte for byte (no regular expression, no glob). `exact=1` asks
for a whole-string match (完全匹配).

What this pins:

  * GET /api/workspaces/{id}/sessions/search finds a session by what was said
    in it, and by what a command printed (the trajectory);
  * scope=title / messages / trajectory narrow the search down;
  * exact=1 only matches the whole string;
  * the query is literal;
  * another workspace's history is not found;
  * the agent can do the same through the toolkit_history_search tool, and the
    tool is advertised to the model.

Usage: python3 tests/smoke_history_search.py [path-to-tea_agent-binary]
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
import urllib.parse
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

# The query strings with Chinese characters have to be percent-encoded before
# they go into a URL.
q_title = urllib.parse.quote("标题里的词")
q_title_exact = urllib.parse.quote("标题里的词 MAGICTOKEN")


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

        # The follow-up request (which sees the tool result) answers with text.
        if tool_msgs:
            self._content("done")
            return
        last_user = ""
        for m in reversed(payload.get("messages") or []):
            if m.get("role") == "user":
                last_user = m.get("content") or ""
                break
        if "SCENARIO=history" in last_user:
            self._tool_call("toolkit_history_search",
                            {"query": "MAGICTOKEN", "scope": "all"},
                            "call_history")
        elif "SCENARIO=exec" in last_user:
            self._tool_call("toolkit_exec",
                            {"command": "echo MAGICTOKEN from the shell"},
                            "call_exec")
        elif "SCENARIO=answer" in last_user:
            self._content("the answer contains MAGICTOKEN right here")
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


def session_ids(payload):
    return [r.get("session_id") for r in payload.get("results") or []]


def snippet_kinds(hit):
    return [s.get("kind") for s in hit.get("snippets") or []]


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

    tmp = tempfile.mkdtemp(prefix="tea_hist_")
    agent_port = free_port()
    ws_dir = os.path.join(tmp, "ws")
    ws2_dir = os.path.join(tmp, "ws2")
    os.makedirs(ws_dir)
    os.makedirs(ws2_dir)
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

        # ---- two conversations with something in them -----------------------
        events = run_turn(agent_port, wid, "answer")
        check("the first turn finished", any(e.get("type") == "done" for e in events), events[-2:])
        _, sessions = call(agent_port, "GET", "/api/workspaces/%s/sessions" % wid)
        answer_session = (sessions.get("sessions") or [{}])[0].get("id")
        check("the session was created", bool(answer_session), sessions)

        events = run_turn(agent_port, wid, "exec")
        res = tool_results(events).get("toolkit_exec")
        check("the command ran", res is not None and res.get("ok"), res)
        check("the command printed the marker",
              res and "MAGICTOKEN" in res.get("output", ""), res and res.get("output", "")[:200])
        _, sessions = call(agent_port, "GET", "/api/workspaces/%s/sessions" % wid)
        ids = [s.get("id") for s in sessions.get("sessions") or []]
        exec_session = None
        for sid in ids:
            if sid != answer_session:
                exec_session = sid
        check("a second session was created", bool(exec_session), ids)

        # ---- what was said --------------------------------------------------
        status, r = call(agent_port, "GET",
                         "/api/workspaces/%s/sessions/search?q=MAGICTOKEN" % wid)
        check("the search answers 200", status == 200, (status, r))
        check("both conversations are found",
              answer_session in session_ids(r) and exec_session in session_ids(r),
              session_ids(r))
        check("the response says what it looked for",
              r.get("query") == "MAGICTOKEN" and r.get("scope") == "all", r)
        hit = [h for h in r.get("results") or [] if h.get("session_id") == answer_session]
        check("the answer is quoted back",
              hit and any("MAGICTOKEN" in (s.get("text") or "") for s in hit[0].get("snippets") or []),
              hit)
        check("the hit says where it came from",
              hit and "message" in snippet_kinds(hit[0]), snippet_kinds(hit[0] if hit else {}))

        # ---- what a command printed (the trajectory) ------------------------
        hit = [h for h in r.get("results") or [] if h.get("session_id") == exec_session]
        check("the trajectory is searched too",
              hit and "trajectory" in snippet_kinds(hit[0]), snippet_kinds(hit[0] if hit else {}))
        check("the tool is named in the hit",
              hit and any(s.get("tool") == "toolkit_exec" for s in hit[0].get("snippets") or []),
              hit and hit[0].get("snippets"))

        # ---- scope narrows it down -----------------------------------------
        status, r = call(agent_port, "GET",
                         "/api/workspaces/%s/sessions/search?q=MAGICTOKEN&scope=messages" % wid)
        kinds = []
        for h in r.get("results") or []:
            kinds += snippet_kinds(h)
        check("scope=messages ignores the trajectory",
              "trajectory" not in kinds and "message" in kinds, kinds)
        status, r = call(agent_port, "GET",
                         "/api/workspaces/%s/sessions/search?q=MAGICTOKEN&scope=trajectory" % wid)
        kinds = []
        for h in r.get("results") or []:
            kinds += snippet_kinds(h)
        check("scope=trajectory only looks at the trajectory",
              kinds and set(kinds) == {"trajectory"}, kinds)

        # ---- the title ------------------------------------------------------
        status, r = call(agent_port, "GET",
                         "/api/workspaces/%s/sessions/search?q=%s&scope=title" % (wid, q_title))
        check("a title that was never used finds nothing", not session_ids(r), session_ids(r))
        status, renamed = call(agent_port, "PATCH", "/api/sessions/%s" % answer_session,
                               {"title": "标题里的词 MAGICTOKEN"})
        check("the session was renamed", status == 200 and renamed.get("ok"), (status, renamed))
        status, r = call(agent_port, "GET",
                         "/api/workspaces/%s/sessions/search?q=%s&scope=title" % (wid, q_title))
        check("the title is searched", answer_session in session_ids(r), session_ids(r))
        hit = [h for h in r.get("results") or [] if h.get("session_id") == answer_session]
        check("the title hit is marked", hit and hit[0].get("title_hit"), hit)

        # ---- 完全字符串匹配 --------------------------------------------------
        status, r = call(agent_port, "GET",
                         "/api/workspaces/%s/sessions/search?q=MAGICTOKEN&exact=1" % wid)
        check("exact does not match a word inside a sentence", not session_ids(r),
              session_ids(r))
        status, r = call(agent_port, "GET",
                         "/api/workspaces/%s/sessions/search?q=%s&scope=title&exact=1"
                         % (wid, q_title_exact))
        check("exact matches the whole title", answer_session in session_ids(r), session_ids(r))

        # ---- the query is literal -------------------------------------------
        status, r = call(agent_port, "GET",
                         "/api/workspaces/%s/sessions/search?q=.*" % wid)
        check("a regex-looking query is taken literally", not session_ids(r), session_ids(r))

        # ---- another workspace's history is not found ------------------------
        status, added = call(agent_port, "POST", "/api/workspaces", {"path": ws2_dir})
        check("a second workspace was registered", status == 200, (status, added))
        wid2 = added.get("id")
        status, r = call(agent_port, "GET",
                         "/api/workspaces/%s/sessions/search?q=MAGICTOKEN" % wid2)
        check("the other workspace's history is empty", not session_ids(r), session_ids(r))
        check("the other workspace saw no session",
              r.get("sessions_scanned") == 0, r.get("sessions_scanned"))

        # ---- bad requests are refused ---------------------------------------
        status, r = call(agent_port, "GET", "/api/workspaces/%s/sessions/search" % wid)
        check("a missing query is refused", status == 400, (status, r))
        status, r = call(agent_port, "GET",
                         "/api/workspaces/%s/sessions/search?q=x&scope=nope" % wid)
        check("an unknown scope is refused", status == 400, (status, r))
        status, r = call(agent_port, "GET", "/api/workspaces/ws_nope/sessions/search?q=x")
        check("an unknown workspace is a 404", status == 404, (status, r))

        # ---- the agent can search too ---------------------------------------
        status, r = call(agent_port, "GET", "/api/tools")
        names = [t.get("name") for t in r.get("tools") or []]
        check("the tool is advertised", "toolkit_history_search" in names, names)
        events = run_turn(agent_port, wid, "history")
        res = tool_results(events).get("toolkit_history_search")
        check("the tool runs", res is not None and res.get("ok"), res)
        output = (res or {}).get("output", "")
        check("the tool reports the sessions it found",
              answer_session in output and exec_session in output, output[:400])
        check("the tool quotes the matching line",
              "MAGICTOKEN" in output, output[:400])
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
