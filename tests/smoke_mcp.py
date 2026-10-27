#!/usr/bin/env python3
"""Smoke test for the MCP client (the `mcp:` section of provider.yaml).

The chain is:

    provider.yaml (mcp: servers) -> the agent starts the server
        -> JSON-RPC over stdio (initialize / tools/list / tools/call)
        -> the server's tools join the agent's tool set
        -> the model calls one -> the answer comes back to the model

Every link can break on its own, so the test pins each of them:

  * without an `mcp:` section nothing is advertised and the routes say so;
  * a server that cannot be started is reported with a reason, and the turn
    still works (the other servers' tools are still there);
  * the tools of a running server appear in the tool list;
  * a tool can be called by hand through the API;
  * a model that calls an MCP tool gets the server's answer back;
  * an MCP server that returns an image has that image attached to the
    conversation;
  * "refresh" restarts the servers.

Usage:  python3 tests/smoke_mcp.py [path-to-tea_agent-binary]
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
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MOCK_MCP = os.path.join(REPO_ROOT, "tests", "mock_mcp_server.py")
MOCK_LLM = os.path.join(REPO_ROOT, "tools", "mock_llm_server.mjs")


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


def call(port, method, path, body=None, timeout=90):
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


def run_turn(port, workspace_id, session_id, text):
    body = {"workspace_id": workspace_id, "session_id": session_id, "message": text}
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


def event_text(events, kind):
    out = []
    for e in events:
        if e.get("type") == kind:
            content = e.get("content")
            if isinstance(content, str):
                out.append(content)
    return "\n".join(out)


def main():
    binary = (sys.argv[1] if len(sys.argv) > 1
              else os.path.join(REPO_ROOT, "build", "bin", "tea_agent"))
    if not os.path.exists(binary):
        print("binary not found:", binary)
        return 1
    if not os.path.exists(MOCK_MCP):
        print("mock MCP server not found:", MOCK_MCP)
        return 1

    tmp = tempfile.mkdtemp(prefix="tea_mcp_")
    home = os.path.join(tmp, "home")
    workspace = os.path.join(tmp, "ws")
    os.makedirs(home)
    os.makedirs(workspace)

    failures = []
    checks = []

    def check(name, ok, detail=""):
        checks.append(name)
        print(("PASS " if ok else "FAIL ") + name + (("  " + str(detail)) if not ok else ""))
        if not ok:
            failures.append(name)

    llm_port = free_port()
    llm = subprocess.Popen(["node", MOCK_LLM, "--port", str(llm_port)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    agent = None
    try:
        def write_config(mcp_section=""):
            text = """version: 1
providers:
  mock:
    api_url: http://127.0.0.1:%d/v1
    api_key: sk-test
    default_model: mock-model
    models:
      mock-model: {}
""" % llm_port
            if mcp_section:
                text += mcp_section
            with open(os.path.join(home, "provider.yaml"), "w", encoding="utf-8") as f:
                f.write(text)

        def reload_config():
            """The agent reads provider.yaml when it is told to; the file on
            disk is the truth, the running config is a copy of it."""
            return call(port, "POST", "/api/config/reload")

        # ---- no mcp: section ----------------------------------------------
        write_config("")
        port = free_port()
        agent = subprocess.Popen([
            binary, "--tea-home", home, "--host", "127.0.0.1", "--port", str(port),
            "--no-console", "--log-file", os.path.join(home, "logs", "tea_agent.log"),
            "--workspace", workspace,
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not wait_health(port):
            print("server did not become healthy")
            return 1

        status, body = call(port, "GET", "/api/mcp/servers")
        check("without an mcp: section the status says it is not configured",
              status == 200 and body.get("configured") is False, (status, body))
        check("without an mcp: section there are no servers",
              body.get("servers") == [], body)

        status, tools = call(port, "GET", "/api/tools")
        names = [t.get("name") for t in (tools.get("tools") or [])]
        check("without an mcp: section no MCP tool is advertised",
              not any(str(n).startswith("mcp__") for n in names), names)

        # ---- an mcp: section ----------------------------------------------
        # The agent starts the mock server itself: the command is the
        # interpreter, the script is the first argument.
        write_config("""mcp:
  servers:
    mock:
      command: "%s"
      args: ["%s"]
