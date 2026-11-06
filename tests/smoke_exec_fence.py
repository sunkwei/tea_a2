#!/usr/bin/env python3
"""Smoke test for the exec fence (the missing half of the policy).

The file tools are fenced by the workspace; the command tool was not, so a
turn that was given a narrow set of directories could still run a command
that touched anything. A caller that drives its own policy (the task queue)
can now say what may be run:

    "policy": {"exec": {"allow": ["npm *", "git *"], "deny": ["rm -rf *"]}}

What is pinned here:

  * without a policy nothing is fenced (the open semantics stay);
  * a denied command does **not run** - and says why;
  * an allow list fences everything that is not on it;
  * the fence is per turn: the next turn is open again.

Usage: python3 tests/smoke_exec_fence.py [path-to-tea_agent-binary]
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
    from http.server import ThreadingHTTPServer
except ImportError:  # Python 3.6
    from http.server import HTTPServer
    from socketserver import ThreadingMixIn

    class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
        daemon_threads = True

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Provider(BaseHTTPRequestHandler):
    """A mock model that asks for a command and then answers."""

    def log_message(self, *args):
        pass

    def _chunk(self, payload):
        try:
            self.wfile.write(("data: " + json.dumps(payload) + "\n\n").encode())
            self.wfile.flush()
        except OSError:
            pass

    def _tool_call(self, name, args, call_id):
        self._chunk({
            "id": "c1", "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": {"role": "assistant", "tool_calls": [{
                "index": 0, "id": call_id, "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)},
            }]}, "finish_reason": None}],
        })
        self._chunk({
            "id": "c1", "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
        })

    def _text(self, text):
        self._chunk({
            "id": "c1", "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
        })
        self._chunk({
            "id": "c1", "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        })

    def do_POST(self):  # noqa: N802 (http.server API)
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            body = {}
        messages = body.get("messages") or []
        last = messages[-1] if messages else {}
        text = last.get("content") if isinstance(last.get("content"), str) else ""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        tool_msgs = [m for m in messages if m.get("role") == "tool"]
        if not tool_msgs:
            # The command the task asked for: "RUN <command>".
            command = text[4:].strip() if text.startswith("RUN ") else "echo hi"
            self._tool_call("toolkit_exec", {"command": command}, "call_1")
            return
        # The tool answered: repeat what it said.
        result = tool_msgs[-1].get("content") or ""
        self._text("the tool said: " + result[:200])
        self.wfile.write(b"data: [DONE]\n\n")
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


def request(port, method, path, body=None, timeout=60):
    head = {"Content-Type": "application/json"} if body is not None else {}
    payload = json.dumps(body).encode() if body is not None else None
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    conn.request(method, path, body=payload, headers=head)
    resp = conn.getresponse()
    raw = resp.read()
    try:
        parsed = json.loads(raw or b"null")
    except ValueError:
        parsed = None
    return resp.status, parsed


def run_turn(port, ws_id, message, policy=None):
    """One turn; returns the answer of the model."""
    body = {"workspace_id": ws_id, "message": message, "provider": "mock", "model": "m"}
    if policy is not None:
        body["policy"] = policy
    status, events = request(port, "POST", "/api/chat", body)
    if status != 200:
        return status, []
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    conn.request("POST", "/api/chat", body=json.dumps(body),
                 headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    frames = []
    try:
        while True:
            line = resp.readline()
            if not line:
                break
            line = line.strip()
            if not line.startswith(b"data: "):
                continue
            if line[6:] == b"[DONE]":
                break
            try:
                frames.append(json.loads(line[6:]))
            except ValueError:
                continue
    except OSError:
        pass
    finally:
        conn.close()
    return status, frames


def answer_of(frames):
    for event in frames:
        if event.get("type") == "done":
            return str(event.get("content") or "")
    return ""


def tool_output(frames):
    for event in frames:
        if event.get("type") == "tool_result":
            return str(event.get("output") or "")
    return ""


def main():
    binary = (
        sys.argv[1]
        if len(sys.argv) > 1
        else os.path.join(REPO_ROOT, "build", "bin", "tea_agent")
    )
    if not os.path.exists(binary):
        print("binary not found:", binary)
        return 1

    provider = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    provider.daemon_threads = True
    threading.Thread(target=provider.serve_forever, daemon=True).start()
    provider_port = provider.server_address[1]

    tmp = tempfile.mkdtemp(prefix="tea_fence_")
    ws_dir = os.path.join(tmp, "ws")
    os.makedirs(ws_dir)
    marker = os.path.join(tmp, "do-not-delete.txt")
    with open(marker, "w", encoding="utf-8") as f:
        f.write("still here")
    config_path = os.path.join(tmp, "provider.yaml")
    with open(config_path, "w", encoding="utf-8") as f:
        f.write("""version: 1
