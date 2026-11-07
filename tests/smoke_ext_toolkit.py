#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Smoke test for the extension layer: a2's tools grow at runtime.

The extension layer (`sched/a2_ext_py/toolkit/`) is an MCP server: a2 starts
it, and the model can save a tool, reload it and **use it in the same turn**.

What this pins:

  * the MCP server is started by a2 (from mcp.yaml) and its tools are
    advertised to the model;
  * the model saves a tool: the file lands in the module directory (outside
    the workspace) and the save is audited;
  * after a reload the new tool is in the tool list - **in the same turn**:
    the agent asks the MCP servers what they can do again before every
    iteration, so a tool that appeared a moment ago is usable;
  * the new tool really runs and its answer comes back to the model;
  * a module that does not fit is reported, not silently skipped.

Usage: python3 tests/smoke_ext_toolkit.py [path-to-tea_agent-binary]
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
    from socketserver import MixIn

    class ThreadingHTTPServer(HTTPServer):
        """The 3.6 spelling of http.server.ThreadingHTTPServer."""
        daemon_threads = True

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXT_SERVER = os.path.join(REPO_ROOT, "sched", "a2_ext_py", "toolkit", "mcp_server.py")

# The tool the model writes for itself.
HELLO_CODE = (
    "def toolkit_hello(who=\"world\"):\n"
    "    \"Say hello.\"\n"
    "    return \"hello, \" + who\n"
)
HELLO_META = {
    "name": "toolkit_hello",
    "description": "Say hello to somebody.",
    "inputSchema": {"type": "object",
                    "properties": {"who": {"type": "string", "description": "whom"}},
                    "required": ["who"]},
}