""" % (sys.executable, MOCK_MCP))
        status, body = reload_config()
        check("a config with an mcp: section loads", status == 200, (status, body))

        status, body = call(port, "POST", "/api/mcp/refresh")
        check("refresh answers with the state of every server", status == 200, (status, body))
        servers = body.get("servers") or []
        check("the configured server is listed", len(servers) == 1 and
              servers[0].get("name") == "mock", servers)
        server = servers[0] if servers else {}
        check("the server is connected", server.get("connected") is True, server)
        check("the server offers its three tools", server.get("tool_count") == 3, server)
        tool_names = [t.get("name") for t in (server.get("tools") or [])]
        check("the tool names are the ones the server announced",
              tool_names == ["echo", "add", "picture"], tool_names)
        check("the protocol version is reported",
              server.get("protocol_version") == "2024-11-05", server)
        check("the server says who it is",
              server.get("server_name") == "mock-mcp", server)

        status, tools = call(port, "GET", "/api/tools")
        names = [t.get("name") for t in (tools.get("tools") or [])]
        check("the MCP tools are advertised with the mcp__<server>__<tool> name",
              "mcp__mock__echo" in names, names)

        # ---- calling a tool by hand ---------------------------------------
        status, body = call(port, "POST", "/api/mcp/servers/mock/call",
                               {"tool": "mcp__mock__echo", "arguments": {"text": "ping"}})
        check("a tool can be called by hand",
              status == 200 and "MOCK-ECHO: ping" in (body.get("text") or ""), (status, body))

        status, body = call(port, "POST", "/api/mcp/servers/mock/call",
                               {"tool": "mcp__mock__add", "arguments": {"a": 2, "b": 40}})
        check("the arguments reach the server",
              status == 200 and "MOCK-SUM: 42" in (body.get("text") or ""), (status, body))

        status, body = call(port, "POST", "/api/mcp/servers/mock/call",
                               {"tool": "mcp__mock__nope", "arguments": {}})
        check("calling a tool that does not exist is refused with a reason",
              status in (400, 502) and body.get("error"), (status, body))

        # ---- a model that calls an MCP tool -------------------------------
        status, body = call(port, "GET", "/api/workspaces")
        workspace_id = (body.get("workspaces") or [{}])[0].get("id")
        events = run_turn(port, workspace_id, "", "please use the MCP tool")
        answer = event_text(events, "done")
        tool_events = [e for e in events if e.get("type") == "tool_call"]
        check("the model called the MCP tool",
              any(str(e.get("name", "")).startswith("mcp__") for e in tool_events),
              [e.get("name") for e in tool_events])
        check("the answer of the MCP server reached the model",
              "MOCK-ECHO" in answer, answer[:400])

        # ---- an MCP tool that returns an image ----------------------------
        status, body = call(port, "POST", "/api/mcp/servers/mock/call",
                               {"tool": "mcp__mock__picture", "arguments": {}})
        check("an image answer is reported as an image",
              status == 200 and body.get("images") == 1, (status, body))

        # ---- a server that cannot be started ------------------------------
        write_config("""mcp:
  servers:
    mock:
      command: "%s"
      args: ["%s"]
    broken:
      command: /nonexistent/mcp-server-that-is-not-there
""" % (sys.executable, MOCK_MCP))
        status, body = reload_config()
        check("the config with a broken server still loads", status == 200, (status, body))

        status, body = call(port, "POST", "/api/mcp/refresh")
        servers = body.get("servers") or []
        broken = [s for s in servers if s.get("name") == "broken"]
        good = [s for s in servers if s.get("name") == "mock"]
        check("the broken server is reported, not hidden", len(broken) == 1, servers)
        check("the broken server says why",
              bool(broken and broken[0].get("error")), broken)
        check("the broken server is not connected",
              broken and broken[0].get("connected") is False, broken)
        check("the good server still works", good and good[0].get("connected") is True, good)

        status, tools = call(port, "GET", "/api/tools")
        names = [t.get("name") for t in (tools.get("tools") or [])]
        check("the tools of the working server are still advertised",
              "mcp__mock__echo" in names, names)

        # ---- a turn with a broken server in the list -----------------------
        events = run_turn(port, workspace_id, "", "please use the MCP tool again")
        answer = event_text(events, "done")
        check("the turn still works with a broken server configured",
              "MOCK-ECHO" in answer, answer[:400])

        # ---- a server that answers nonsense -------------------------------
        write_config("""mcp:
  servers:
    mock:
      command: "%s"
      args: ["%s", "--garbage"]
""" % (sys.executable, MOCK_MCP))
        status, body = reload_config()
        check("a config with a nonsense server still loads", status == 200, (status, body))
        status, body = call(port, "POST", "/api/mcp/refresh")
        servers = body.get("servers") or []
        mock = [s for s in servers if s.get("name") == "mock"]
        check("a server that answers nonsense is reported as broken",
              mock and mock[0].get("connected") is False, mock)
        check("the reason is in the status",
              mock and bool(mock[0].get("error")), mock)

    finally:
        if agent is not None:
            agent.terminate()
            try:
                agent.wait(timeout=10)
            except Exception:
                agent.kill()
        llm.terminate()
        try:
            llm.wait(timeout=10)
        except Exception:
            llm.kill()
        shutil.rmtree(tmp, ignore_errors=True)

    print("")
    print("%d/%d checks passed" % (len(checks) - len(failures), len(checks)))
    if failures:
        for name in failures:
            print("  failed:", name)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
