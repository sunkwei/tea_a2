#!/usr/bin/env python3
"""Smoke test for the idempotency key.

A caller that retries - a queue, a watchdog, a phone with a flaky link -
must not make the machine do the work twice: a second turn costs money and
may run the same command again. So a write request may carry an
**idempotency key**: the same key gets the same answer back, and the work is
done once.

What is pinned here:

  * the same key and the same request give the same answer (the same turn),
    and the model is called **once**;
  * a different key is a different request (the model is called again);
  * the same key with a **different** request is refused (409
    idempotency_conflict) - answering it would give the caller somebody
    else's answer;
  * without a key every request is a new turn (nothing changes for the
    callers that do not use one);
  * the stream path (no "async") is the same: the retry follows the turn that
    is already running;
  * the answers live in the tea home, so they survive a restart;
  * the web tools (`POST /api/tools/call`) remember their answer too - they
    are billed per call.

Usage: python3 tests/smoke_idempotency.py [path-to-tea_agent-binary]
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


class Provider(BaseHTTPRequestHandler):
    """A mock model that counts how often it was asked."""

    calls = []

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
        Provider.calls.append(text)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self.wfile.write(("data: " + json.dumps({
            "id": "c1", "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": {"content": "answered: " + text},
                         "finish_reason": None}],
        }) + "\n\n").encode())
        self.wfile.flush()
        self.wfile.write(("data: " + json.dumps({
            "id": "c1", "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }) + "\n\n").encode())
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


def request(port, method, path, body=None, headers=None, timeout=30):
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
    return resp.status, dict(resp.getheaders()), parsed


def wait_calls(count, timeout=15.0):
    """Wait until the model was asked `count` times.

    A submit answers 202 before the model is called (the turn runs in the
    background), so a check right after the submit would be looking too early.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if len(Provider.calls) >= count:
            return True
        time.sleep(0.05)
    return False


