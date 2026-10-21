#!/usr/bin/env python3
"""Smoke test for the task queue and the quota-gated scheduler.

What this pins:

  * POST /api/tasks creates a task, GET /api/tasks lists it, and the record
    survives on disk under .tea_agent_run/tasks/;
  * POST /api/tasks/{id}/start runs it: the task goes queued -> running -> done,
    and the work really happened (the mock provider was called);
  * a task whose agent asked the user something ends in waiting_confirmation, not
    in done - and the answer (POST .../confirm) starts the next turn;
  * the scheduler (sched/task_runner.py) only starts a queued task while the
    provider has quota left: with the quota at 0% the task stays queued, with
    quota it is started and finished;
  * the scheduler reports the quota it saw, so the log says why it did (or did
    not) work;
  * a task that finishes on its own publishes task_done, so a watcher (the
    browser's tab blink, a script) learns about it without polling;
  * the system prompt of a task turn says that this turn works on a queued
    task - without that the model never reports progress;
  * the record names a model of the provider that runs the work;
  * a task that was running when the agent died is recovered (failed with a
    reason) instead of sitting in the queue forever, and can be queued again.

Usage: python3 tests/smoke_task_runner.py [path-to-tea_agent-binary]
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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNNER = os.path.join(REPO_ROOT, "sched", "task_runner.py")


class Provider(BaseHTTPRequestHandler):
    """A tiny OpenAI-compatible mock plus the MiniMax M Plan quota endpoint.

    The quota is shared state, so the test can set it to "no quota left" and see
    the scheduler refuse to work.
    """

    quota_remaining = 100.0
    calls = []
    bodies = []

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
            # The shape the MiniMax M Plan endpoint answers with: one entry per
            # model, "general" being the subscription itself.
            row = {
                "model_name": "general",
                "current_interval_remaining_percent": Provider.quota_remaining,
                "current_weekly_remaining_percent": Provider.quota_remaining,
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

    def do_POST(self):  # noqa: N802 (http.server API)
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        Provider.calls.append(self.path)
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            body = {}
        Provider.bodies.append(body)
        messages = body.get("messages") or []
        last = messages[-1] if messages else {}

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()

        if last.get("role") == "tool":
            # The tool already ran; answer and let the turn finish.
            self._sse_text("understood, continuing")
        elif self._wants_confirmation(messages):
            # The agent is told to ask the user first.
            self._sse_tool_call("toolkit_task", {
                "op": "update",
                "progress": 50,
                "note": "waiting for the user",
                "needs_confirmation": True,
                "question": "may I delete the old build output?",
            })
        else:
            self._sse_text("done")

        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    @staticmethod
    def _wants_confirmation(messages):
        # Only the newest user message counts: after the user answered, the same
        # word is still in the history, and the agent would ask forever.
        for message in reversed(messages):
            if message.get("role") != "user":
                continue
            content = message.get("content")
            return isinstance(content, str) and "NEEDS_CONFIRMATION" in content
        return False

    def _sse_text(self, text):
        self.wfile.write(("data: " + json.dumps({
            "id": "c1", "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
        }) + "\n\n").encode())
        self.wfile.flush()

    def _sse_tool_call(self, name, arguments):
        self.wfile.write(("data: " + json.dumps({
            "id": "c1", "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": {"tool_calls": [{
                "index": 0, "id": "call_1", "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }]}, "finish_reason": None}],
        }) + "\n\n").encode())
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
    headers = {"Content-Type": "application/json"} if body is not None else {}
    conn.request(method, path, body=json.dumps(body) if body is not None else None,
                 headers=headers)
    resp = conn.getresponse()
    raw = resp.read()
    try:
        return resp.status, json.loads(raw or b"null")
    except ValueError:
        return resp.status, {"raw": raw.decode("utf-8", "replace")}


def wait_task_status(port, ws_id, task_id, wanted, timeout=60.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        _, task = call(port, "GET", "/api/tasks/%s?workspace_id=%s" % (task_id, ws_id))
        if isinstance(task, dict):
            last = task
            if task.get("status") in wanted:
                return task
        time.sleep(0.4)
    return last


def main():
    binary = (sys.argv[1] if len(sys.argv) > 1
              else os.path.join(REPO_ROOT, "build", "bin", "tea_agent"))
    if not os.path.exists(binary):
        print("binary not found:", binary)
        return 1
    if not os.path.exists(RUNNER):
        print("scheduler not found:", RUNNER)
        return 1

    provider = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    provider.daemon_threads = True
    threading.Thread(target=provider.serve_forever, daemon=True).start()
    provider_port = provider.server_address[1]

    tmp = tempfile.mkdtemp(prefix="tea_tasks_")
    home = os.path.join(tmp, "home")
    ws_dir = os.path.join(tmp, "ws")
    os.makedirs(home)
    os.makedirs(ws_dir)
    config_path = os.path.join(home, "provider.yaml")
    with open(config_path, "w", encoding="utf-8") as f:
        # The first provider is the active one - and it is NOT the provider the
        # tasks run on, so the record has to name the model of the provider that
        # does the work (not the active one).
        f.write("""version: 1