providers:
  mock:
    api_url: http://127.0.0.1:%d/v1
    api_key: sk-test
    default_model: m
    models:
      m:
        max_context_tokens: 32768
        max_output_tokens: 4096
        supports_tools: true
""" % (provider_port,))

    agent_port = free_port()
    agent = subprocess.Popen([
        binary, "--config", config_path, "--tea-home", os.path.join(tmp, "home"),
        "--host", "127.0.0.1", "--port", str(agent_port), "--no-console",
        "--log-file", os.path.join(tmp, "tea_agent.log"), "--workspace", ws_dir,
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
            print("agent did not become healthy")
            return 1
        _, listing = request(agent_port, "GET", "/api/workspaces")
        ws_id = listing["workspaces"][0]["id"]

        # ---- 1) without a policy nothing is fenced ------------------------
        status, frames = run_turn(agent_port, ws_id, "RUN echo hello-fence")
        check("the turn ran", status == 200, status)
        check("the command ran", "hello-fence" in tool_output(frames),
              tool_output(frames)[:200])

        # ---- 2) a denied command does not run ----------------------------
        status, frames = run_turn(
            agent_port, ws_id, "RUN rm -f %s" % marker,
            policy={"exec": {"deny": ["rm -f *"]}})
        output = tool_output(frames)
        check("the denied command was refused", "forbids" in output or "denied" in output,
              output[:200])
        check("the denied command did not run", os.path.exists(marker),
              "the file is gone")
        check("the refusal says what the policy allows",
              "policy" in output.lower(), output[:200])

        # ---- 3) an allow list fences everything else ---------------------
        status, frames = run_turn(
            agent_port, ws_id, "RUN echo allowed-one",
            policy={"exec": {"allow": ["echo *"]}})
        check("the allowed command ran", "allowed-one" in tool_output(frames),
              tool_output(frames)[:200])
        status, frames = run_turn(
            agent_port, ws_id, "RUN ls -la",
            policy={"exec": {"allow": ["echo *"]}})
        output = tool_output(frames)
        check("the command that is not allowed was refused",
              "does not allow" in output or "forbids" in output, output[:200])
        check("it did not run", "total" not in output, output[:200])

        # ---- 4) the fence is per turn -------------------------------------
        status, frames = run_turn(agent_port, ws_id, "RUN echo open-again")
        check("the next turn is open again", "open-again" in tool_output(frames),
              tool_output(frames)[:200])

        # ---- 5) the deny list wins over the allow list -------------------
        status, frames = run_turn(
            agent_port, ws_id, "RUN rm -f %s" % marker,
            policy={"exec": {"allow": ["rm *", "echo *"], "deny": ["rm -f *"]}})
        check("the denied command is refused even when it is allowed",
              os.path.exists(marker) and "forbids" in tool_output(frames),
              (os.path.exists(marker), tool_output(frames)[:120]))
    finally:
        try:
            agent.terminate()
            agent.wait(timeout=10)
        except Exception:
            agent.kill()
        provider.shutdown()

    print()
    print("%d/%d checks passed" % (len(checks) - len(failures), len(checks)))
    if failures:
        print("FAILED:", ", ".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
