#!/usr/bin/env python3
"""Smoke test for the workspace experience log (`.tea_agent_run/agent.md`).

The file is the counterpart of "memory" for *how this project is worked on*:
pitfalls hit, non-obvious commands, scripts a session wrote under
`.tea_agent_run/scripts/`. It is injected into the system prompt of every turn
and maintained by the agent through `toolkit_agent_doc`.

What this pins:

  * `agent.md` is seeded (with its section template) when a workspace is opened,
    and `.tea_agent_run/scripts/` exists next to it;
  * every model request carries the document in its system prompt, so the model
    starts each turn knowing what earlier sessions learned;
  * `toolkit_agent_doc` op=append writes a dated entry on disk and shows up in
    the *next* request's system prompt (no restart needed);
  * op=read returns the document, op=replace rewrites it, op=scripts lists what
    is in the scripts directory;
  * an append that would push the file past its 32 KB cap fails with a message
    telling the model to compact it, instead of silently growing the prompt;
  * GET/PUT /api/workspaces/{id}/agent-doc let a human read and rewrite it.

Usage: python3 tests/smoke_agent_doc.py [path-to-tea_agent-binary]
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

        # The first request of each turn asks for one tool; the follow-up (which
        # sees the tool result) answers with text. `scenario` comes from the
        # user message so one mock can drive every case.
        if tool_msgs:
            self._content("done")
            return
        last_user = ""
        for m in reversed(payload.get("messages") or []):
            if m.get("role") == "user":
                last_user = m.get("content") or ""
                break
        if "SCENARIO=append" in last_user:
            self._tool_call("toolkit_agent_doc",
                            {"op": "append",
                             "text": "pytest needs `python3 -m pytest -q` here; plain `pytest` "
                                     "picks up the wrong interpreter."},
                            "call_append")
        elif "SCENARIO=read" in last_user:
            self._tool_call("toolkit_agent_doc", {"op": "read"}, "call_read")
        elif "SCENARIO=replace" in last_user:
            self._tool_call("toolkit_agent_doc",
                            {"op": "replace",
                             "content": "# agent.md - workspace experience\n\n## Pitfalls\n"
                                        "- rewritten by the test\n"},
                            "call_replace")
        elif "SCENARIO=scripts" in last_user:
            self._tool_call("toolkit_agent_doc", {"op": "scripts"}, "call_scripts")
        elif "SCENARIO=overflow" in last_user:
            self._tool_call("toolkit_agent_doc",
                            {"op": "append", "text": "X" * (40 * 1024)},
                            "call_overflow")
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


def system_prompt(request):
    for m in request.get("messages") or []:
        if m.get("role") == "system":
            c = m.get("content")
            if isinstance(c, list):
                return "".join(p.get("text", "") for p in c if isinstance(p, dict))
            return c or ""
    return ""


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

    tmp = tempfile.mkdtemp(prefix="tea_agentdoc_")
    agent_port = free_port()
    ws_dir = os.path.join(tmp, "ws")
    os.makedirs(ws_dir)
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

    run_dir = os.path.join(ws_dir, ".tea_agent_run")
    doc_path = os.path.join(run_dir, "agent.md")
    scripts_dir = os.path.join(run_dir, "scripts")

    try:
        if not wait_health(agent_port):
            print("server did not become healthy")
            return 1
        _, listing = call(agent_port, "GET", "/api/workspaces")
        wid = listing["workspaces"][0]["id"]

        # ---- seeded on open ------------------------------------------------
        check("agent.md is created with the workspace", os.path.isfile(doc_path), doc_path)
        seeded = open(doc_path, encoding="utf-8").read() if os.path.isfile(doc_path) else ""
        check("the seed carries the section template",
              "## Pitfalls" in seeded and "## Commands & scripts worth reusing" in seeded,
              seeded[:120])
        check("the reusable-script directory exists", os.path.isdir(scripts_dir), scripts_dir)

        # ---- injected into every request -----------------------------------
        before = len(MockProvider.records)
        events = run_turn(agent_port, wid, "plain")
        check("plain turn finished", any(e.get("type") == "done" for e in events), events[-2:])
        first_prompt = system_prompt(MockProvider.records[before])
        check("the system prompt carries agent.md",
              "Workspace experience" in first_prompt and "## Pitfalls" in first_prompt,
              first_prompt[-200:])
        check("the system prompt says how to maintain it",
              "toolkit_agent_doc" in first_prompt and ".tea_agent_run/scripts/" in first_prompt)

        # ---- append happens mid-turn and is visible next turn --------------
        events = run_turn(agent_port, wid, "append")
        r = tool_results(events).get("toolkit_agent_doc")
        check("op=append succeeds", r is not None and r.get("ok"), r)
        text = open(doc_path, encoding="utf-8").read()
        check("the entry is on disk with a date",
              "pytest needs `python3 -m pytest -q` here" in text and "- 20" in text, text[-240:])
        # The follow-up request of the same turn must already see it.
        follow_up_prompt = system_prompt(MockProvider.records[-1])
        check("the same turn's follow-up request sees the new entry",
              "pytest needs `python3 -m pytest -q` here" in follow_up_prompt,
              follow_up_prompt[-300:])

        # ---- read / replace / scripts --------------------------------------
        events = run_turn(agent_port, wid, "read")
        r = tool_results(events).get("toolkit_agent_doc")
        check("op=read returns the document",
              r is not None and r.get("ok") and "pytest needs" in r.get("output", ""), r)

        script = os.path.join(scripts_dir, "build_fast.sh")
        with open(script, "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\necho fast build\n")
        os.chmod(script, 0o755)
        events = run_turn(agent_port, wid, "scripts")
        r = tool_results(events).get("toolkit_agent_doc")
        check("op=scripts lists reusable scripts",
              r is not None and r.get("ok") and "build_fast.sh" in r.get("output", ""), r)

        events = run_turn(agent_port, wid, "replace")
        r = tool_results(events).get("toolkit_agent_doc")
        check("op=replace rewrites the document", r is not None and r.get("ok"), r)
        text = open(doc_path, encoding="utf-8").read()
        check("the rewritten document is what was written",
              "rewritten by the test" in text and "pytest needs" not in text, text)

        # ---- the size cap is enforced, not silently ignored -----------------
        events = run_turn(agent_port, wid, "overflow")
        r = tool_results(events).get("toolkit_agent_doc")
        check("an oversized append fails with a compaction hint",
              r is not None and not r.get("ok") and "limit" in r.get("output", ""),
              r and r.get("output", "")[:200])
        check("the oversized append did not touch the file",
              os.path.getsize(doc_path) < 32768, os.path.getsize(doc_path))

        # ---- the human-facing API -----------------------------------------
        status, doc = call(agent_port, "GET", "/api/workspaces/%s/agent-doc" % wid)
        check("GET agent-doc returns the document",
              status == 200 and "rewritten by the test" in doc.get("content", ""), doc)
        check("GET agent-doc reports the paths",
              doc.get("relative_path", "").endswith(".tea_agent_run/agent.md")
              and doc.get("scripts_dir", "").endswith(".tea_agent_run/scripts"),
              doc.get("relative_path"))
        check("GET agent-doc lists the scripts",
              [s.get("name") for s in doc.get("scripts", [])] == ["build_fast.sh"],
              doc.get("scripts"))

        status, saved = call(agent_port, "PUT", "/api/workspaces/%s/agent-doc" % wid,
                             {"content": "# hand edited\n\n- keep it short\n"})
        check("PUT agent-doc saves", status == 200 and saved.get("ok"), saved)
        text = open(doc_path, encoding="utf-8").read()
        check("the hand-edited content is on disk", text == "# hand edited\n\n- keep it short\n", text)
        before = len(MockProvider.records)
        run_turn(agent_port, wid, "plain")
        check("the edit is visible to the next turn",
              "keep it short" in system_prompt(MockProvider.records[before]))

        status, err = call(agent_port, "PUT", "/api/workspaces/%s/agent-doc" % wid,
                           {"content": "Y" * (33 * 1024)})
        check("PUT refuses a document past the cap",
              status == 400 and "limit" in ((err.get("error") or {}).get("message") or ""),
              (status, err))
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