class MockProvider(BaseHTTPRequestHandler):
    """A model that writes a tool, reloads it and then uses it."""

    def log_message(self, *args):
        pass

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"{}")

    def _chunk(self, payload):
        try:
            self.wfile.write(("data: " + json.dumps(payload) + "\n\n").encode())
            self.wfile.flush()
        except OSError:
            pass

    def _tool_call(self, cid, name, arguments):
        self._chunk({
            "id": "c",
            "choices": [{
                "index": 0,
                "delta": {
                    "role": "assistant",
                    "tool_calls": [{
                        "index": 0, "id": cid, "type": "function",
                        "function": {"name": name,
                                     "arguments": json.dumps(arguments)},
                    }],
                },
                "finish_reason": None,
            }],
        })
        self._chunk({"id": "c", "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]})

    def _text(self, text):
        self._chunk({"id": "c", "choices": [{"index": 0, "delta": {"content": text},
                                             "finish_reason": None}]})
        self._chunk({"id": "c", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})

    def do_POST(self):
        if not self.path.endswith("/chat/completions"):
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        payload = self._read_json()
        msgs = payload.get("messages") or []
        answers = [m for m in msgs if m.get("role") == "tool"]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()

        if len(answers) == 0:
            # 1) write the tool
            self._tool_call("c0", "mcp__a2ext__toolkit_save",
                            {"name": "toolkit_hello", "meta": HELLO_META, "pycode": HELLO_CODE})
            return
        if len(answers) == 1:
            # 2) make it usable
            self._tool_call("c1", "mcp__a2ext__toolkit_reload", {})
            return
        if len(answers) == 2:
            # 3) use it - this only works if the tool list was refreshed
            self._tool_call("c2", "mcp__a2ext__toolkit_hello", {"who": "tea_a2"})
            return
        # 4) report what the tool said
        self._text("the tool said: " + str(answers[-1].get("content") or ""))


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
    payload = None
    headers = {}
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    conn.request(method, path, body=payload, headers=headers)
    resp = conn.getresponse()
    raw = resp.read()
    try:
        return resp.status, json.loads(raw or b"null")
    except ValueError:
        return resp.status, {"raw": raw.decode("utf-8", "replace")}


def run_turn(port, workspace_id, session_id, message="go", timeout=120):
    body = {"workspace_id": workspace_id, "session_id": session_id, "message": message}
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
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


def main():
    binary = (sys.argv[1] if len(sys.argv) > 1
              else os.path.join(REPO_ROOT, "build", "bin", "tea_agent"))
    if not os.path.exists(binary):
        print("binary not found:", binary)
        return 1
    if not os.path.exists(EXT_SERVER):
        print("the extension server is not there:", EXT_SERVER)
        return 1

    mock = ThreadingHTTPServer(("127.0.0.1", 0), MockProvider)
    mock.daemon_threads = True
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    mock_port = mock.server_address[1]

    failures = []
    checks = []

    def check(name, ok, detail=""):
        checks.append(name)
        print(("PASS " if ok else "FAIL ") + name + (("  " + str(detail)) if not ok else ""))
        if not ok:
            failures.append(name)

    tmp = tempfile.mkdtemp(prefix="tea_ext_")
    home = os.path.join(tmp, "home")
    ws_dir = os.path.join(tmp, "ws")
    modules = os.path.join(tmp, "ext")
    os.makedirs(home)
    os.makedirs(ws_dir)
    os.makedirs(modules)
    port = free_port()

    config_path = os.path.join(home, "provider.yaml")
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
        supports_tools: true
""" % mock_port)
    # The MCP server the agent starts: the extension layer itself.
    # The paths are quoted with single quotes: in YAML a double quoted
    # scalar processes backslash escapes, and a Windows path ("C:\\Users\\...")
    # is not a valid escape - yaml-cpp then refuses the whole file and the
    # MCP configuration is dropped *silently* (the file is not found, the
    # servers are never started).
    with open(os.path.join(home, "mcp.yaml"), "w", encoding="utf-8") as f:
        f.write("""servers:
  a2ext:
    command: '%s'
    args: ['%s', '--modules', '%s', '--agent-url', 'http://127.0.0.1:%d']
""" % (sys.executable, EXT_SERVER, modules, port))

    proc = subprocess.Popen([
        binary, "--tea-home", home, "--host", "127.0.0.1", "--port", str(port),
        "--no-console", "--log-file", os.path.join(home, "logs", "tea_agent.log"),
        "--workspace", ws_dir,
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    try:
        if not wait_health(port):
            print("the agent did not become healthy")
            return 1

        # ---- the MCP server is up and its tools are advertised --------------
        _, status = call(port, "GET", "/api/mcp/servers")
        servers = status.get("servers") or []
        check("the extension server is configured",
              any(s.get("name") == "a2ext" for s in servers), servers)
        _, tools = call(port, "GET", "/api/tools")
        names = [t.get("name") for t in (tools.get("tools") or [])]
        check("the save tool is advertised",
              "mcp__a2ext__toolkit_save" in names, names[:20])

        _, listing = call(port, "GET", "/api/workspaces")
        wid = listing["workspaces"][0]["id"]
        _, sess = call(port, "POST", "/api/workspaces/%s/sessions" % wid, {"title": "grow"})
        events = run_turn(port, wid, sess["id"], "write yourself a tool and use it")

        done = [e for e in events if e.get("type") == "done"]
        check("the turn ends", bool(done), [e.get("type") for e in events][-8:])
        answer = str(done[0].get("content") or "") if done else ""
        check("the model used the tool it had just written",
              "hello, tea_a2" in answer, answer[:200])

        # ---- the tool is on disk, outside the workspace ---------------------
        saved = os.path.join(modules, "toolkit_hello.py")
        check("the tool was saved in the module directory", os.path.exists(saved), saved)
        if os.path.exists(saved):
            text = open(saved, encoding="utf-8").read()
            check("the saved file carries its version", "# version: 1.0.0" in text,
                  text[:120])
            check("the saved file carries the schema",
                  "def meta_toolkit_hello()" in text, text[-200:])

        # ---- the save is audited --------------------------------------------
        audit = os.path.join(os.path.expanduser("~"), ".tea_agent", "ext-audit.jsonl")
        check("the save is audited", os.path.exists(audit), audit)

        # ---- the operator can turn model-driven saves off ------------------
        sys.path.insert(0, os.path.join(REPO_ROOT, "sched"))
        from a2_ext_py.toolkit.mcp_server import Server
        locked = Server(module_dir=os.path.join(modules, "locked"), allow_save=False)
        answer = locked.call("toolkit_save", {
            "name": "toolkit_blocked", "meta": HELLO_META, "pycode": HELLO_CODE})
        text = str((answer.get("content") or [{}])[0].get("text") or "")
        check("the operator can turn model-driven saves off",
              "turned" in text and not os.path.exists(
                  os.path.join(modules, "locked", "toolkit_blocked.py")), text)

        # ---- a module may import what it needs --------------------------
        # The loader execs a module with separate globals and locals. With two
        # dicts a module level import lands in the locals, where neither a
        # function (its __globals__ is the globals dict) nor a class body can
        # see it - so "a module may import what it needs" did not work.
        with open(os.path.join(modules, "toolkit_imports.py"), "w", encoding="utf-8") as f:
            f.write('import os\n\n\n'
                    'class Where:\n'
                    '    sep = os.sep\n\n\n'
                    'def toolkit_imports():\n'
                    '    """Where are we?"""\n'
                    '    return os.getcwd() + Where.sep\n\n\n'
                    'def meta_toolkit_imports():\n'
                    '    return {"name": "toolkit_imports",\n'
                    '            "description": "Where are we?",\n'
                    '            "inputSchema": {"type": "object", "properties": {}}}\n')
        server = Server(module_dir=modules)
        answer = server.call("toolkit_reload", {})
        text = str((answer.get("content") or [{}])[0].get("text") or "")
        broken = text.split("these files did not load:")[-1] if "these files did not load:" in text else ""
        check("a module that imports something loads",
              "toolkit_imports" not in broken, text)
        answer = server.call("toolkit_imports", {})
        text = str((answer.get("content") or [{}])[0].get("text") or "")
        check("the imported name works in a function and in a class body",
              text.endswith(os.sep) and "Error" not in text, text)

        # ---- the tool is in the agent's tool list now -----------------------
        _, tools = call(port, "GET", "/api/tools")
        names = [t.get("name") for t in (tools.get("tools") or [])]
        check("the new tool is in the agent's tool list",
              "mcp__a2ext__toolkit_hello" in names, names[:20])
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        mock.shutdown()

    print("")
    print("%d/%d checks passed" % (len(checks) - len(failures), len(checks)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
