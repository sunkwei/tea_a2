#!/usr/bin/env python3
"""Smoke test for the queue's new shape: **submit + subscribe** (S1).

The queue used to hold one SSE connection per task until the turn ended. The
connection was the task's life: break it (a proxy, a network hiccup, the queue
process dying) and the task was declared dead - while the agent went on
working on it, burning the provider's tokens, with nobody able to stop it.

Now the queue **submits** the turn and keeps its **handle**. What is pinned
here:

  * a task that is running has a `turn_id` on its record, and that id is a
    real turn on the agent's side (the same session);
  * the queue is killed in the middle of a turn and started again: the task is
    **taken over**, not failed - the work was still running, so it keeps
    running and the answer still lands on the record;
  * the queue is killed after the turn finished: the verdict comes from the
    agent's status, so the task ends as done instead of failed;
  * a task that runs out of time stops **its** turn: no turn is left running
    with nobody watching it.

Usage: python3 tests/smoke_task_turn_handle.py [path-to-tea_agent-binary]
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
SERVER = os.path.join(REPO_ROOT, "sched", "task_server.py")


class Provider(BaseHTTPRequestHandler):
    """A tiny OpenAI-compatible mock plus the MiniMax M Plan quota endpoint."""

    calls = []

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
                "current_interval_remaining_percent": 100.0,
                "current_weekly_remaining_percent": 100.0,
                "start_time": 0,
                "end_time": 0,
                "remains_time": 3600000,
                "weekly_start_time": 0,
                "weekly_end_time": 0,
                "weekly_remains_time": 86400000,
                "current_interval_status": 1,
                "current_weekly_status": 1,
            }
            self._json(200, {"base_resp": {"status_code": 0, "status_msg": "success"},
                             "model_remains": [row]})
            return
        self._json(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        Provider.calls.append(self.path)
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            body = {}
        messages = body.get("messages") or []
        last = messages[-1] if messages else {}
        text = last.get("content") if isinstance(last.get("content"), str) else ""

        # The tasks that take their time. The queue has to survive them.
        if text and "SLOWME" in text:
            time.sleep(12.0)
            answer = "the slow answer"
        elif text and "QUICKME" in text:
            time.sleep(1.5)
            answer = "the quick answer"
        elif text and "STUCKME" in text:
            time.sleep(8.0)
            answer = "the stuck answer"
        else:
            answer = "the answer"

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self.wfile.write(("data: " + json.dumps({
            "id": "c1", "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": {"content": answer},
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


class OldAgent(BaseHTTPRequestHandler):
    """An agent from before the turn handles (0.18.8).

    It knows the old way: POST /api/chat answers with the stream itself.
    The queue has to notice that and keep working the way it always did.
    """

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
        if self.path == "/api/health":
            self._json(200, {"ok": True, "version": "0.18.8", "provider": "minimax",
                             "model": "MiniMax-M3.1-Flash",
                             "limits": {"max_iterations": 300}})
            return
        if self.path.startswith("/api/providers/") and self.path.endswith("/usage"):
            self._json(200, {"ok": True, "provider": "minimax",
                             "interval": {"remaining_percent": 100.0},
                             "weekly": {"remaining_percent": 100.0}})
            return
        if self.path == "/api/workspaces":
            self._json(200, {"ok": True, "workspaces": [
                {"id": "ws_old", "name": "old", "path": "/tmp/old-workspace"}]})
            return
        self._json(404, {"error": {"code": "not_found", "message": self.path}})

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        if self.path == "/api/workspaces":
            self._json(200, {"ok": True, "workspace": {"id": "ws_old"}})
            return
        if self.path != "/api/chat":
            self._json(404, {"error": {"code": "not_found", "message": self.path}})
            return
        # The old way: the answer is the stream.
        OldAgent.chats += 1
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for frame in (
                {"type": "start", "session_id": "s_old"},
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


def call(port, method, path, body=None, timeout=60):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    head = {"Content-Type": "application/json"} if body is not None else {}
    conn.request(method, path, body=json.dumps(body) if body is not None else None,
                 headers=head)
    resp = conn.getresponse()
    payload = resp.read()
    try:
        parsed = json.loads(payload or b"null")
    except ValueError:
        return resp.status, payload.decode("utf-8", "replace")
    if isinstance(parsed, dict) and "code" in parsed and "data" in parsed:
        return resp.status, parsed.get("data") or {}
    return resp.status, parsed


def get_task(port, ws_id, task_id):
    _, task = call(port, "GET", "/api/tasks/%s?workspace_id=%s" % (task_id, ws_id))
    return task if isinstance(task, dict) else {}


def wait_task(port, ws_id, task_id, wanted, timeout=60.0):
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        last = get_task(port, ws_id, task_id)
        if last.get("status") in wanted:
            return last
        time.sleep(0.3)
    return last


def wait_turn_id(port, ws_id, task_id, timeout=30.0):
    """Wait until the task is running and its handle is on the record."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        task = get_task(port, ws_id, task_id)
        if task.get("turn_id"):
            return task
        time.sleep(0.2)
    return get_task(port, ws_id, task_id)


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

    tmp = tempfile.mkdtemp(prefix="tea_handles_")
    home = os.path.join(tmp, "home")
    ws_dir = os.path.join(tmp, "ws")
    data_dir = os.path.join(tmp, "tasks")
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
        supports_tools: true
