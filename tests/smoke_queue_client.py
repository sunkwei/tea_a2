#!/usr/bin/env python3
"""Regression test for three things that were broken and should stay fixed.

  1. **The queue's answers are written once.** `_json` used to write the body
     twice (the envelope and then the bare payload), so the second copy sat in
     the socket and the *next* request on the same keep-alive connection read
     garbage instead of a response. A client that reuses a connection (every
     browser does) saw the answer of the request before it.

  2. **A project key does not see another project's events.** The event stream
     is a broadcast; without a filter a key holder learned what every other
     project was doing - the prompt text travels with the event.

  3. **`tea_api.py` talks to the right server.** The task routes moved to the
     Python queue, but the client still asked the agent for them (404). The
     client now has `Tea` (the agent) and `Queue` (the queue).

Usage: python3 tests/smoke_queue_client.py [path-to-tea_agent-binary]
"""

import http.client
import json
import os
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
SERVER = os.path.join(REPO_ROOT, "sched", "task_server.py")
sys.path.insert(0, os.path.join(REPO_ROOT, "sched"))


def free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def wait_health(port, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            conn.request("GET", "/api/health")
            conn.getresponse().read()
            conn.close()
            return True
        except Exception:
            time.sleep(0.3)
    return False


def call(port, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    payload = None
    head = dict(headers or {})
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
        head["Content-Type"] = "application/json"
    conn.request(method, path, body=payload, headers=head)
    resp = conn.getresponse()
    raw = resp.read().decode("utf-8", "replace")
    conn.close()
    try:
        return resp.status, json.loads(raw)
    except ValueError:
        return resp.status, raw


class Provider(BaseHTTPRequestHandler):
    """A tiny OpenAI-compatible mock plus the MiniMax M Plan quota endpoint."""

    quota_remaining = 100.0

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
                "current_interval_remaining_percent": Provider.quota_remaining,
                "current_weekly_remaining_percent": Provider.quota_remaining,
                "start_time": 0, "end_time": 0, "remains_time": 3600000,
                "weekly_start_time": 0, "weekly_end_time": 0, "weekly_remains_time": 86400000,
                "current_interval_status": 1, "current_weekly_status": 1,
            }
            self._json(200, {"base_resp": {"status_code": 0, "status_msg": "success"},
                             "model_remains": [row]})
            return
        self._json(404, {"error": {"message": "not found"}})

    def do_POST(self):  # noqa: N802 (http.server API)
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            request = json.loads(raw)
        except ValueError:
            request = {}
        if request.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n')
            self.wfile.write(b'data: {"usage":{"total_tokens":10,"prompt_tokens":8,'
                             b'"completion_tokens":2}}\n\n')
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            return
        self._json(200, {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                         "usage": {"total_tokens": 10, "prompt_tokens": 8, "completion_tokens": 2}})


def main():
    binary = (sys.argv[1] if len(sys.argv) > 1
              else os.path.join(REPO_ROOT, "build", "bin", "tea_agent"))
    if not os.path.exists(binary):
        print("binary not found:", binary)
        return 1

    provider = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    provider.daemon_threads = True
    threading.Thread(target=provider.serve_forever, daemon=True).start()
    provider_port = provider.server_address[1]

    tmp = tempfile.mkdtemp(prefix="tea_client_")
    home = os.path.join(tmp, "home")
    ws_dir = os.path.join(tmp, "ws")
    os.makedirs(home)
    os.makedirs(ws_dir)
    with open(os.path.join(home, "provider.yaml"), "w", encoding="utf-8") as f:
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
        supports_tools: true
""" % (provider_port, provider_port))

    agent_port = free_port()
    task_port = free_port()
    agent = subprocess.Popen([
        binary, "--tea-home", home, "--host", "127.0.0.1", "--port", str(agent_port),
        "--no-console", "--log-file", os.path.join(home, "logs", "tea_agent.log"),
        "--workspace", ws_dir,
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    server_log = open(os.path.join(tmp, "server.log"), "w")
    task_server = subprocess.Popen([
        sys.executable, SERVER, "--agent", "http://127.0.0.1:%d" % agent_port,
        "--host", "127.0.0.1", "--port", str(task_port),
        "--data-dir", os.path.join(tmp, "tasks"),
        "--provider", "minimax", "--interval", "2",
        "--workspace-path", os.path.join(tmp, "task-workspace"),
    ], stdout=server_log, stderr=subprocess.STDOUT, universal_newlines=True)

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
        if not wait_health(task_port):
            print("task server did not become healthy")
            return 1
        _, listing = call(agent_port, "GET", "/api/workspaces")
        ws_id = listing["workspaces"][0]["id"]

        # ---- 1. the answer is written once --------------------------------
        conn = http.client.HTTPConnection("127.0.0.1", task_port, timeout=10)
        answers = []
        broken = ""
        try:
            for _ in range(4):
                conn.request("GET", "/api/health")
                resp = conn.getresponse()
                body = resp.read()
                try:
                    answers.append(json.loads(body.decode("utf-8")))
                except ValueError as exc:
                    broken = "%s / %s" % (exc, body[:120].decode("utf-8", "replace"))
                    break
        except Exception as exc:  # a leftover response is not a valid status line
            broken = "%s: %s" % (type(exc).__name__, exc)
        finally:
            conn.close()
        check("four requests on one connection all parse", not broken and len(answers) == 4,
              broken or answers)
        check("each answer is the envelope",
              all(a.get("code") == 0 and "data" in a for a in answers if "code" in a),
              answers[:1])

        # ---- 2. a key does not see another project's events ----------------
        status, key = call(task_port, "POST", "/api/projects", {"workspace_id": ws_id})
        check("a project key can be minted", status == 200 and key.get("data", {}).get("key"),
              (status, key))
        project_key = key.get("data", {}).get("key")
        auth = {"Authorization": "Bearer " + project_key}

        # a second project: the queue's own workspace (the agent knows it).
        # A task may only name a workspace the agent knows, so the second
        # project has to be a real one.
        _, all_ws = call(agent_port, "GET", "/api/workspaces")
        other_id = ""
        for one in all_ws.get("workspaces") or []:
            if one.get("id") != ws_id:
                other_id = one.get("id")
                break
        check("there is a second project to use", bool(other_id), all_ws.get("workspaces"))
        status, other = call(task_port, "POST", "/api/projects", {"workspace_id": other_id})
        check("the second project gets a key", status == 200
              and other.get("data", {}).get("key"), (status, other))

        seen = []

        def watch():
            try:
                c = http.client.HTTPConnection("127.0.0.1", task_port, timeout=15)
                c.request("GET", "/api/events", headers=auth)
                resp = c.getresponse()
                for _ in range(40):
                    line = resp.fp.readline()
                    if not line:
                        break
                    text = line.decode("utf-8", "replace").strip()
                    if text.startswith("data:"):
                        try:
                            seen.append(json.loads(text[5:].strip()))
                        except ValueError:
                            pass
                c.close()
            except Exception:
                pass

        watcher = threading.Thread(target=watch, daemon=True)
        watcher.start()
        time.sleep(1.0)

        # a task in the *other* project: a key holder must not see it
        call(task_port, "POST", "/api/tasks",
             {"workspace_id": ws_id, "prompt": "OWN-PROMPT-marker"})
        other_status, other_task = call(task_port, "POST", "/api/tasks",
                                        {"workspace_id": other_id,
                                         "prompt": "FOREIGN-PROMPT-marker"})
        foreign_id = (other_task.get("data") or {}).get("id") if isinstance(other_task, dict) else None
        time.sleep(3.0)

        prompts = []
        for event in seen:
            task = event.get("task") or {}
            if task.get("prompt"):
                prompts.append(task["prompt"])
        check("the key holder sees no foreign prompt",
              "FOREIGN-PROMPT-marker" not in prompts, prompts)
        check("the key holder sees its own prompt",
              "OWN-PROMPT-marker" in prompts, prompts)

        # ---- 3. the client talks to the right server ----------------------
        from tea_api import ApiError, Queue, Tea

        tea = Tea("http://127.0.0.1:%d" % agent_port)
        check("Tea.health works", tea.health().get("ok") is True)
        check("Tea.providers works", len(tea.providers().get("providers") or []) >= 1)
        check("Tea.workspaces works", len(tea.workspaces()) >= 1)
        try:
            tea.usage("minimax")
            check("Tea.usage works", True)
        except ApiError as exc:
            check("Tea.usage works", False, exc)

        queue = Queue("http://127.0.0.1:%d" % task_port)
        created = queue.create_task("say ok", title="via the client")
        check("Queue.create_task works", bool(created.get("id")), created)
        check("the client unwraps the envelope",
              isinstance(created.get("id"), str) and "code" not in created, created)
        later = queue.create_task("do it later", not_before="+5")
        check("Queue.create_task takes not_before",
              bool(later.get("not_before")) and later.get("not_before_text") == "+5", later)
        one = queue.task(created["id"])
        check("Queue.task works", one.get("id") == created["id"], one)
        listing = queue.tasks()
        check("Queue.tasks works", isinstance(listing.get("tasks"), list), listing)
        check("Queue.health works", queue.health().get("agent_ok") is True)
        check("Queue.audit works", isinstance(queue.audit().get("entries"), list))

        # the error path: a non-zero code becomes an ApiError
        try:
            queue.task("t_does_not_exist")
            check("a missing task raises ApiError", False, "no exception")
        except ApiError as exc:
            check("a missing task raises ApiError", exc.status == 404, exc.status)

        # a key restricts the client to its project
        scoped = Queue("http://127.0.0.1:%d" % task_port)
        scoped.project_key(ws_id)
        check("the foreign task was created", bool(foreign_id), other_task)
        try:
            scoped.task(foreign_id)
            check("a keyed client cannot read a foreign task", False, "no exception")
        except ApiError as exc:
            check("a keyed client cannot read a foreign task", exc.status == 403, exc.status)

    finally:
        for proc in (task_server, agent):
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    print("")
    print("%d/%d checks passed" % (len(checks) - len(failures), len(checks)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
