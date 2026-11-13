#!/usr/bin/env python3
"""Smoke test for the workspace roles (the stage profiles).

A role is a named way of working: a persona, the rules of the stage and the
boundaries it runs under (tools, command fence, model, budget). The files live
in `<workspace>/.tea_agent_run/roles/<id>.yaml`; the *current* role is a
pointer in the workspace's settings.yaml.

What this pins:

  * the roles directory exists with the workspace, and the API lists, writes,
    reads and deletes the role files;
  * a role file that does not parse is reported without taking the other
    roles down;
  * switching the role (POST /role) points the workspace at it, and the
    switch reaches the **next** turn - a running turn keeps its role;
  * the role's persona, its stage rules and its fences are in the system
    prompt of the turn that runs under it;
  * the role only tightens: a command its fence denies does not run, and a
    tool it does not allow is refused;
  * a request may name a role for one turn only, and may not name one that
    does not exist.

Usage: python3 tests/smoke_role.py [path-to-tea_agent-binary]
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

DEV_ROLE = """name: 开发
description: 写代码、跑测试
system_prompt: |
  你是本项目的开发者：先读代码，再改代码。
checklist: |
  改代码前先跑一次现有测试。
tools:
  - toolkit_exec
  - toolkit_file
exec:
  deny:
    - "git push *"
"""

QA_ROLE = """name: 测试
description: 只读代码、跑测试
checklist: |
  不要改任何文件。
tools:
  - toolkit_exec
"""


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

        if tool_msgs:
            self._content("done")
            return
        last_user = ""
        for m in reversed(payload.get("messages") or []):
            if m.get("role") == "user":
                last_user = m.get("content") or ""
                break
        if "SCENARIO=exec_denied" in last_user:
            self._tool_call("toolkit_exec", {"command": "git push origin main"}, "call_exec")
        elif "SCENARIO=file_write" in last_user:
            self._tool_call("toolkit_file",
                            {"op": "write", "path": "notes.txt", "content": "written"},
                            "call_write")
        elif "SCENARIO=mcp_call" in last_user:
            self._tool_call("mcp__mock__echo", {"text": "hi"}, "call_mcp")
        elif "SCENARIO=exec_ok" in last_user:
            self._tool_call("toolkit_exec", {"command": "echo hello"}, "call_exec")
        elif "SCENARIO=tool_denied" in last_user:
            self._tool_call("toolkit_memory", {"op": "read"}, "call_mem")
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


def call(port, method, path, body=None, token=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    headers = {"Content-Type": "application/json"} if body is not None else {}
    if token:
        headers["Authorization"] = "Bearer " + token
    conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
    resp = conn.getresponse()
    raw = resp.read()
    try:
        return resp.status, json.loads(raw or b"null")
    except ValueError:
        return resp.status, {"raw": raw.decode("utf-8", "replace")}


def run_turn(port, workspace_id, scenario, extra=None):
    """One full SSE turn; returns the events."""
    body = {"workspace_id": workspace_id, "message": "SCENARIO=" + scenario}
    if extra:
        body.update(extra)
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


def tool_names(request):
    return [t.get("function", {}).get("name") for t in (request.get("tools") or [])]


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

    tmp = tempfile.mkdtemp(prefix="tea_agentrole_")
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

    # An MCP server, so the "mcp__*" family in a role's tool list can be seen
    # at work (the mock server offers echo/add/picture). The MCP file lives
    # next to provider.yaml.
    with open(os.path.join(tmp, "mcp.yaml"), "w", encoding="utf-8") as f:
        f.write("""servers:
  mock:
    command: '%s'
    args: ['%s']