""" % (provider_port, provider_port))

    agent_port = free_port()
    task_port = free_port()
    agent = subprocess.Popen([
        binary, "--tea-home", home, "--host", "127.0.0.1", "--port", str(agent_port),
        "--no-console", "--log-file", os.path.join(home, "logs", "tea_agent.log"),
        "--workspace", ws_dir,
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def start_queue(port, data, chat_timeout=3600.0):
        log_path = os.path.join(tmp, "server-%d.log" % port)
        handle = open(log_path, "a")
        proc = subprocess.Popen([
            sys.executable, SERVER, "--agent", "http://127.0.0.1:%d" % agent_port,
            "--host", "127.0.0.1", "--port", str(port), "--data-dir", data,
            "--provider", "minimax", "--interval", "1", "--max-per-round", "4",
            "--workspace-path", os.path.join(tmp, "ws-%d" % port),
            # A restarted queue has to take over quickly.
            "--stale-after", "1",
            "--chat-timeout", str(chat_timeout),
        ], stdout=handle, stderr=subprocess.STDOUT, universal_newlines=True)
        return proc

    queue = start_queue(task_port, data_dir)
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

        # ---- 1) a running task carries the handle -------------------------
        _, task = call(task_port, "POST", "/api/tasks",
                       {"workspace_id": ws_id, "prompt": "do the thing", "title": "a task"})
        task_id = task.get("id")
        check("the task is created", bool(task_id), task)
        done = wait_task(task_port, ws_id, task_id, ("done", "failed"))
        check("the task finishes", done.get("status") == "done", done)
        check("the answer is on the record", done.get("result") == "the answer", done.get("result"))
        turn_id = done.get("turn_id") or ""
        check("the record carries the turn handle", bool(turn_id), done)
        status, turn = call(agent_port, "GET", "/api/turns/" + turn_id)
        check(
            "the handle is a real turn on the agent",
            status == 200 and turn.get("session_id") == done.get("session_id"),
            (status, turn),
        )
        check("the turn is finished", turn.get("state") == "done", turn)

        # ---- 2) the queue dies mid-turn: the task is taken over -----------
        _, task = call(task_port, "POST", "/api/tasks",
                       {"workspace_id": ws_id, "prompt": "SLOWME please"})
        slow_id = task.get("id")
        running = wait_turn_id(task_port, ws_id, slow_id)
        slow_turn = running.get("turn_id") or ""
        check("the slow task has a handle", bool(slow_turn), running)
        # Kill the queue while the turn is still running. The turn does not
        # die with it: the agent goes on working.
        queue.terminate()
        try:
            queue.wait(timeout=10)
        except Exception:
            queue.kill()
        check("the queue is gone", queue.poll() is not None, queue.poll())
        status, turn = call(agent_port, "GET", "/api/turns/" + slow_turn)
        check("the turn kept running without the queue",
              status == 200 and turn.get("state") == "running", (status, turn))

        queue = start_queue(task_port, data_dir)
        if not wait_health(task_port):
            print("the restarted task server did not become healthy")
            return 1
        done = wait_task(task_port, ws_id, slow_id, ("done", "failed"), timeout=60)
        check("the task was taken over, not failed", done.get("status") == "done", done)
        check("the answer still lands on the record",
              done.get("result") == "the slow answer", done.get("result"))
        check("the handle survived the restart",
              (done.get("turn_id") or "") == slow_turn, done)

        # ---- 3) the queue dies after the turn finished --------------------
        _, task = call(task_port, "POST", "/api/tasks",
                       {"workspace_id": ws_id, "prompt": "QUICKME please"})
        quick_id = task.get("id")
        running = wait_turn_id(task_port, ws_id, quick_id)
        quick_turn = running.get("turn_id") or ""
        check("the quick task has a handle", bool(quick_turn), running)
        queue.terminate()
        try:
            queue.wait(timeout=10)
        except Exception:
            queue.kill()
        # The turn finishes while nobody is watching it.
        deadline = time.time() + 20
        while time.time() < deadline:
            status, turn = call(agent_port, "GET", "/api/turns/" + quick_turn)
            if status == 200 and turn.get("state") == "done":
                break
            time.sleep(0.3)
        check("the turn finished on its own",
              status == 200 and turn.get("state") == "done", (status, turn))

        queue = start_queue(task_port, data_dir)
        if not wait_health(task_port):
            print("the restarted task server did not become healthy")
            return 1
        done = wait_task(task_port, ws_id, quick_id, ("done", "failed"), timeout=60)
        check("the task ends as done, not failed", done.get("status") == "done", done)
        check("the answer comes from the agent's status",
              done.get("result") == "the quick answer", done.get("result"))

        # ---- 4) a task that runs out of time stops its turn ---------------
        # A second queue, with a very short leash.
        short_port = free_port()
        short_data = os.path.join(tmp, "tasks-short")
        short = start_queue(short_port, short_data, chat_timeout=2.0)
        if not wait_health(short_port):
            print("the short-leash task server did not become healthy")
            return 1
        _, task = call(short_port, "POST", "/api/tasks",
                       {"workspace_id": ws_id, "prompt": "STUCKME please"})
        stuck_id = task.get("id")
        running = wait_turn_id(short_port, ws_id, stuck_id)
        stuck_turn = running.get("turn_id") or ""
        check("the stuck task has a handle", bool(stuck_turn), running)
        done = wait_task(short_port, ws_id, stuck_id, ("done", "failed"), timeout=40)
        check("the task fails when it runs out of time",
              done.get("status") == "failed", done)
        check("the reason says it was the clock",
              "time" in str(done.get("error") or "").lower(), done.get("error"))
        # The turn must not be left running: nobody watches it any more.
        deadline = time.time() + 25
        state = ""
        while time.time() < deadline:
            status, turn = call(agent_port, "GET", "/api/turns/" + stuck_turn)
            state = (turn or {}).get("state") or ""
            if state in ("stopped", "done", "failed"):
                break
            time.sleep(0.5)
        check("the turn was stopped - no orphan is left running",
              state in ("stopped", "done", "failed"), state)
        short.terminate()
        try:
            short.wait(timeout=10)
        except Exception:
            short.kill()

        # ---- 5) an agent that does not know the handles ------------------
        # The queue may point at an older a2 (a device that was never
        # updated). It still has to work - the old way.
        old_agent = ThreadingHTTPServer(("127.0.0.1", 0), OldAgent)
        old_agent.daemon_threads = True
        threading.Thread(target=old_agent.serve_forever, daemon=True).start()
        old_port = old_agent.server_address[1]
        old_queue_port = free_port()
        old_data = os.path.join(tmp, "tasks-old")
        old_queue = subprocess.Popen([
            sys.executable, SERVER, "--agent", "http://127.0.0.1:%d" % old_port,
            "--host", "127.0.0.1", "--port", str(old_queue_port), "--data-dir", old_data,
            "--provider", "minimax", "--interval", "1", "--max-per-round", "4",
            "--workspace-path", os.path.join(tmp, "ws-old"),
            "--stale-after", "1",
        ], stdout=open(os.path.join(tmp, "server-old.log"), "a"),
            stderr=subprocess.STDOUT, universal_newlines=True)
        try:
            if not wait_health(old_queue_port):
                print("the queue for the old agent did not become healthy")
                return 1
            _, task = call(old_queue_port, "POST", "/api/tasks",
                           {"workspace_id": "ws_old", "prompt": "old style please"})
            old_task_id = task.get("id")
            check("the task is created for the old agent", bool(old_task_id), task)
            done = wait_task(old_queue_port, "ws_old", old_task_id, ("done", "failed"),
                             timeout=60)
            check("the old agent still works", done.get("status") == "done", done)
            check("the answer comes from the old stream",
                  done.get("result") == "the old answer", done.get("result"))
            check("the old agent was asked for a chat", OldAgent.chats >= 1, OldAgent.chats)
        finally:
            old_queue.terminate()
            try:
                old_queue.wait(timeout=10)
            except Exception:
                old_queue.kill()
            old_agent.shutdown()
    finally:
        try:
            queue.terminate()
            queue.wait(timeout=10)
        except Exception:
            try:
                queue.kill()
            except Exception:
                pass
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