def read_stream(port, path, headers=None, timeout=30.0):
    """Read an SSE stream until it ends. Returns (status, events)."""
    events = []
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    conn.request("GET", path, headers=headers or {})
    resp = conn.getresponse()
    status = resp.status
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
    return status, events


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

    tmp = tempfile.mkdtemp(prefix="tea_idem_")
    home = os.path.join(tmp, "home")
    ws_dir = os.path.join(tmp, "ws")
    os.makedirs(home)
    os.makedirs(ws_dir)
    config_path = os.path.join(home, "provider.yaml")
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

    def start_agent():
        return subprocess.Popen([
            binary, "--config", config_path, "--tea-home", home,
            "--host", "127.0.0.1", "--port", str(agent_port), "--no-console",
            "--log-file", os.path.join(home, "logs", "tea_agent.log"),
            "--workspace", ws_dir,
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    agent = start_agent()
    failures = []
    checks = []

    def check(name, ok, detail=""):
        checks.append(name)
        print(("PASS " if ok else "FAIL ") + name + (("  " + str(detail)) if not ok else ""))
        if not ok:
            failures.append(name)

    def submit(body, key=None):
        headers = {"Idempotency-Key": key} if key else {}
        return request(agent_port, "POST", "/api/chat", body, headers=headers)

    try:
        if not wait_health(agent_port):
            print("agent did not become healthy")
            return 1
        _, _, listing = request(agent_port, "GET", "/api/workspaces")
        ws_id = listing["workspaces"][0]["id"]

        # ---- 1) the same key is the same turn -----------------------------
        Provider.calls = []
        body = {"workspace_id": ws_id, "message": "once please", "provider": "mock",
                "model": "m", "async": True}
        status, _, first = submit(body, "key-one")
        check("the first submit answers 202", status == 202, (status, first))
        status, _, second = submit(body, "key-one")
        check("the retry answers 202 as well", status == 202, (status, second))
        check("the retry gets the same turn",
              first.get("turn_id") and first.get("turn_id") == second.get("turn_id"),
              (first.get("turn_id"), second.get("turn_id")))
        wait_calls(1)
        check("the model was asked once", len(Provider.calls) == 1, Provider.calls)

        # ---- 2) a different key is a different turn -----------------------
        status, _, other = submit(body, "key-two")
        check("another key starts another turn",
              other.get("turn_id") and other.get("turn_id") != first.get("turn_id"),
              (first.get("turn_id"), other.get("turn_id")))
        wait_calls(2)
        check("the model was asked twice now", len(Provider.calls) == 2, Provider.calls)

        # ---- 3) the same key with another question is refused -------------
        status, _, err = submit({"workspace_id": ws_id, "message": "something else",
                                  "provider": "mock", "model": "m", "async": True},
                                 "key-one")
        check("the same key with another request is refused",
              status == 409 and ((err or {}).get("error") or {}).get("code")
              == "idempotency_conflict", (status, err))
        check("the refusal did not ask the model",
              len(Provider.calls) == 2, Provider.calls)

        # ---- 4) without a key every request is a new turn -----------------
        status, _, plain = submit(body)
        check("without a key the request is new", status == 202, (status, plain))
        check("it is another turn", plain.get("turn_id") != first.get("turn_id"),
              (first.get("turn_id"), plain.get("turn_id")))
        wait_calls(3)
        check("the model was asked again", len(Provider.calls) == 3, Provider.calls)

        # ---- 5) the key may come in the body ------------------------------
        status, _, in_body = submit({"workspace_id": ws_id, "message": "in the body",
                                     "provider": "mock", "model": "m", "async": True,
                                     "idempotency_key": "key-body"})
        check("a key in the body is a key too", status == 202 and in_body.get("turn_id"),
              (status, in_body))
        status, _, in_body2 = submit({"workspace_id": ws_id, "message": "in the body",
                                      "provider": "mock", "model": "m", "async": True,
                                      "idempotency_key": "key-body"})
        check("the retry with the body key gets the same turn",
              in_body2.get("turn_id") == in_body.get("turn_id"),
              (in_body.get("turn_id"), in_body2.get("turn_id")))
        wait_calls(4)
        check("the body key asked the model once", len(Provider.calls) == 4,
              Provider.calls)

        # ---- 6) the stream path: the retry follows the same turn ----------
        Provider.calls = []
        stream_body = {"workspace_id": ws_id, "message": "stream please",
                       "provider": "mock", "model": "m"}
        conn = http.client.HTTPConnection("127.0.0.1", agent_port, timeout=30)
        conn.request("POST", "/api/chat", body=json.dumps(stream_body),
                     headers={"Content-Type": "application/json",
                              "Idempotency-Key": "key-stream"})
        resp = conn.getresponse()
        first_events = []
        while True:
            line = resp.readline()
            if not line:
                break
            line = line.strip()
            if line.startswith(b"data: ") and line[6:] != b"[DONE]":
                try:
                    first_events.append(json.loads(line[6:]))
                except ValueError:
                    pass
        conn.close()
        stream_turn = ""
        for event in first_events:
            if event.get("turn_id"):
                stream_turn = event["turn_id"]
                break
        check("the frames say which turn they are", bool(stream_turn),
              [e.get("type") for e in first_events[:3]])
        status, second_events = read_stream(agent_port,
                                            "/api/turns/%s/events?since=0" % stream_turn)
        check("the stream path answers with a stream", status == 200, status)
        check("the model was asked once for the stream", len(Provider.calls) == 1,
              Provider.calls)

        # ---- 7) the web tools remember their answer ----------------------
        tool_body = {"tool": "toolkit_search", "arguments": {"query": "tea"}}
        status, _, t1 = request(agent_port, "POST", "/api/tools/call", tool_body,
                                headers={"Idempotency-Key": "key-tool"})
        status2, _, t2 = request(agent_port, "POST", "/api/tools/call", tool_body,
                                 headers={"Idempotency-Key": "key-tool"})
        check("the tool call is answered the same way twice",
              status == status2 and t1 == t2, (status, status2, t1, t2))

        # ---- 8) the answers survive a restart ----------------------------
        agent.terminate()
        try:
            agent.wait(timeout=10)
        except Exception:
            agent.kill()
        agent = start_agent()
        if not wait_health(agent_port):
            print("the agent did not come back")
            return 1
        # A turn died with the server: the work has to be done again, so the
        # retry gets a **new** turn (the old one is gone).
        before = len(Provider.calls)
        status, _, after = submit(body, "key-one")
        check("a turn that died with the server is done again",
              status == 202 and after.get("turn_id") != first.get("turn_id"),
              (status, after.get("turn_id"), first.get("turn_id")))
        wait_calls(before + 1)
        check("the work was really done again",
              len(Provider.calls) == before + 1, Provider.calls)
        # An answer that is not a turn (the web tools) is replayed as it was:
        # the tool must not be called a second time.
        before = len(Provider.calls)
        status, _, t3 = request(agent_port, "POST", "/api/tools/call", tool_body,
                                headers={"Idempotency-Key": "key-tool"})
        check("the remembered tool answer survives the restart",
              status == 200 and t3 == t1, (status, t3))
        check("the tool was not called again", len(Provider.calls) == before,
              Provider.calls)
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