""" % (sys.executable, os.path.join(REPO_ROOT, "tests", "mock_mcp_server.py")))

    proc = subprocess.Popen([
        binary, "--config", config_path, "--tea-home", os.path.join(tmp, "home"),
        "--host", "127.0.0.1", "--port", str(agent_port), "--no-console",
        "--log-file", os.path.join(tmp, "tea_agent.log"), "--workspace", ws_dir,
        # The peer channel: another a2 drives this machine over it, so the
        # role switch has to be reachable there too.
        "--api-token", "role-test-token",
    ])
    failures = []
    checks = []

    def check(name, ok, detail=""):
        checks.append(name)
        print(("PASS " if ok else "FAIL ") + name + (("  " + str(detail)) if not ok else ""))
        if not ok:
            failures.append(name)

    run_dir = os.path.join(ws_dir, ".tea_agent_run")
    roles_dir = os.path.join(run_dir, "roles")

    try:
        if not wait_health(agent_port):
            print("server did not become healthy")
            return 1
        _, listing = call(agent_port, "GET", "/api/workspaces")
        wid = listing["workspaces"][0]["id"]
        base = "/api/workspaces/" + wid

        # ---- the directory exists with the workspace ------------------------
        check("the roles directory exists", os.path.isdir(roles_dir), roles_dir)

        # ---- the roles that ship with the agent ------------------------------
        # They are templates in the program: the workspace holds no file for
        # them until one of them is copied.
        seeded = sorted(os.listdir(roles_dir)) if os.path.isdir(roles_dir) else []
        check("the built-in roles are not files of the workspace", seeded == [], seeded)
        status, info = call(agent_port, "GET", base + "/roles")
        by_id = {r["id"]: r for r in (info.get("roles") or [])}
        check("all five built-in roles are listed", len(by_id) == 5, sorted(by_id))
        check("the listed roles are the built-in templates",
              all(r.get("builtin") is True for r in (info.get("roles") or [])),
              [(r.get("id"), r.get("builtin")) for r in (info.get("roles") or [])])
        check("the workspace has no role files yet", info.get("files") == [], info.get("files"))
        check("the roles carry their Chinese display names",
              by_id.get("analysis", {}).get("name") == "需求分析" and
              by_id.get("ops", {}).get("name") == "运维",
              [r.get("name") for r in (info.get("roles") or [])])
        check("the analysis stage is read-only",
              by_id.get("analysis", {}).get("read_only") is True,
              by_id.get("analysis", {}).get("read_only"))
        check("the other stages may write",
              all(by_id.get(k, {}).get("read_only") is False
                  for k in ("design", "dev", "test", "ops")),
              [k for k in ("design", "dev", "test", "ops")
               if by_id.get(k, {}).get("read_only") is not False])
        # A checklist that names a tool a2 does not have is a checklist the
        # model cannot follow: every name must be a real tool (or a family).
        known = {"toolkit_exec", "toolkit_file", "toolkit_workspace_search",
                 "toolkit_history_search", "toolkit_memory", "toolkit_todo",
                 "toolkit_task", "toolkit_load_image", "toolkit_agent_doc",
                 "toolkit_search", "toolkit_fetch", "toolkit_peer", "toolkit_fleet",
                 "toolkit_phone"}
        unknown = []
        for role in (info.get("roles") or []):
            for name in (role.get("tools") or []):
                if name in known or name.endswith("*"):
                    continue
                unknown.append(role.get("id") + ":" + name)
        check("every tool the roles name really exists", not unknown, unknown)
        check("the built-in roles fence the destructive commands",
              "git push*" in (by_id.get("analysis", {}).get("exec") or {}).get("deny", []),
              by_id.get("analysis", {}).get("exec"))

        # ---- copying a built-in role -----------------------------------------
        # The built-in is a template: the way to get your own version of it is
        # to copy it, and the copy is a normal, writable role on disk.
        status, body = call(agent_port, "POST", base + "/roles/analysis/copy",
                            {"as": "my-analysis"})
        check("a built-in role can be copied", status == 200 and
              body.get("role", {}).get("id") == "my-analysis", (status, body))
        copy_path = os.path.join(roles_dir, "my-analysis.yaml")
        check("the copy is a file of the workspace", os.path.isfile(copy_path), copy_path)
        check("the copy has its own name",
              body.get("role", {}).get("name") == "需求分析（副本）",
              body.get("role", {}).get("name"))
        check("the copy is not a built-in any more",
              body.get("role", {}).get("builtin") is False,
              body.get("role", {}).get("builtin"))
        copy_text = open(copy_path, encoding="utf-8").read()
        check("the copy keeps the stage's rules",
              "工具/能力" in copy_text and "system_prompt" in copy_text, copy_text[:120])
        # The copy is writable: the API serves it and the workspace's own
        # version is what the next turn sees.
        status, body = call(agent_port, "GET", base + "/roles/my-analysis")
        check("the copy can be read back",
              status == 200 and body.get("role", {}).get("id") == "my-analysis" and
              body.get("has_file") is True, (status, body))
        status, body = call(agent_port, "PUT", base + "/roles/my-analysis",
                            {"content": "name: 我自己的需求分析\ndescription: 改过的\n"})
        check("the copy can be edited", status == 200 and
              body.get("role", {}).get("name") == "我自己的需求分析", (status, body))
        status, body = call(agent_port, "GET", base + "/roles")
        ids = [r.get("id") for r in (body.get("roles") or [])]
        check("the copy is in the list", "my-analysis" in ids, ids)
        # The built-in itself is still the template.
        status, body = call(agent_port, "GET", base + "/roles/analysis")
        check("the built-in is still there",
              status == 200 and body.get("role", {}).get("name") == "需求分析" and
              body.get("has_file") is False, (status, body))
        # It cannot be deleted: it is part of the program, not a file.
        status, body = call(agent_port, "DELETE", base + "/roles/analysis")
        check("a built-in role cannot be deleted", status == 404, (status, body))
        # The copy takes the pristine template, not somebody's edit.
        status, body = call(agent_port, "POST", base + "/roles/analysis/copy", {})
        check("a copy without a name gets one",
              status == 200 and body.get("role", {}).get("id") == "analysis-copy",
              (status, body))
        fresh = body.get("content") or ""
        check("the copy of a built-in is the pristine template",
              "改过的" not in fresh and "需求分析" in fresh, fresh[:120])

        # ---- an empty workspace has no roles -------------------------------
        status, info = call(agent_port, "GET", base + "/roles")
        check("the roles API answers", status == 200, status)
        check("the workspace has the built-in roles",
              len(info.get("roles") or []) >= 5, len(info.get("roles") or []))
        check("no role is current", info.get("current_id") == "", info.get("current_id"))

        # ---- writing a role -------------------------------------------------
        status, body = call(agent_port, "PUT", base + "/roles/dev", {"content": DEV_ROLE})
        check("the role is written", status == 200, body)
        path = os.path.join(roles_dir, "dev.yaml")
        check("the role file is on disk", os.path.isfile(path), path)
        status, info = call(agent_port, "GET", base + "/roles")
        roles = [r for r in (info.get("roles") or []) if r["id"] == "dev"]
        check("the role is listed", len(roles) == 1, roles)
        if roles:
            one = roles[0]
            check("the display name is read", one["name"] == "开发", one["name"])
            check("the description is read", one["description"] == "写代码、跑测试", one["description"])
            check("the checklist is read", "跑一次现有测试" in (one["checklist"] or ""),
                  one["checklist"])
            check("the tool list is read", one["tools"] == ["toolkit_exec", "toolkit_file"],
                  one["tools"])
            check("the fence is read", one["exec"]["deny"] == ["git push *"], one["exec"])

        # ---- a broken role file is reported, the others survive -------------
        with open(os.path.join(roles_dir, "broken.yaml"), "w", encoding="utf-8") as f:
            f.write("name: [unclosed\n")
        status, info = call(agent_port, "GET", base + "/roles")
        check("a broken role file is reported", len(info.get("errors") or []) == 1,
              info.get("errors"))
        check("the good roles are still listed",
              len(info.get("roles") or []) >= 5, len(info.get("roles") or []))

        # ---- switching the role ---------------------------------------------
        status, body = call(agent_port, "POST", base + "/role", {"role": "dev"})
        check("the role is switched", status == 200 and body.get("current_id") == "dev", body)
        settings_text = open(os.path.join(run_dir, "settings.yaml"), encoding="utf-8").read()
        check("the pointer is in settings.yaml", "role" in settings_text and "dev" in settings_text,
              settings_text)
        status, body = call(agent_port, "POST", base + "/role", {"role": "nope"})
        check("an unknown role is refused", status == 400, (status, body))

        # ---- the turn runs under the role ------------------------------------
        before = len(MockProvider.records)
        events = run_turn(agent_port, wid, "plain")
        check("the turn finished", any(e.get("type") == "done" for e in events), events[-2:])
        prompt = system_prompt(MockProvider.records[before])
        check("the prompt carries the role", "Current role: 开发 (dev)" in prompt, prompt[:300])
        check("the role's persona is the prompt's first block",
              prompt.startswith("你是本项目的开发者"), prompt[:80])
        check("the stage rules are in the prompt", "Rules of this stage:" in prompt
              and "跑一次现有测试" in prompt, prompt[:400])
        check("the fence is in the prompt", "git push *" in prompt, prompt[:400])
        start = [e for e in events if e.get("type") == "start"]
        check("the start event names the role", start and start[0].get("role") == "dev",
              start[:1])

        # ---- the role's tools are the ones the model sees --------------------
        names = tool_names(MockProvider.records[before])
        check("the role narrows the tool list",
              "toolkit_memory" not in names and "toolkit_exec" in names, names)

        # ---- the fence refuses the command ----------------------------------
        events = run_turn(agent_port, wid, "exec_denied")
        results = tool_results(events)
        out = (results.get("toolkit_exec") or {}).get("output") or ""
        check("the fence refuses the command", "fence" in out or "forbids" in out, out[:200])

        # ---- a read-only stage cannot write ----------------------------------
        call(agent_port, "POST", base + "/role", {"role": "analysis"})
        events = run_turn(agent_port, wid, "file_write")
        results = tool_results(events)
        out = (results.get("toolkit_file") or {}).get("output") or ""
        check("a read-only stage may not write a file", "read-only" in out, out[:200])
        check("the file was not created",
              not os.path.exists(os.path.join(ws_dir, "notes.txt")), ws_dir)

        # ---- the analysis stage fences the destructive commands ---------------
        events = run_turn(agent_port, wid, "exec_denied")
        results = tool_results(events)
        out = (results.get("toolkit_exec") or {}).get("output") or ""
        check("the stage fence refuses git push", "fence" in out or "forbids" in out, out[:200])
        call(agent_port, "POST", base + "/role", {"role": "dev"})

        # ---- a tool outside the role is refused ------------------------------
        events = run_turn(agent_port, wid, "tool_denied")
        results = tool_results(events)
        out = (results.get("toolkit_memory") or {}).get("output") or ""
        check("the tool outside the role is refused", "may not use the tool" in out, out[:200])

        # ---- a command the fence allows still runs ---------------------------
        events = run_turn(agent_port, wid, "exec_ok")
        results = tool_results(events)
        out = (results.get("toolkit_exec") or {}).get("output") or ""
        check("an allowed command still runs", "hello" in out, out[:200])

        # ---- a wildcard names a family of tools ------------------------------
        status, body = call(agent_port, "PUT", base + "/roles/family", {"content":
            "name: 家族\ntools:\n  - \"mcp__*\"\n"})
        check("a role with a family is written", status == 200, body)
        call(agent_port, "POST", base + "/role", {"role": "family"})
        before = len(MockProvider.records)
        events = run_turn(agent_port, wid, "plain")
        names = tool_names(MockProvider.records[before])
        check("the family brings the MCP tools along",
              any(n and n.startswith("mcp__mock__") for n in names), names)
        check("the family leaves the built-in tools out",
              "toolkit_exec" not in names, names)
        call(agent_port, "DELETE", base + "/roles/family")

        # ---- the switch reaches the next turn -------------------------------
        status, body = call(agent_port, "PUT", base + "/roles/qa", {"content": QA_ROLE})
        check("the second role is written", status == 200, body)
        status, body = call(agent_port, "POST", base + "/role", {"role": "qa"})
        check("the role is switched to the second one",
              status == 200 and body.get("current_id") == "qa", body)
        before = len(MockProvider.records)
        events = run_turn(agent_port, wid, "plain")
        prompt = system_prompt(MockProvider.records[before])
        check("the next turn runs under the new role", "Current role: 测试 (qa)" in prompt,
              prompt[:300])
        check("the new role's rules are in the prompt", "不要改任何文件" in prompt, prompt[:400])

        # ---- a request may name a role for one turn only ---------------------
        call(agent_port, "POST", base + "/role", {"role": "dev"})
        before = len(MockProvider.records)
        events = run_turn(agent_port, wid, "plain", {"role": "qa"})
        prompt = system_prompt(MockProvider.records[before])
        check("the request's role wins for that turn", "Current role: 测试 (qa)" in prompt,
              prompt[:300])
        _, info = call(agent_port, "GET", base + "/roles")
        check("the one-turn role did not move the pointer", info.get("current_id") == "dev",
              info.get("current_id"))
        before = len(MockProvider.records)
        events = run_turn(agent_port, wid, "plain")
        prompt = system_prompt(MockProvider.records[before])
        check("the next turn is back to the workspace's role", "Current role: 开发 (dev)" in prompt,
              prompt[:300])

        # ---- an unknown role in a request is refused -------------------------
        # The turn is refused before it starts, so the answer is a plain 400
        # (not an event stream).
        status, body = call(agent_port, "POST", "/api/chat",
                            {"workspace_id": wid, "message": "SCENARIO=plain", "role": "ghost"})
        check("a request may not name a role that does not exist", status == 400, (status, body))
        check("the refusal says which role", "ghost" in json.dumps(body), body)

        # ---- the prompt preview shows the role --------------------------------
        status, preview = call(agent_port, "GET", base + "/prompt")
        check("the preview carries the role", preview.get("role_id") == "dev", preview.get("role_id"))
        check("the preview shows the role block", "Current role: 开发 (dev)" in
              (preview.get("system_prompt") or ""), (preview.get("system_prompt") or "")[:200])

        # ---- the peer channel may switch the role too ------------------------
        # The operator may be another a2 driving this machine; the agent
        # itself may not switch.
        status, body = call(agent_port, "POST", base + "/role", {"role": "qa"})
        check("the role is switched again", status == 200, body)
        peer_base = "/api/peer" + base[len("/api"):]
        status, body = call(agent_port, "POST", peer_base + "/role", {"role": "dev"},
                            token="role-test-token")
        check("another a2 may switch the role over the peer channel",
              status == 200 and body.get("current_id") == "dev", (status, body))
        status, body = call(agent_port, "GET", peer_base + "/roles")
        check("the peer channel needs the token", status == 401, (status, body))
        status, body = call(agent_port, "GET", peer_base + "/roles",
                            token="role-test-token")
        check("the peer channel may read the roles", status == 200, (status, body))

        # ---- deleting a role clears the pointer -------------------------------
        status, body = call(agent_port, "DELETE", base + "/roles/dev")
        check("the role is deleted", status == 200, body)
        check("the file is gone", not os.path.isfile(path), path)
        check("the pointer was cleared", body.get("current_id") == "", body.get("current_id"))
        before = len(MockProvider.records)
        events = run_turn(agent_port, wid, "plain")
        prompt = system_prompt(MockProvider.records[before])
        check("a turn without a role has no role block", "Current role:" not in prompt,
              prompt[:200])

        # ---- clearing the role by hand ----------------------------------------
        status, body = call(agent_port, "POST", base + "/role", {"role": ""})
        check("the role can be cleared", status == 200 and body.get("current_id") == "", body)

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        mock.shutdown()

    print("")
    print("%d checks, %d failed" % (len(checks), len(failures)))
    if failures:
        for name in failures:
            print("  FAILED: " + name)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
