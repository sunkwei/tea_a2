#!/usr/bin/env python3
"""Smoke test for the plan orchestrator (the extension layer).

The orchestrator splits a job into subtasks that run in parallel and puts
the results back together:

  * the model writes the plan (one /api/llm/chat) and marks every step
    read-only ("ro") or read-write ("rw");
  * the read-only steps start next to each other, a read-write step runs
    alone;
  * the model writes the summary (one /api/llm/chat).

What this pins:

  * the extension module loads and the agent advertises the tool;
  * op="start" answers right away (it does not wait for the subtasks);
  * two read-only steps really overlap in time;
  * a read-only step does not get the command tool, the memory tool or the
    agent-doc tool - the promise is in the tool list, not in the prompt;
  * a read-write step does not overlap with anything;
  * op="status" answers with the state, and with the summary once the
    subtasks are done;
  * the gates: one plan per workspace per minute;
  * the plan is written down with the handle of every subtask;
  * a model can call the tool in a turn.

Usage: python3 tests/smoke_plan_orchestrator.py [path-to-tea_agent-binary]
"""

import http.client
import json
import os
import random
import re
import shutil
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

# How long a subtask takes. Long enough that two of them overlap.
SUBTASK_DELAY = 2.0


class MockProvider(BaseHTTPRequestHandler):
    """A provider that answers three kinds of request.

    * the planner (the system prompt says "拆成"): a plan with two
      read-only steps;
    * the aggregator (the system prompt says "汇总"): a summary that names
      the steps;
    * everything else: a subtask, which takes a moment and then answers.
    """

    records = []      # every request: what it was, and when
    lock = threading.Lock()

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
        self._chunk({"id": "c1", "choices": [{"index": 0, "delta": {"content": text},
                                               "finish_reason": None}]})
        self._chunk({"id": "c1", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
        self._done()

    def _json_answer(self, text):
        """A plain answer, for a request that did not ask for a stream.

        The primitive layer (/api/llm/chat without stream) sends a normal
        request to the provider, so the provider answers with JSON - not
        with the event stream an agent turn gets.
        """
        body = json.dumps({
            "id": "c1",
            "object": "chat.completion",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": text}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
        }).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path != "/v1/chat/completions":
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        payload = self._read_json()
        started = time.time()
        system = payload.get("system") or ""
        messages = payload.get("messages") or []
        # The agent sends the system prompt as a message, not as a field.
        for m in messages:
            if m.get("role") == "system":
                system += "\n" + str(m.get("content") or "")
        has_tool_result = any(m.get("role") == "tool" for m in messages)
        last_user = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                last_user = m.get("content") or ""
                break

        kind = "subtask"
        if "拆成" in system:
            kind = "plan"
        elif "汇总" in system:
            kind = "aggregate"
        elif has_tool_result:
            kind = "after_tool"
        elif "SCENARIO=" in last_user:
            kind = "parent"

        with MockProvider.lock:
            MockProvider.records.append({
                "kind": kind, "started": started, "finished": None,
                "tools": [t.get("function", {}).get("name")
                          for t in (payload.get("tools") or [])],
                "last_user": last_user,
            })
        record = MockProvider.records[-1]

        fail = False
        if kind == "plan":
            # The agent turns the prompt into a user message, so the goal is
            # in the messages, not in a "prompt" field.
            if "FAILFIRST" in last_user:
                # The step that changes something fails; the one that waits
                # for it can never run.
                plan = {
                    "title": "two writes",
                    "goal": "one after the other",
                    "aggregate_prompt": "put it together",
                    "steps": [
                        {"key": "s1", "title": "改", "prompt": "SUBTASK-FAIL",
                         "mode": "rw", "write_paths": ["out/"], "depends_on": []},
                        {"key": "s2", "title": "改完再验", "prompt": "SUBTASK-NEVER",
                         "mode": "rw", "write_paths": ["out2/"],
                         "depends_on": ["s1"]},
                    ],
                }
            elif "SUMMARIZE-FAIL" in last_user:
                # One read-only step; its finding carries the marker the
                # summary request will choke on.
                plan = {
                    "title": "one read",
                    "goal": "read it",
                    "aggregate_prompt": "put it together",
                    "steps": [
                        {"key": "s1", "title": "读", "prompt": "SUBTASK-SUMMARYFAIL",
                         "mode": "ro", "depends_on": []},
                    ],
                }
            elif "MIXED" in last_user:
                plan = {
                    "title": "mixed",
                    "goal": "one read, one write",
                    "aggregate_prompt": "put it together",
                    "steps": [
                        {"key": "s1", "title": "read", "prompt": "SUBTASK-READ",
                         "mode": "ro", "depends_on": []},
                        {"key": "s2", "title": "write", "prompt": "SUBTASK-WRITE",
                         "mode": "rw", "write_paths": ["out/"], "depends_on": []},
                    ],
                }
            else:
                plan = {
                    "title": "two reads",
                    "goal": "look at two things",
                    "aggregate_prompt": "put it together",
                    "steps": [
                        {"key": "s1", "title": "first", "prompt": "SUBTASK-ONE",
                         "mode": "ro", "depends_on": []},
                        {"key": "s2", "title": "second", "prompt": "SUBTASK-TWO",
                         "mode": "ro", "depends_on": []},
                    ],
                }
            answer = json.dumps(plan, ensure_ascii=False)
        elif kind == "aggregate":
            if "SUMMARIZE-FAIL" in last_user:
                fail = True
                answer = "the provider is down"
            else:
                answer = "SUMMARY: the first step says ONE, the second says TWO"
        elif kind == "parent":
            answer = None
        elif kind == "after_tool":
            answer = "the plan tool answered"
        else:
            # A subtask: it takes a moment, so two of them overlap.
            time.sleep(SUBTASK_DELAY)
            answer = "SUBTASK-ANSWER"
            if "SUBTASK-SUMMARYFAIL" in last_user:
                answer = "SUMMARIZE-FAIL: the step found something"
            if "SUBTASK-FAIL" in last_user:
                fail = True

        streaming = bool(payload.get("stream"))
        if fail:
            # A provider that says no: the turn fails, which is what a step
            # that cannot do its work looks like.
            body = json.dumps({"error": {"message": "the provider is down"}}).encode()
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            with MockProvider.lock:
                record["finished"] = time.time()
            return
        if kind == "parent":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self._tool_call("mcp__a2ext__toolkit_plan",
                            {"op": "status", "plan_id": "plan_doesnotexist"},
                            "call_plan")
        elif streaming:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self._content(answer)
        else:
            self._json_answer(answer)

        with MockProvider.lock:
            record["finished"] = time.time()


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
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=120)
    headers = {"Content-Type": "application/json"} if body is not None else {}
    conn.request(method, path, body=json.dumps(body) if body is not None else None,
                 headers=headers)
    resp = conn.getresponse()
    raw = resp.read()
    try:
        return resp.status, json.loads(raw or b"null")
    except ValueError:
        return resp.status, {"raw": raw.decode("utf-8", "replace")}


def mcp_call(port, tool, arguments):
    """Call a tool of an MCP server, the way the API offers it."""
    status, body = call(port, "POST", "/api/mcp/servers/a2ext/call",
                        {"tool": tool, "arguments": arguments})
    if status != 200:
        return status, "", body
    return status, str(body.get("text") or ""), body


def run_turn(port, workspace_id, message):
    """One full SSE turn; returns the events."""
    body = {"workspace_id": workspace_id, "message": message}
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=180)
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
    return [e for e in events if e.get("type") == "tool_result"]