providers:
  other:
    api_url: http://127.0.0.1:%d/v1
    api_key: sk-other
    models:
      other-model:
        max_context_tokens: 1000000
        max_output_tokens: 128000
        supports_tools: true
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
""" % (provider_port, provider_port, provider_port))

    port = free_port()
    proc = subprocess.Popen([
        binary, "--tea-home", home, "--host", "127.0.0.1", "--port", str(port),
        "--no-console", "--log-file", os.path.join(home, "logs", "tea_agent.log"),
        "--workspace", ws_dir,
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    failures = []
    checks = []

    def check(name, ok, detail=""):
        checks.append(name)
        print(("PASS " if ok else "FAIL ") + name + (("  " + str(detail)) if not ok else ""))
        if not ok:
            failures.append(name)

    try:
        if not wait_health(port):
            print("server did not become healthy")
            return 1
        _, listing = call(port, "GET", "/api/workspaces")
        ws_id = listing["workspaces"][0]["id"]

        # ---- create a task ------------------------------------------------
        status, task = call(port, "POST", "/api/tasks",
                            {"workspace_id": ws_id, "prompt": "do the thing",
                             "title": "a task"})
        check("POST /api/tasks creates a task", status == 200 and task.get("id"), (status, task))
        check("a new task is queued", task.get("status") == "queued", task.get("status"))
        check("the task keeps its prompt and title",
              task.get("prompt") == "do the thing" and task.get("title") == "a task", task)
        task_id = task.get("id")

        _, listing = call(port, "GET", "/api/tasks?workspace_id=" + ws_id)
        check("GET /api/tasks lists it", len(listing.get("tasks") or []) == 1, listing)
        check("the counts say one is queued",
              (listing.get("counts") or {}).get("queued") == 1, listing.get("counts"))
        task_file = os.path.join(ws_dir, ".tea_agent_run", "tasks", task_id + ".json")
        check("the task is on disk", os.path.isfile(task_file), task_file)

        # ---- the scheduler must not work without quota --------------------
        Provider.quota_remaining = 0.0
        Provider.calls = []
        result = subprocess.run(
            [sys.executable, RUNNER, "--base", "http://127.0.0.1:%d" % port,
             "--workspace", ws_id, "--provider", "minimax", "--once", "--poll", "0.5"],
            capture_output=True, text=True, timeout=120)
        check("the scheduler exits cleanly", result.returncode == 0, result.stderr[-300:])
        check("the scheduler says the quota is exhausted",
              "0.0% left" in result.stdout or "0%" in result.stdout, result.stdout[-300:])
        _, listing = call(port, "GET", "/api/tasks?workspace_id=" + ws_id)
        check("the task is still queued without quota",
              (listing.get("counts") or {}).get("queued") == 1, listing.get("counts"))
        check("no model call was made without quota", len(Provider.calls) == 0, Provider.calls)

        # ---- with quota the scheduler works the queue ---------------------
        Provider.quota_remaining = 60.0
        result = subprocess.run(
            [sys.executable, RUNNER, "--base", "http://127.0.0.1:%d" % port,
             "--workspace", ws_id, "--provider", "minimax", "--once", "--poll", "0.5",
             "--timeout", "60"],
            capture_output=True, text=True, timeout=180)
        check("the scheduler exits cleanly", result.returncode == 0, result.stderr[-300:])
        check("the scheduler reports the quota it saw",
              "60.0% left" in result.stdout, result.stdout[-300:])
        final = wait_task_status(port, ws_id, task_id, ("done", "failed"), timeout=60)
        check("the task was started and finished",
              final and final.get("status") == "done", final)
        check("the work really reached the provider", len(Provider.calls) >= 1, Provider.calls)
        check("a finished task is at 100%",
              final and final.get("progress") == 100, final)

        # ---- a task that needs a human ------------------------------------
        status, task2 = call(port, "POST", "/api/tasks",
                             {"workspace_id": ws_id, "prompt": "ask the user"})
        check("a second task can be queued", status == 200, (status, task2))
        task2_id = task2.get("id")
        # Put the task into the waiting state the way the agent would.
        call(port, "POST", "/api/tasks/%s/start?workspace_id=%s" % (task2_id, ws_id), {})
        time.sleep(2)
        _, one = call(port, "GET", "/api/tasks/%s?workspace_id=%s" % (task2_id, ws_id))
        check("a started task names its session", bool(one.get("session_id")), one)

        # ---- a task that needs the user ------------------------------------
        status, task4 = call(port, "POST", "/api/tasks",
                             {"workspace_id": ws_id, "prompt": "NEEDS_CONFIRMATION please"})
        check("a task that asks for a confirmation can be queued", status == 200, (status, task4))
        task4_id = task4.get("id")
        call(port, "POST", "/api/tasks/%s/start?workspace_id=%s" % (task4_id, ws_id), {})
        waiting = wait_task_status(port, ws_id, task4_id,
                                 ("waiting_confirmation", "done", "failed"), timeout=60)
        check("a task that asked the user waits instead of claiming to be done",
              waiting and waiting.get("status") == "waiting_confirmation", waiting)
        check("the question is in the task record",
              waiting and "may I delete" in (waiting.get("question") or ""), waiting)
        check("the progress made before the question is kept",
              waiting and waiting.get("progress") == 50, waiting)
        check("the intermediate state is recorded",
              waiting and len(waiting.get("steps") or []) >= 1, waiting)

        # The answer continues the work in the same session.
        status, answered = call(port, "POST","/api/tasks/%s/confirm?workspace_id=%s"
                               % (task4_id, ws_id), {"message": "yes, go ahead"})
        check("the user can answer a waiting task", status == 200, (status, answered))
        check("answering clears the confirmation flag",
              answered.get("needs_confirmation") is False, answered)
        final4 = wait_task_status(port, ws_id, task4_id, ("done", "failed"), timeout=60)
        check("the task finishes after the answer",
              final4 and final4.get("status") == "done", final4)

        # ---- the events a watcher waits for -------------------------------
        # A task that finishes on its own has to say so. Without the event the
        # browser never blinks and a script that waits for "task_done" waits
        # forever.
        events = []

        def listen_events():
            try:
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
                conn.request("GET", "/api/events?workspace_id=" + ws_id)
                resp = conn.getresponse()
                while True:
                    line = resp.readline()
                    if not line:
                        break
                    line = line.strip()
                    if not line.startswith(b"data: "):
                        continue
                    try:
                        events.append(json.loads(line[6:]))
                    except ValueError:
                        pass
            except Exception:
                pass

        listener = threading.Thread(target=listen_events, daemon=True)
        listener.start()
        time.sleep(0.5)
        status, ev_task = call(port, "POST", "/api/tasks",
                               {"workspace_id": ws_id, "prompt": "say it with an event",
                                "title": "event task"})
        ev_id = ev_task.get("id")
        # The scheduler starts the work on the provider whose quota it checked -
        # here that is minimax, while the *active* provider is another one.
        call(port, "POST", "/api/tasks/%s/start?workspace_id=%s" % (ev_id, ws_id),
             {"provider": "minimax"})
        final = wait_task_status(port, ws_id, ev_id, ("done", "failed"), timeout=60)
        check("the event task finished", final and final.get("status") == "done", final)
        deadline = time.time() + 10
        types = []
        while time.time() < deadline:
            types = [e.get("type") for e in events]
            if "task_done" in types:
                break
            time.sleep(0.3)
        check("a finished task publishes task_done", "task_done" in types, types)
        check("the event carries the task record",
              any(e.get("type") == "task_done" and e.get("task", {}).get("id") == ev_id
                  for e in events), [e.get("type") for e in events])
        check("the event stream also said the task started",
              "task_started" in types, types)

        # ---- the model of the record belongs to its provider --------------
        _, one = call(port, "GET", "/api/tasks/%s?workspace_id=%s" % (ev_id, ws_id))
        check("the record names a model of the provider that ran the work",
              one.get("provider") == "minimax" and one.get("model") == "MiniMax-M3.1-Flash",
              (one.get("provider"), one.get("model")))

        # ---- the model is told that this turn works on a task -------------
        prompt = ""
        for body in Provider.bodies:
            for message in body.get("messages") or []:
                if message.get("role") == "system":
                    prompt = message.get("content") or ""
        check("the system prompt says the turn works on a queued task",
              "QUEUED TASK" in prompt, prompt[-400:])
        check("the system prompt names the task",
              ev_id in prompt, prompt[-400:])
        check("the system prompt tells the model to report on it",
              "toolkit_task" in prompt, prompt[-400:])

        # ---- a task that was running when the agent died ------------------
        status, zombie = call(port, "POST", "/api/tasks",
                              {"workspace_id": ws_id, "prompt": "work that was interrupted",
                               "title": "zombie"})
        zombie_id = zombie.get("id")
        zombie_file = os.path.join(ws_dir, ".tea_agent_run", "tasks", zombie_id + ".json")
        record = json.load(open(zombie_file, encoding="utf-8"))
        record["status"] = "running"
        record["started_at"] = "2020-01-01T00:00:00Z"
        record["updated_at"] = "2020-01-01T00:00:00Z"
        with open(zombie_file, "w", encoding="utf-8") as f:
            json.dump(record, f)

        # Restart the agent: the task was "running" when the process died.
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        proc = subprocess.Popen([
            binary, "--tea-home", home, "--host", "127.0.0.1", "--port", str(port),
            "--no-console", "--log-file", os.path.join(home, "logs", "tea_agent.log"),
            "--workspace", ws_dir,
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not wait_health(port):
            print("server did not come back")
            return 1
        _, one = call(port, "GET", "/api/tasks/%s?workspace_id=%s" % (zombie_id, ws_id))
        check("a task left running by a crash is recovered",
              one.get("status") == "failed", one)
        check("the recovery says what happened",
              "restarted" in (one.get("error") or ""), one.get("error"))
        status, patched = call(port, "PATCH", "/api/tasks/%s?workspace_id=%s" % (zombie_id, ws_id),
                               {"status": "queued"})
        check("a recovered task can be queued again",
              status == 200 and patched.get("status") == "queued", (status, patched))

        # ---- a task of another provider is left alone --------------------
        status, task3 = call(port, "POST", "/api/tasks",
                             {"workspace_id": ws_id, "prompt": "other provider",
                              "provider": "somebody-else"})
        check("a task can name its provider", status == 200, (status, task3))
        Provider.quota_remaining = 60.0
        result = subprocess.run(
            [sys.executable, RUNNER, "--base", "http://127.0.0.1:%d" % port,
             "--workspace", ws_id, "--provider", "minimax", "--once", "--poll", "0.5",
             "--max-per-round", "10"],
            capture_output=True, text=True, timeout=120)
        check("the scheduler leaves a task of another provider alone",
              "wants provider" in result.stdout, result.stdout[-300:])
        _, listing = call(port, "GET", "/api/tasks?workspace_id=" + ws_id)
        queued = [t for t in listing.get("tasks") or [] if t.get("id") == task3.get("id")]
        check("that task is still queued",
              queued and queued[0].get("status") == "queued", queued)
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        provider.shutdown()

    print()
    print("%d/%d checks passed" % (len(checks) - len(failures), len(checks)))
    if failures:
        print("FAILED:", ", ".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
