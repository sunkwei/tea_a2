#!/usr/bin/env python3
"""Smoke test for the request id, the error catalog and the capabilities.

Three things that make the API usable by a program instead of by a reader:

  * **the catalog** (`GET /api/errors`): every error the API can answer with
    says what it means and whether trying again makes sense. A caller must
    not have to parse an English sentence to find that out. The check that
    matters most is the one against the source: every code the server can
    send is in the catalog, with the same status.
  * **the request id** (`X-Request-Id`): every answer carries one, the
    caller's own is echoed, and the frames of a turn carry it too - so a frame
    can be traced back to the call that caused it.
  * **the capabilities** (`GET /api/capabilities`): what this machine can do.
    The task queue reads it and picks its way (no `async_turns` means the old
    way of holding a stream open).

Usage: python3 tests/smoke_error_catalog.py [path-to-tea_agent-binary]
"""

import http.client
import io
import json
import os
import random
import re
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
SERVER_SRC = os.path.join(REPO_ROOT, "src", "server.cpp")
CATALOG_SRC = os.path.join(REPO_ROOT, "src", "errors.cpp")


class Provider(BaseHTTPRequestHandler):
    """A mock model that streams a little and then answers."""

    def log_message(self, *args):
        pass

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
        if "HANGME" in text:
            # A turn that never ends: the follower slot it holds stays taken,
            # so the next follower has to be refused - every time.
            for _ in range(600):
                try:
                    self.wfile.write(("data: " + json.dumps({
                        "id": "c1", "object": "chat.completion.chunk",
                        "choices": [{"index": 0, "delta": {"content": "."},
                                     "finish_reason": None}],
                    }) + "\n\n").encode())
                    self.wfile.flush()
                except OSError:
                    return
                time.sleep(0.5)
            return
        for piece in ("slow ", "answer"):
            self.wfile.write(("data: " + json.dumps({
                "id": "c1", "object": "chat.completion.chunk",
                "choices": [{"index": 0, "delta": {"content": piece},
                             "finish_reason": None}],
            }) + "\n\n").encode())
            self.wfile.flush()
            time.sleep(1.0)
        self.wfile.write(("data: " + json.dumps({
            "id": "c1", "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }) + "\n\n").encode())
        self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


class OldishAgent(BaseHTTPRequestHandler):
    """An agent that knows the capabilities but not the turn handles."""

    chats = 0

    def log_message(self, *args):
        pass

    def _json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802 (http.server API)
        if self.path == "/api/capabilities":
            self._json(200, {"ok": True, "version": "0.19.9",
                             "features": {"async_turns": False, "limits": True},
                             "limits": {}, "routes": []})
            return
        if self.path == "/api/health":
            self._json(200, {"ok": True, "version": "0.19.9", "provider": "minimax",
                             "model": "m", "limits": {"max_iterations": 300}})
            return
        if self.path.startswith("/api/providers/") and self.path.endswith("/usage"):
            self._json(200, {"ok": True, "interval": {"remaining_percent": 100.0},
                             "weekly": {"remaining_percent": 100.0}})
            return
        if self.path == "/api/workspaces":
            self._json(200, {"ok": True, "workspaces": [
                {"id": "ws_old", "name": "old", "path": "/tmp/old"}]})
            return
        self._json(404, {"error": {"code": "not_found", "message": self.path}})

    def do_POST(self):  # noqa: N802 (http.server API)
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        if self.path == "/api/workspaces":
            self._json(200, {"ok": True, "workspace": {"id": "ws_old"}})
            return
        if self.path != "/api/chat":
            self._json(404, {"error": {"code": "not_found", "message": self.path}})
            return
        OldishAgent.chats += 1
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for frame in ({"type": "start", "session_id": "s_old"},
                      {"type": "content_delta", "text": "the old answer"},
                      {"type": "done", "content": "the old answer", "iterations": 1}):
            self.wfile.write(("data: " + json.dumps(frame) + "\n\n").encode())
            self.wfile.flush()
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


def request(port, method, path, body=None, headers=None, timeout=20):
    """One request. Returns (status, headers, parsed-json-or-None)."""
    head = dict(headers or {})
    payload = None
    if body is not None:
        payload = json.dumps(body).encode()
        head["Content-Type"] = "application/json"
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    conn.request(method, path, body=payload, headers=head)
    resp = conn.getresponse()
    raw = resp.read()
    try:
        parsed = json.loads(raw or b"null")
    except ValueError:
        parsed = None
    # The queue answers in one shape: {code, message, request_id, data}. The
    # checks want the data.
    if isinstance(parsed, dict) and "code" in parsed and "data" in parsed:
        parsed = parsed.get("data") or {}
    return resp.status, dict(resp.getheaders()), parsed


def source_pairs():
    """Every (status, code) the server can answer with, from the source."""
    src = io.open(SERVER_SRC, encoding="utf-8").read()
    # The variable the response is called by does not matter ("res", "res_",
    # ...), only the status and the code do.
    return set((int(m.group(1)), m.group(2))
               for m in re.finditer(r'send_error\(\s*\w+\s*,\s*(\d+),\s*"([a-z_]+)"', src))


def catalog_pairs():
    """Every (status, code) the catalog knows, from the source."""
    src = io.open(CATALOG_SRC, encoding="utf-8").read()
    return set((int(m.group(2)), m.group(1))
               for m in re.finditer(r'\{"([a-z_]+)",\s*(\d+),', src))


def read_stream(port, path, timeout=20.0):
    """Read an SSE stream until it ends. Returns the events."""
    events = []
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    conn.request("GET", path)
    resp = conn.getresponse()
    try:
        while True:
            line = resp.readline()
            if not line:
                break
            line = line.strip()
            if not line.startswith(b"data: "):
                continue
            data = line[len(b"data: "):]
            if data == b"[DONE]":
                break
            try:
                events.append(json.loads(data))
            except ValueError:
                continue
    except OSError:
        pass
    finally:
        conn.close()
    return events


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

    tmp = tempfile.mkdtemp(prefix="tea_catalog_")
    ws_dir = os.path.join(tmp, "ws")
    os.makedirs(ws_dir)
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
""" % (provider_port,))

    agent_port = free_port()
    agent = subprocess.Popen([
        binary, "--config", config_path, "--tea-home", os.path.join(tmp, "home"),
        "--host", "127.0.0.1", "--port", str(agent_port), "--no-console",
        "--log-file", os.path.join(tmp, "tea_agent.log"), "--workspace", ws_dir,
        # One follower per turn: the second one has to be refused, and the
        # refusal has to say when to come back.
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env=dict(os.environ, TEA_AGENT_MAX_SUBSCRIBERS="1"))

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
        _, _, listing = request(agent_port, "GET", "/api/workspaces")
        ws_id = listing["workspaces"][0]["id"]

        # ---- 1) the catalog and the source agree --------------------------
        used = source_pairs()
        known = catalog_pairs()
        check("every error the code can send is in the catalog",
              not (used - known), sorted(used - known))
        check("the catalog has no entry the code cannot send",
              not (known - used), sorted(known - used))

        status, _, catalog = request(agent_port, "GET", "/api/errors")
        check("GET /api/errors answers", status == 200, status)
        entries = catalog.get("errors") or []
        check("the catalog lists every pair", len(entries) == len(used),
              (len(entries), len(used)))
        check("every entry says what it means",
              all(e.get("meaning") and e.get("hint") for e in entries),
              [e for e in entries if not e.get("meaning")][:2])
        check("every entry says whether it can be retried",
              all(isinstance(e.get("retryable"), bool) for e in entries), entries[:2])
        check("the 429s say when to come back",
              all(e.get("retry_after", 0) > 0 for e in entries if e.get("http") == 429),
              [e for e in entries if e.get("http") == 429])

        # ---- 2) the capabilities ------------------------------------------
        status, _, caps = request(agent_port, "GET", "/api/capabilities")
        check("GET /api/capabilities answers", status == 200, status)
        features = caps.get("features") or {}
        check("it says the machine knows the turn handles",
              features.get("async_turns") is True, features)
        check("it says what is not there yet",
              features.get("approvals") is False
              and features.get("idempotency") is False, features)
        limits = caps.get("limits") or {}
        check("it reports the ceilings",
              limits.get("max_active_turns", 0) > 0
              and limits.get("max_subscribers", 0) > 0
              and limits.get("max_tool_output", 0) > 0, limits)
        routes = caps.get("routes") or []
        check("it lists the routes a caller may rely on",
              any(r.get("path") == "/api/chat" for r in routes)
              and any(r.get("path") == "/api/turns/{turn_id}" for r in routes),
              routes)
        check("it names the version", str(caps.get("version") or "").startswith("0."),
              caps.get("version"))

        # ---- 3) the request id --------------------------------------------
        status, headers, _ = request(agent_port, "GET", "/api/health")
        check("every answer carries a request id",
              bool(headers.get("X-Request-Id")), headers)
        status, headers, _ = request(agent_port, "GET", "/api/health",
                                      headers={"X-Request-Id": "trace-42"})
        check("the caller's own id is echoed",
              headers.get("X-Request-Id") == "trace-42", headers)
        status, headers, _ = request(agent_port, "GET", "/api/health",
                                      headers={"X-Request-Id": "bad id with spaces"})
        check("an unusable id is replaced, not echoed",
              headers.get("X-Request-Id") not in (None, "", "bad id with spaces"),
              headers.get("X-Request-Id"))
        status, headers, body = request(agent_port, "GET", "/api/turns/t_nope")
        error = (body or {}).get("error") or {}
        check("the error body carries the request id",
              error.get("request_id") == headers.get("X-Request-Id"),
              (error.get("request_id"), headers.get("X-Request-Id")))
        check("the error body says whether it can be retried",
              isinstance(error.get("retryable"), bool), error)
        check("the error body carries the hint", bool(error.get("hint")), error)

        # ---- 4) the frames of a turn carry it too ------------------------
        status, headers, body = request(
            agent_port, "POST", "/api/chat",
            {"workspace_id": ws_id, "message": "hello", "provider": "mock",
             "model": "m", "async": True},
            headers={"X-Request-Id": "turn-trace-1"})
        check("the turn is submitted", status == 202, (status, body))
        turn_id = body.get("turn_id")
        check("the answer carries the request id",
              headers.get("X-Request-Id") == "turn-trace-1", headers)
        events = read_stream(agent_port, "/api/turns/%s/events?since=0" % turn_id)
        check("the frames carry the request id",
              events and all(e.get("request_id") == "turn-trace-1" for e in events),
              [e.get("request_id") for e in events[:3]])

        # ---- 5) a 429 says when to come back ------------------------------
        # One follower per turn: the second one is refused. The turn hangs on
        # purpose, so the first follower keeps its slot while the second one
        # asks for it.
        _, _, hanging = request(
            agent_port, "POST", "/api/chat",
            {"workspace_id": ws_id, "message": "HANGME please", "provider": "mock",
             "model": "m", "async": True})
        hang_id = hanging.get("turn_id")
        check("the hanging turn is submitted", bool(hang_id), hanging)
        first = http.client.HTTPConnection("127.0.0.1", agent_port, timeout=20)
        first.request("GET", "/api/turns/%s/events?since=0" % hang_id)
        first.getresponse().readline()      # take the first frame, keep the slot
        status, headers, body = request(agent_port, "GET",
                                        "/api/turns/%s/events" % hang_id)
        check("the second follower is refused with 429",
              status == 429, (status, body))
        check("the 429 says when to come back",
              bool(headers.get("Retry-After")), headers)
        check("the refusal names the code",
              ((body or {}).get("error") or {}).get("code") == "too_many_subscribers",
              body)
        first.close()

        # ---- 6) the queue reads the capabilities --------------------------
        old_agent = ThreadingHTTPServer(("127.0.0.1", 0), OldishAgent)
        old_agent.daemon_threads = True
        threading.Thread(target=old_agent.serve_forever, daemon=True).start()
        old_port = old_agent.server_address[1]
        queue_port = free_port()
        queue = subprocess.Popen([
            sys.executable, os.path.join(REPO_ROOT, "sched", "task_server.py"),
            "--agent", "http://127.0.0.1:%d" % old_port,
            "--host", "127.0.0.1", "--port", str(queue_port),
            "--data-dir", os.path.join(tmp, "tasks-old"),
            "--provider", "minimax", "--interval", "1", "--max-per-round", "4",
            "--workspace-path", os.path.join(tmp, "ws-old"),
        ], stdout=open(os.path.join(tmp, "queue.log"), "a"),
            stderr=subprocess.STDOUT, universal_newlines=True)
        try:
            if not wait_health(queue_port):
                print("the queue did not become healthy")
                return 1
            status, _, task = request(queue_port, "POST", "/api/tasks",
                                      {"workspace_id": "ws_old", "prompt": "please"})
            task_id = task.get("id")
            check("the task is created", bool(task_id), task)
            deadline = time.time() + 40
            last = {}
            while time.time() < deadline:
                _, _, last = request(queue_port, "GET",
                                     "/api/tasks/%s?workspace_id=ws_old" % task_id)
                if (last or {}).get("status") in ("done", "failed"):
                    break
                time.sleep(0.3)
            check("an agent without async_turns still works",
                  last.get("status") == "done", last)
            check("the queue asked for a chat the old way",
                  OldishAgent.chats >= 1, OldishAgent.chats)
            check("the task has no handle (the agent gave none)",
                  not last.get("turn_id"), last.get("turn_id"))
        finally:
            queue.terminate()
            try:
                queue.wait(timeout=10)
            except Exception:
                queue.kill()
            old_agent.shutdown()
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