def overlaps(first, second):
    """Do two recorded requests overlap in time?"""
    a, b = first["started"], second["started"]
    a_end = first["finished"] or time.time()
    b_end = second["finished"] or time.time()
    return a < b_end and b < a_end


def wait_for(predicate, timeout=60.0, step=0.3):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(step)
    return None


def subtask_records():
    with MockProvider.lock:
        return [r for r in MockProvider.records if r["kind"] == "subtask"]


def main():
    binary = (sys.argv[1] if len(sys.argv) > 1
              else os.path.join(REPO_ROOT, "build", "bin", "tea_agent"))
    if not os.path.exists(binary):
        print("binary not found:", binary)
        return 1
    module = os.path.join(REPO_ROOT, "sched", "a2_ext_py", "modules", "toolkit_plan.py")
    if not os.path.exists(module):
        print("module not found:", module)
        return 1
    mcp_server = os.path.join(REPO_ROOT, "sched", "a2_ext_py", "toolkit", "mcp_server.py")
    if not os.path.exists(mcp_server):
        print("mcp server not found:", mcp_server)
        return 1

    MockProvider.records = []
    mock = ThreadingHTTPServer(("127.0.0.1", 0), MockProvider)
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    mock_port = mock.server_address[1]

    tmp = tempfile.mkdtemp(prefix="tea_orch_")
    agent_port = free_port()
    home = os.path.join(tmp, "home")
    ext_dir = os.path.join(tmp, "ext")
    os.makedirs(ext_dir)
    os.makedirs(home)
    # The operator's copy of the module.
    shutil.copyfile(module, os.path.join(ext_dir, "toolkit_plan.py"))

    ws_a = os.path.join(tmp, "ws_a")
    ws_b = os.path.join(tmp, "ws_b")
    ws_c = os.path.join(tmp, "ws_c")
    ws_d = os.path.join(tmp, "ws_d")
    for one in (ws_a, ws_b, ws_c, ws_d):
        os.makedirs(one)

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

    # The extension server: the module needs to know where the agent is and
    # where the plans live.
    with open(os.path.join(tmp, "mcp.yaml"), "w", encoding="utf-8") as f:
        f.write("""servers:
  a2ext:
    command: "python3"
    args: ["%s", "--modules", "%s", "--agent-url", "http://127.0.0.1:%d"]
    env:
      TEA_AGENT_URL: "http://127.0.0.1:%d"
      TEA_HOME: "%s"
    startup_timeout_ms: 20000
    timeout_ms: 120000
""" % (mcp_server, ext_dir, agent_port, agent_port, home))

    proc = subprocess.Popen([
        binary, "--config", config_path, "--tea-home", home,
        "--host", "127.0.0.1", "--port", str(agent_port), "--no-console",
        "--log-file", os.path.join(tmp, "tea_agent.log"), "--workspace", ws_a,
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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

        # ---- the extension server is up and the tool is there ----------------
        def server_ready():
            _, listing = call(agent_port, "GET", "/api/mcp/servers")
            for one in listing.get("servers") or []:
                if one.get("name") == "a2ext" and one.get("connected"):
                    return one
            return None

        server = wait_for(server_ready, timeout=30.0)
        check("the extension server is connected", server is not None,
              call(agent_port, "GET", "/api/mcp/servers")[1])
        _, tools = call(agent_port, "GET", "/api/tools")
        names = [t.get("name") for t in (tools.get("tools") or [])]
        check("the orchestrator tool is advertised",
              "mcp__a2ext__toolkit_plan" in names, names)

        _, listing = call(agent_port, "GET", "/api/workspaces")
        wid_a = listing["workspaces"][0]["id"]
        status, made = call(agent_port, "POST", "/api/workspaces",
                            {"path": ws_b, "name": "ws_b"})
        check("a second workspace is registered", status == 200, (status, made))
        # The answer is the workspace entry itself.
        wid_b = made.get("id") or made.get("workspace", {}).get("id") or ""
        check("the second workspace has an id", bool(wid_b), made)

        # ---- op=start: it answers right away -------------------------------
        began = time.time()
        status, output, body = mcp_call(agent_port, "mcp__a2ext__toolkit_plan",
                                        {"op": "start", "goal": "look at two things",
                                         "workspace_id": wid_a})
        took = time.time() - began
        check("the orchestrator tool ran", status == 200, (status, body))
        check("the tool says the plan started", "编排已经开始了" in output, output[:400])
        match = re.search(r"(plan_[0-9a-f]+)", output)
        plan_id = match.group(1) if match else ""
        check("the tool answers with a plan id", bool(plan_id), output[:400])
        check("the tool does not wait for the subtasks", took < 3.0, took)

        # ---- the two read-only steps really ran at the same time -----------
        subs = wait_for(lambda: subtask_records() if len(subtask_records()) >= 2 else None,
                        timeout=60.0)
        check("both subtasks were started", subs is not None and len(subs) >= 2,
              len(MockProvider.records))
        if subs and len(subs) >= 2:
            check("the two read-only steps overlapped in time",
                  overlaps(subs[0], subs[1]),
                  [(r["started"], r["finished"]) for r in subs[:2]])

        # ---- a read-only step cannot change anything ------------------------
        ro_request = None
        with MockProvider.lock:
            for record in MockProvider.records:
                if record["kind"] == "subtask" and record["tools"]:
                    ro_request = record
                    break
        check("the read-only step got a tool list", ro_request is not None, None)
        if ro_request:
            tools_of_step = ro_request["tools"]
            check("the read-only step cannot run commands",
                  "toolkit_exec" not in tools_of_step, tools_of_step)
            check("the read-only step cannot write the shared memory",
                  "toolkit_memory" not in tools_of_step, tools_of_step)
            check("the read-only step cannot edit the agent doc",
                  "toolkit_agent_doc" not in tools_of_step, tools_of_step)
            check("the read-only step can search the workspace",
                  "toolkit_workspace_search" in tools_of_step, tools_of_step)

        # ---- the plan is written down --------------------------------------
        def plan_file():
            path = os.path.join(home, "plans", plan_id + ".json")
            if not os.path.exists(path):
                return None
            try:
                with open(path, encoding="utf-8") as f:
                    return json.load(f)
            except (OSError, ValueError):
                return None

        saved = wait_for(plan_file, timeout=60.0)
        check("the plan is written down", saved is not None, plan_id)
        if saved:
            handles = [s.get("turn_id") for s in (saved.get("subtasks") or {}).values()]
            check("every subtask has a handle", all(handles), handles)

        # ---- op=status: the state, and then the summary --------------------
        status, output, body = mcp_call(agent_port, "mcp__a2ext__toolkit_plan",
                                        {"op": "status", "plan_id": plan_id})
        check("the status tool answers", status == 200 and bool(output), (status, output[:300]))

        def status_text():
            _, text, _ = mcp_call(agent_port, "mcp__a2ext__toolkit_plan",
                                  {"op": "status", "plan_id": plan_id})
            return text

        deadline = time.time() + 90.0
        final = ""
        while time.time() < deadline:
            final = status_text()
            if "SUMMARY" in final:
                break
            time.sleep(2.0)
        check("the summary is there once the subtasks are done",
              "SUMMARY" in final, final[-400:])
        check("the summary names the steps it came from",
              "first" in final or "second" in final, final[-400:])

        # ---- the gate: one plan per workspace per minute --------------------
        status, output, body = mcp_call(agent_port, "mcp__a2ext__toolkit_plan",
                                        {"op": "start", "goal": "look at two things",
                                         "workspace_id": wid_a})
        check("a second plan right away is refused", "冷却" in output, output[:300])

        # ---- a read-write step runs alone -----------------------------------
        before = len(subtask_records())
        status, output, body = mcp_call(agent_port, "mcp__a2ext__toolkit_plan",
                                        {"op": "start", "goal": "MIXED look at two things",
                                         "workspace_id": wid_b})
        check("the mixed plan starts", "编排已经开始了" in output, output[:300])

        def mixed_done():
            records = subtask_records()[before:]
            if len(records) < 2:
                return None
            if any(r["finished"] is None for r in records):
                return None
            return records

        subs = wait_for(mixed_done, timeout=90.0)
        check("both steps of the mixed plan ran", subs is not None and len(subs) >= 2,
              len(MockProvider.records))
        if subs and len(subs) >= 2:
            check("the read-write step did not overlap with the read-only one",
                  not overlaps(subs[0], subs[1]),
                  ("before=%d " % before) +
                  str([(r["kind"], r["last_user"][:24], r["started"], r["finished"])
                       for r in MockProvider.records]))

        # ---- the planner is asked for what the tool allows ----------------
        with MockProvider.lock:
            plan_requests = [r["last_user"] for r in MockProvider.records
                             if r["kind"] == "plan"]
        check("the planner is asked for at most the steps the tool allows",
              any("拆成不超过 4 个子任务" in one for one in plan_requests),
              [one[:80] for one in plan_requests[:2]])

        # ---- a step that waits for a failed step is skipped, not waited for -
        status, made = call(agent_port, "POST", "/api/workspaces",
                            {"path": ws_c, "name": "ws_c"})
        wid_c = made.get("id") or made.get("workspace", {}).get("id") or ""
        check("a third workspace is registered", bool(wid_c), made)

        def status_of(pid):
            _, text_out, _ = mcp_call(agent_port, "mcp__a2ext__toolkit_plan",
                                      {"op": "status", "plan_id": pid})
            return text_out

        began = time.time()
        status, output, body = mcp_call(agent_port, "mcp__a2ext__toolkit_plan",
                                        {"op": "start", "goal": "FAILFIRST do it",
                                         "workspace_id": wid_c})
        match = re.search(r"(plan_[0-9a-f]+)", output or "")
        plan_c = match.group(1) if match else ""
        check("a plan whose first step will fail starts", bool(plan_c), output[:300])
        # The budget is 300 s per step: if the scheduler waited for the step
        # that can never run, this would take ten minutes (and burn a core).
        def plan_over(text):
            # "done" or "interrupted" - anything but still running. The
            # summary text alone is not enough: "跑完了这里会有汇总" says 汇总.
            return text if ("状态：完成" in text or "出错了" in text) else None

        final = wait_for(lambda: plan_over(status_of(plan_c)),
                         timeout=90.0) if plan_c else None
        took = time.time() - began
        check("a plan whose step failed still ends", final is not None, final)
        check("it ends without waiting for the deadline", took < 60.0, took)
        check("the step that waited for the failed one is marked skipped",
              "skipped" in (final or ""), final)
        check("the summary is told which step did not run",
              final is not None and "没有完成" in final, final)

        # ---- a summary that cannot be made says so ------------------------
        status, made = call(agent_port, "POST", "/api/workspaces",
                            {"path": ws_d, "name": "ws_d"})
        wid_d = made.get("id") or made.get("workspace", {}).get("id") or ""
        check("a fourth workspace is registered", bool(wid_d), made)
        status, output, body = mcp_call(agent_port, "mcp__a2ext__toolkit_plan",
                                        {"op": "start", "goal": "SUMMARIZE-FAIL look",
                                         "workspace_id": wid_d})
        match = re.search(r"(plan_[0-9a-f]+)", output or "")
        plan_d = match.group(1) if match else ""
        check("the plan for the failing summary starts", bool(plan_d), output[:300])
        final = wait_for(
            lambda: (lambda t: t if "出错了" in t else None)(status_of(plan_d)),
            timeout=90.0) if plan_d else None
        check("a summary that failed is reported to the caller", final is not None, final)
        check("the plan still says the step itself ran",
              final is not None and "done" in final, final)

        # ---- a model can call the tool in a turn ----------------------------
        events = run_turn(agent_port, wid_a, "SCENARIO=plan")
        results = tool_results(events)
        model_call = None
        for one in results:
            if one.get("name") == "mcp__a2ext__toolkit_plan":
                model_call = one
        check("the model can call the tool", model_call is not None, results)
        check("the model sees the tool's answer",
              model_call is not None and "没有这个计划" in (model_call.get("output") or ""),
              (model_call or {}).get("output"))
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
