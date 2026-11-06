#!/usr/bin/env python3
"""Smoke test for the queue's policy side: the event bridge and the quota.

Two things the strategy layer does with the mechanism layer's answers:

  * **the event bridge (S6)**: the agent and the queue each have their own
    events, and the two do not line up - the queue says task_done, the agent
    says done, and nobody knows which came first. The agent's events now come
    onto the queue's own bus (with a `turn_` prefix), so the router, the
    watchdog and the page subscribe to **one** stream;

  * **the quota becomes a budget (S5)**: the quota used to be only a gate in
    front of the work ("don't start if less than 5% is left"). With
    `--quota-tokens` (what a full quota is worth) and `--quota-share` (how
    much of what is left one task may take), the quota also says how much
    **this turn** may spend.

Usage: python3 tests/smoke_queue_policy.py [path-to-tea_agent-binary]
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
SERVER = os.path.join(REPO_ROOT, "sched", "task_server.py")

# How much of the quota is left (the mock says 40%).
QUOTA_PERCENT = 40.0


class Provider(BaseHTTPRequestHandler):
    """A mock model plus the MiniMax M Plan quota endpoint."""

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
        if self.path.startswith("/v1/token_plan/remains"):
            row = {
                "model_name": "general",
                "current_interval_remaining_percent": QUOTA_PERCENT,
                "current_weekly_remaining_percent": QUOTA_PERCENT,
                "start_time": 0, "end_time": 0, "remains_time": 3600000,
                "weekly_start_time": 0, "weekly_end_time": 0, "weekly_remains_time": 86400000,
                "current_interval_status": 1, "current_weekly_status": 1,
            }
            self._json(200, {"base_resp": {"status_code": 0, "status_msg": "success"},
                             "model_remains": [row]})
            return
        self._json(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802 (http.server API)
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self.wfile.write(("data: " + json.dumps({
            "id": "c1", "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": {"content": "the answer"},
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


def call(port, method, path, body=None, timeout=30):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    head = {"Content-Type": "application/json"} if body is not None else {}
    conn.request(method, path, body=json.dumps(body) if body is not None else None,
                 headers=head)
    resp = conn.getresponse()
    raw = resp.read()
    try:
        parsed = json.loads(raw or b"null")
    except ValueError:
        return resp.status, raw.decode("utf-8", "replace")
    if isinstance(parsed, dict) and "code" in parsed and "data" in parsed:
        return resp.status, parsed.get("data") or {}
    return resp.status, parsed


def read_events(port, seconds=12.0):
    """Read the queue's event bus for a while."""
    events = []
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=seconds + 5)
    conn.request("GET", "/api/events")
    resp = conn.getresponse()
    deadline = time.time() + seconds
    try:
        while time.time() < deadline:
            line = resp.readline()
            if not line:
                break
            line = line.strip()
            if not line.startswith(b"data: "):
                continue
            try:
                events.append(json.loads(line[6:]))
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
    if not os.path.exists(SERVER):
        print("task server not found:", SERVER)
        return 1

    provider = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    provider.daemon_threads = True
    threading.Thread(target=provider.serve_forever, daemon=True).start()
    provider_port = provider.server_address[1]

    tmp = tempfile.mkdtemp(prefix="tea_qpolicy_")
    home = os.path.join(tmp, "home")
    ws_dir = os.path.join(tmp, "ws")
    os.makedirs(home)
    os.makedirs(ws_dir)
    config_path = os.path.join(home, "provider.yaml")
    with open(config_path, "w", encoding="utf-8") as f:
        f.write("""version: 1
providers:
  minimax:
    api_url: http://127.0.0.1:%d/v1
    api_key: sk-cp-test
    supports_usage: true
    usage_type: minimax_token_plan
    usage_url: http://127.0.0.1:%d/v1/token_plan/remains
    models:
      MiniMax-M3.1-Flash:
        max_context_tokens: 1000000
        max_output_tokens: 128000
""" % (provider_port, provider_port))

    agent_port = free_port()
    queue_port = free_port()
    agent = subprocess.Popen([
        binary, "--tea-home", home, "--host", "127.0.0.1", "--port", str(agent_port),
        "--no-console", "--log-file", os.path.join(home, "logs", "tea_agent.log"),
        "--workspace", ws_dir,
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # The quota is worth 1,000,000 tokens at 100%, and one task may take half
    # of what is left: 40% left => 200,000 tokens for this task.
    queue = subprocess.Popen([
        sys.executable, SERVER, "--agent", "http://127.0.0.1:%d" % agent_port,
        "--host", "127.0.0.1", "--port", str(queue_port),
        "--data-dir", os.path.join(tmp, "tasks"),
        "--provider", "minimax", "--interval", "1", "--max-per-round", "4",
        "--workspace-path", os.path.join(tmp, "task-workspace"),
        "--quota-tokens", "1000000", "--quota-share", "0.5",
    ], stdout=open(os.path.join(tmp, "queue.log"), "a"),
        stderr=subprocess.STDOUT, universal_newlines=True)

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
        if not wait_health(queue_port):
            print("task server did not become healthy")
            return 1
        _, listing = call(agent_port, "GET", "/api/workspaces")
        ws_id = listing["workspaces"][0]["id"]

        # ---- the event bus ------------------------------------------------
        events = []
        import threading as _threading
        collector = {}

        def collect():
            collector["events"] = read_events(queue_port, seconds=45.0)

        reader = _threading.Thread(target=collect, daemon=True)
        reader.start()
        time.sleep(0.5)

        _, task = call(queue_port, "POST", "/api/tasks",
                       {"workspace_id": ws_id, "prompt": "do the thing",
                        "title": "a bridged task"})
        task_id = task.get("id")
        check("the task is created", bool(task_id), task)

        # ---- S5: the quota became this task's budget ----------------------
        deadline = time.time() + 40
        record = {}
        while time.time() < deadline:
            _, record = call(queue_port, "GET",
                             "/api/tasks/%s?workspace_id=%s" % (task_id, ws_id))
            if (record or {}).get("status") in ("done", "failed"):
                break
            time.sleep(0.3)
        check("the task finished", record.get("status") == "done", record)
        limits = record.get("limits") or {}
        check("the quota gave the task a token budget",
              int(limits.get("max_total_tokens") or 0) == 200000, limits)
        check("the answer is on the record", record.get("result") == "the answer",
              record.get("result"))

        reader.join(timeout=50)
        events = collector.get("events") or []
        types = [e.get("type") for e in events]
        check("the agent's own event came onto the queue's bus",
              "turn_done" in types, types[:12])
        check("the queue's verdict is on the same bus",
              "task_done" in types, types[:12])
        check("the agent's event came first",
              "turn_done" in types and "task_done" in types
              and types.index("turn_done") < types.index("task_done"), types[:12])
        bridged = [e for e in events if e.get("type") == "turn_done"]
        check("the bridged event says which turn it is",
              bool(bridged and bridged[0].get("turn_id")), bridged[:1])
        check("the bridged event carries the task",
              bool(bridged and (bridged[0].get("task") or {}).get("id")), bridged[:1])
    finally:
        try:
            queue.terminate()
            queue.wait(timeout=10)
        except Exception:
            queue.kill()
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
