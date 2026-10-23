#!/usr/bin/env python3
"""Smoke test for the task queue (the Python task server) and the agent channel.

What this pins:

  * the queue lives in the task server, not in the agent: the agent has no
    /api/tasks at all, the task server has the whole task API;
  * enqueue / list / get / delete work over HTTP;
  * the work starts only while the provider has quota left: with the quota at
    0% the task stays queued and not a single model request is made, with quota
    it is started and finished;
  * the progress comes from the agent's own report: the task server reads the
    toolkit_task call out of the turn's event stream (the agent does not keep
    task records);
  * a task that asks the user something waits for the answer and continues in
    the same session afterwards;
  * the results are kept: the final answer, the progress, the steps.

Usage: python3 tests/smoke_task_server.py [path-to-tea_agent-binary]
"""

import http.client
import json
import os
import random
import select
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

    quota_remaining = 100.0
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
        if isinstance(last.get("content"), str) and "SLOWME" in last["content"]:
            # A task that takes a while: the queue must not start the next one
            # in the same project, but another project may work in parallel.
            time.sleep(2.0)
        if isinstance(last.get("content"), str) and "FAILME" in last["content"]:
            # A task that cannot be done: the agent's turn fails.
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":{"message":"the model is on fire"}}')
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()

        if last.get("role") == "tool":
            # The report was made; answer and let the turn finish.
            self._sse_text("understood, continuing")
        elif self._wants_confirmation(messages):
            self._sse_tool_call("toolkit_task", {
                "op": "update",
                "progress": 50,
                "note": "waiting for the user",
                "needs_confirmation": True,
                "question": "may I delete the old build output?",
            })
        else:
            # Report the progress first - the task server reads this call out
            # of the event stream, exactly as it does with a real model.
            self._sse_tool_call("toolkit_task", {
                "op": "update",
                "progress": 80,
                "note": "the work is done",
            })
            self._sse_text("done")

        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    @staticmethod
    def _wants_confirmation(messages):
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


def call(port, method, path, body=None, timeout=60, headers=None, raw=False):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    head = {"Content-Type": "application/json"} if body is not None else {}
    head.update(headers or {})
    conn.request(method, path, body=json.dumps(body) if body is not None else None,
                 headers=head)
    resp = conn.getresponse()
    payload = resp.read()
    try:
        parsed = json.loads(payload or b"null")
    except ValueError:
        # Not JSON (the page): hand back the text.
        return resp.status, payload.decode("utf-8", "replace")
    if raw:
        return resp.status, parsed
    # The queue answers in one shape: {code, message, request_id, data}. The
    # checks want the data (an error puts its detail there too).
    if isinstance(parsed, dict) and "code" in parsed and "data" in parsed:
        return resp.status, parsed.get("data") or {}
    return resp.status, parsed


def wait_task_status(base_port, ws_id, task_id, wanted, timeout=90.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        _, task = call(base_port, "GET", "/api/tasks/%s?workspace_id=%s" % (task_id, ws_id))
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
    if not os.path.exists(SERVER):
        print("task server not found:", SERVER)
        return 1

    provider = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    provider.daemon_threads = True
    threading.Thread(target=provider.serve_forever, daemon=True).start()
    provider_port = provider.server_address[1]

    tmp = tempfile.mkdtemp(prefix="tea_tasks_")
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
    task_ws_path = os.path.join(tmp, "task-workspace")
    task_server_cmd = [
        sys.executable, SERVER, "--agent", "http://127.0.0.1:%d" % agent_port,
        "--host", "127.0.0.1", "--port", str(task_port), "--data-dir", data_dir,
        "--provider", "minimax", "--interval", "2", "--max-per-round", "4",
        "--workspace-path", task_ws_path,
    ]
    # The server's output goes to a file, not to a pipe: a pipe nobody reads
    # fills up after a while, and then the server blocks on the write - the
    # scheduler stops and the tasks never start.
    server_log = open(os.path.join(tmp, "server.log"), "w")
    task_server = subprocess.Popen(task_server_cmd,
                                   stdout=server_log, stderr=subprocess.STDOUT, universal_newlines=True)

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

        # ---- the queue is not in the agent any more ------------------------
        status, body = call(agent_port, "GET", "/api/tasks?workspace_id=" + ws_id)
        check("the agent has no task API", status == 404, (status, body))
        status, body = call(agent_port, "POST", "/api/tasks",
                            {"workspace_id": ws_id, "prompt": "x"})
        check("the agent does not create tasks either", status == 404, (status, body))

        # ---- the task server's own health -----------------------------------
        status, health = call(task_port, "GET", "/api/health")
        check("the task server answers", status == 200 and health.get("ok") is True, health)
        check("the task server knows the agent",
              health.get("agent_ok") is True and health.get("provider") == "minimax", health)

        # ---- enqueue --------------------------------------------------------
        status, task = call(task_port, "POST", "/api/tasks",
                            {"workspace_id": ws_id, "prompt": "do the thing",
                             "title": "a task"})
        check("POST /api/tasks creates a task", status == 200 and task.get("id"), (status, task))
        check("a new task is queued", task.get("status") == "queued", task.get("status"))
        check("the task keeps its prompt and title",
              task.get("prompt") == "do the thing" and task.get("title") == "a task", task)
        task_id = task.get("id")

        status, listing = call(task_port, "GET", "/api/tasks?workspace_id=" + ws_id)
        check("GET /api/tasks lists it",
              status == 200 and len(listing.get("tasks") or []) == 1, listing)
        check("the counts say one is queued",
              (listing.get("counts") or {}).get("queued") == 1, listing.get("counts"))
        status, one = call(task_port, "GET", "/api/tasks/%s?workspace_id=%s" % (task_id, ws_id))
        check("GET /api/tasks/{id} reads one", status == 200 and one.get("id") == task_id, one)

        # ---- the quota gate -------------------------------------------------
        Provider.quota_remaining = 0.0
        Provider.calls = []
        time.sleep(6)
        _, one = call(task_port, "GET", "/api/tasks/%s?workspace_id=%s" % (task_id, ws_id))
        check("without quota the task stays queued",
              one.get("status") == "queued", one.get("status"))
        check("without quota not a single model request is made",
              len(Provider.calls) == 0, Provider.calls)

        # ---- with quota the queue is worked ---------------------------------
        Provider.quota_remaining = 60.0
        final = wait_task_status(task_port, ws_id, task_id, ("done", "failed"), timeout=90)
        check("the task was started and finished",
              final and final.get("status") == "done", final)
        check("the work really reached the provider", len(Provider.calls) >= 1, Provider.calls)
        check("a finished task is at 100%", final and final.get("progress") == 100, final)
        check("the agent's report became the task's note",
              final and "the work is done" in (final.get("note") or ""), final.get("note"))
        check("the intermediate state is recorded",
              final and len(final.get("steps") or []) >= 1, final.get("steps"))
        check("the result is kept",
              final and "understood" in (final.get("result") or ""), final.get("result"))

        # ---- a task that asks the user --------------------------------------
        status, task2 = call(task_port, "POST", "/api/tasks",
                             {"workspace_id": ws_id, "prompt": "NEEDS_CONFIRMATION please"})
        check("a second task can be queued", status == 200, (status, task2))
        waiting = wait_task_status(task_port, ws_id, task2.get("id"),
                                   ("waiting_confirmation", "done", "failed"), timeout=90)
        check("a task that asked the user waits instead of claiming to be done",
              waiting and waiting.get("status") == "waiting_confirmation", waiting)
        check("the question is in the task record",
              waiting and "delete the old build" in (waiting.get("question") or ""),
              waiting.get("question"))
        status, answered = call(task_port, "POST",
                                "/api/tasks/%s/confirm?workspace_id=%s" % (task2.get("id"), ws_id),
                                {"message": "yes, go ahead"})
        check("the user can answer a waiting task", status == 200, (status, answered))
        # The answer does not interrupt what is running: the task goes back
        # into the queue and waits its turn.
        check("the answered task goes back into the queue",
              answered.get("status") == "queued", answered.get("status"))
        check("the task carries the answer with it",
              answered.get("pending_message"), answered)
        done2 = wait_task_status(task_port, ws_id, task2.get("id"), ("done", "failed"), timeout=90)
        check("the task finishes after the answer",
              done2 and done2.get("status") == "done", done2)
        check("the answer reached the agent",
              done2 and done2.get("result") and "understood" in done2.get("result", ""),
              done2 and done2.get("result"))

        # ---- delete ---------------------------------------------------------
        status, deleted = call(task_port, "DELETE",
                               "/api/tasks/%s?workspace_id=%s" % (task2.get("id"), ws_id))
        check("a task can be deleted", status == 200 and deleted.get("ok") is True, deleted)
        status, one = call(task_port, "GET", "/api/tasks/%s?workspace_id=%s" % (task2.get("id"), ws_id))
        check("the deleted task is gone", status == 404, (status, one))

        # ---- the events a watcher waits for ---------------------------------
        events = []

        def listen_events():
            try:
                conn = http.client.HTTPConnection("127.0.0.1", task_port, timeout=60)
                conn.request("GET", "/api/events")
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
        status, task3 = call(task_port, "POST", "/api/tasks",
                             {"workspace_id": ws_id, "prompt": "say it with an event"})
        final3 = wait_task_status(task_port, ws_id, task3.get("id"), ("done", "failed"), timeout=90)
        check("the event task finished", final3 and final3.get("status") == "done", final3)
        deadline = time.time() + 10
        types = []
        while time.time() < deadline:
            types = [e.get("type") for e in events]
            if "task_done" in types:
                break
            time.sleep(0.3)
        check("a finished task publishes task_done", "task_done" in types, types)
        check("the event carries the task record",
              any(e.get("type") == "task_done" and e.get("task", {}).get("id") == task3.get("id")
                  for e in events), types)

        # ---- no workspace named: it is found anyway ------------------------
        # The task ids are unique, so the workspace is optional everywhere: a
        # task that does not name one goes to the first one, and a lookup finds
        # it wherever it lives.
        status, plain = call(task_port, "POST", "/api/tasks",
                             {"prompt": "no workspace named"})
        check("a task can be queued without naming a workspace",
              status == 200 and bool(plain.get("workspace_id")), (status, plain))
        check("the queue works in a workspace of its own",
              plain.get("workspace_id") != ws_id and os.path.isdir(task_ws_path),
              (plain.get("workspace_id"), task_ws_path))
        _, listed = call(agent_port, "GET", "/api/workspaces")
        check("the agent knows the queue's workspace",
              any(w.get("id") == plain.get("workspace_id") for w in listed.get("workspaces", [])),
              [w.get("path") for w in listed.get("workspaces", [])])
        status, gone = call(task_port, "DELETE", "/api/tasks/" + plain.get("id", ""))
        check("a task can be deleted without naming a workspace",
              status == 200 and gone.get("ok") is True, (status, gone))
        status, _ = call(task_port, "GET", "/api/tasks/" + plain.get("id", ""))
        check("the deleted task is gone", status == 404, status)
        status, body = call(task_port, "POST", "/api/tasks",
                            {"prompt": "x", "workspace_id": "ws_nope"})
        check("a task for a workspace nobody has is refused",
              status == 400, (status, body))

        # ---- a task that may touch another directory ------------------------
        status, allowed = call(task_port, "POST", "/api/tasks",
                               {"prompt": "may look elsewhere",
                                "allow_paths": ["/tmp/one", "/tmp/two"]})
        check("a task can ask for extra directories",
              status == 200 and allowed.get("allow_paths") == ["/tmp/one", "/tmp/two"],
              (status, allowed.get("allow_paths")))
        status, body = call(task_port, "POST", "/api/tasks",
                            {"prompt": "x", "allow_paths": "not/a/list"})
        check("a broken allow_paths is ignored, not fatal", status == 200, (status, body))
        call(task_port, "DELETE", "/api/tasks/" + allowed.get("id", ""))

        # ---- the page ------------------------------------------------------
        status, page = call(task_port, "GET", "/")
        check("the task page is served", status == 200 and "排队中" in str(page), status)
        check("the page has a section for the finished tasks",
              "已完成" in str(page) and "进行中" in str(page), "")
        status, one = call(task_port, "GET", "/api/tasks/%s?workspace_id=%s" % (task_id, ws_id))
        check("a task says where its record lives",
              str(one.get("record_path", "")).endswith(task_id + ".json"), one.get("record_path"))
        check("a task says which agent to reach",
              one.get("agent_url") == "http://127.0.0.1:%d" % agent_port, one.get("agent_url"))
        check("a task says which workspace it works in",
              bool(one.get("workspace_path")), one.get("workspace_path"))
        status, health = call(task_port, "GET", "/api/health")
        check("the health reports the quota left",
              isinstance(health.get("quota_percent"), (int, float)), health.get("quota_percent"))

        # ---- a provider without a quota -------------------------------------
        status, task4 = call(task_port, "POST", "/api/tasks",
                             {"workspace_id": ws_id, "prompt": "no quota here",
                              "provider": "somebody-else"})
        check("a task can name its provider", status == 200, (status, task4))
        time.sleep(6)
        _, one = call(task_port, "GET", "/api/tasks/%s?workspace_id=%s" % (task4.get("id"), ws_id))
        check("a task of another provider is left alone",
              one.get("status") == "queued", one.get("status"))

        # ---- the queue is serial --------------------------------------------
        # Two tasks, one provider: the second must wait for the first. The
        # provider answers in a moment, so a queue that starts both would show
        # two RUNNING tasks at the same time.
        Provider.quota_remaining = 100.0
        _, first = call(task_port, "POST", "/api/tasks",
                        {"workspace_id": ws_id, "prompt": "serial one"})
        _, second = call(task_port, "POST", "/api/tasks",
                         {"workspace_id": ws_id, "prompt": "serial two"})
        worst = 0
        for _ in range(40):
            _, listing = call(task_port, "GET", "/api/tasks?workspace_id=" + ws_id)
            running = [t for t in (listing.get("tasks") or []) if t.get("status") == "running"]
            worst = max(worst, len(running))
            if len(listing.get("tasks") or []) >= 2 and all(
                    t.get("status") in ("done", "failed")
                    for t in listing.get("tasks") or []):
                break
            time.sleep(0.3)
        check("only one task runs at a time", worst <= 1, "max concurrent: %d" % worst)

        # ---- the queue position ---------------------------------------------
        # The quota is closed, so the tasks stay in the line and the order is
        # the queue's to decide.
        Provider.quota_remaining = 0.0
        time.sleep(3)
        _, third = call(task_port, "POST", "/api/tasks",
                        {"workspace_id": ws_id, "prompt": "third", "priority": 5})
        _, fourth = call(task_port, "POST", "/api/tasks",
                         {"workspace_id": ws_id, "prompt": "fourth"})
        check("a queued task knows where it stands",
              third.get("queue_position", 0) >= 1, third.get("queue_position"))
        check("the queue position counts the tasks in front",
              fourth.get("queue_position", 0) > third.get("queue_position", 0),
              (third.get("queue_position"), fourth.get("queue_position")))
        check("a task that is not queued has no position",
              call(task_port, "GET", "/api/tasks/%s?workspace_id=%s"
                   % (first.get("id"), ws_id))[1].get("queue_position", 0) == 0,
              first.get("id"))
        # A higher priority runs first.
        Provider.quota_remaining = 100.0
        deadline = time.time() + 60
        while time.time() < deadline:
            _, third_now = call(task_port, "GET", "/api/tasks/%s?workspace_id=%s"
                                % (third.get("id"), ws_id))
            if third_now.get("started_at"):
                break
            time.sleep(0.3)
        _, fourth_now = call(task_port, "GET", "/api/tasks/%s?workspace_id=%s"
                             % (fourth.get("id"), ws_id))
        check("the priority decides who runs first",
              third_now.get("started_at")
              and (not fourth_now.get("started_at")
                   or third_now.get("started_at") <= fourth_now.get("started_at")),
              (third_now.get("started_at"), fourth_now.get("started_at")))

        # ---- the idempotency key --------------------------------------------
        key = "key-%d" % random.randint(1, 999999)
        status, one = call(task_port, "POST", "/api/tasks",
                           {"workspace_id": ws_id, "prompt": "only once"},
                           headers={"Idempotency-Key": key})
        status2, again = call(task_port, "POST", "/api/tasks",
                              {"workspace_id": ws_id, "prompt": "only once"},
                              headers={"Idempotency-Key": key})
        check("the same idempotency key does not queue the work twice",
              one.get("id") and again.get("id") == one.get("id"), (one.get("id"),
                                                                  again.get("id")))
        call(task_port, "DELETE", "/api/tasks/%s?workspace_id=%s" % (one.get("id"), ws_id))

        # ---- the queue is serial per project ---------------------------------
        # Two fresh projects: each works on one task at a time, but the two may
        # run at the same time. (Fresh, because the first project still has
        # work from the checks above - and a busy project is exactly what
        # makes the other one wait.)
        parallel = []
        for name in ("parallel-a", "parallel-b"):
            path = os.path.join(tmp, name)
            os.makedirs(path, exist_ok=True)
            _, created = call(agent_port, "POST", "/api/workspaces",
                              {"path": path, "name": name})
            parallel.append(created.get("id") or "")
        check("two more projects are registered", all(parallel), parallel)
        slow_tasks = []
        for workspace_id in parallel:
            _, created = call(task_port, "POST", "/api/tasks",
                              {"workspace_id": workspace_id,
                               "prompt": "SLOWME in this project"})
            slow_tasks.append(created.get("id"))
        both = 0
        for _ in range(80):
            _, listing = call(task_port, "GET", "/api/tasks")
            running = [t for t in (listing.get("tasks") or []) if t.get("status") == "running"]
            projects = set(t.get("workspace_id") for t in running)
            both = max(both, len(projects))
            if len(running) >= 2:
                break
            time.sleep(0.3)
        check("two projects may work at the same time", both >= 2, both)
        # The same project: one at a time.
        for i in range(2):
            call(task_port, "POST", "/api/tasks",
                 {"workspace_id": parallel[0], "prompt": "SLOWME again %d" % i})
        worst = 0
        for _ in range(80):
            _, listing = call(task_port, "GET", "/api/tasks?workspace_id=" + parallel[0])
            running = [t for t in (listing.get("tasks") or []) if t.get("status") == "running"]
            worst = max(worst, len(running))
            if len(running) >= 2:
                break
            time.sleep(0.3)
        check("one project works on one task at a time", worst <= 1, worst)

        # ---- the project key --------------------------------------------------
        status, key_answer = call(task_port, "POST", "/api/projects",
                                  {"workspace_id": ws_id})
        key = key_answer.get("key") or ""
        check("the operator can ask for a project's key",
              status == 200 and len(key) > 10, (status, key_answer))
        auth = {"Authorization": "Bearer " + key}
        status, mine = call(task_port, "GET", "/api/tasks", headers=auth)
        check("a key sees only its own project",
              status == 200 and all(t.get("workspace_id") == ws_id
                                    for t in (mine.get("tasks") or [])), mine)
        status, denied = call(task_port, "GET", "/api/tasks/%s?workspace_id=%s"
                              % (slow_tasks[1], parallel[1]), headers=auth)
        check("a key cannot read another project's task", status == 403, (status, denied))
        status, denied = call(task_port, "POST", "/api/tasks",
                              {"workspace_id": parallel[1], "prompt": "not yours"},
                              headers=auth)
        check("a key cannot queue work in another project", status == 403, (status, denied))
        status, denied = call(task_port, "POST", "/api/projects",
                              {"workspace_id": parallel[1]}, headers=auth)
        check("a key cannot ask for another project's key", status == 403, (status, denied))
        status, denied = call(task_port, "GET", "/api/tasks/%s/export?workspace_id=%s"
                              % (slow_tasks[1], parallel[1]), headers=auth)
        check("a key cannot export another project's task", status == 403, (status, denied))
        status, ok = call(task_port, "GET", "/api/tasks?workspace_id=%s" % ws_id, headers=auth)
        check("a key can read its own task", status == 200, (status, ok))
        # The key file lives in the same directory as the task records: it must
        # not turn up as a task.
        status, plain = call(task_port, "GET", "/api/tasks")
        check("the key file is not mistaken for a task",
              status == 200 and all(t.get("id") for t in (plain.get("tasks") or [])),
              (status, plain))

        # ---- the key is a door, not a master key -----------------------------
        # The audit log holds every project's business and the submit line
        # carries the prompt: a key reads its own lines and no others.
        status, foreign_audit = call(task_port, "GET", "/api/audit?task_id=" + slow_tasks[1],
                                     headers=auth)
        check("a key does not read another project's audit line",
              status == 200 and not any(e.get("task_id") == slow_tasks[1]
                                        for e in (foreign_audit.get("entries") or [])),
              (status, foreign_audit))
        status, roster = call(task_port, "GET", "/api/projects", headers=auth)
        check("a key does not see the project roster",
              status == 200 and not [p for p in (roster.get("projects") or [])
                                     if p.get("workspace_id") == parallel[0]],
              (status, roster))
        status, wrong = call(task_port, "GET", "/api/tasks",
                              headers={"Authorization": "Bearer NOT-A-REAL-KEY"})
        check("a key that belongs to nobody is refused",
              status == 401, (status, wrong))
        status, own = call(task_port, "GET", "/api/tasks", headers=auth)
        check("the counts belong to the caller's project",
              status == 200 and sum((own.get("counts") or {}).values())
              == len(own.get("tasks") or []), (status, own.get("counts")))

        # ---- the operator sees everything, a key sees its own project ------
        # The operator is the person on the machine: no key, so no project to
        # narrow down.  The web page on the same machine is the operator.
        status, other = call(task_port, "GET", "/api/tasks/%s?workspace_id=%s"
                             % (slow_tasks[1], parallel[1]))
        check("the operator sees another project's task",
              status == 200 and other.get("prompt") == "SLOWME in this project",
              (status, other.get("prompt")))
        status, whole = call(task_port, "GET", "/api/tasks")
        projects = set(t.get("workspace_id") for t in (whole.get("tasks") or []))
        check("the operator sees every project's tasks",
              status == 200 and len(projects) > 1, sorted(projects))

        # ---- the event stream is a project stream, not a broadcast ----------
        # The stream carries the prompt with the event, so a key that hears
        # every project learns what the other projects are doing.  The
        # operator (no key) hears everything - that is the point of it.
        marker = "STREAMMARK %d" % int(time.time() * 1000)
        operator_sink, key_sink = [], []

        def collect_stream(headers, sink, seconds=4.0):
            """Read the SSE stream on a raw socket.

            A plain readline() with a socket timeout poisons the connection
            on the first timeout, so the wait is done with select() and the
            bytes are read raw - the chunk framing does not matter here, we
            only look for names.
            """
            sock = socket.create_connection(("127.0.0.1", task_port), timeout=10)
            request = "GET /api/events HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n" % task_port
            for name, value in (headers or {}).items():
                request += "%s: %s\r\n" % (name, value)
            request += "\r\n"
            sock.sendall(request.encode("utf-8"))
            sock.setblocking(False)
            deadline = time.time() + seconds
            while time.time() < deadline:
                if not select.select([sock], [], [], 0.2)[0]:
                    continue
                try:
                    chunk = sock.recv(65536)
                except (BlockingIOError, socket.timeout):
                    continue
                except OSError:
                    break
                if not chunk:
                    break
                sink.append(chunk.decode("utf-8", "replace"))
            try:
                sock.close()
            except OSError:
                pass

        operator_thread = threading.Thread(target=collect_stream, args=({}, operator_sink))
        key_thread = threading.Thread(target=collect_stream, args=(auth, key_sink))
        operator_thread.start()
        key_thread.start()
        time.sleep(0.8)
        # The trigger: a task of the *other* project, waiting for its time so
        # that no work starts - only the event matters here.
        _, streamed = call(task_port, "POST", "/api/tasks",
                           {"workspace_id": parallel[1], "prompt": marker,
                            "not_before": "+1d"})
        streamed_id = streamed.get("id") or ""
        time.sleep(2.0)
        operator_thread.join(timeout=8)
        key_thread.join(timeout=8)
        operator_text = "".join(operator_sink)
        key_text = "".join(key_sink)
        check("the operator's event stream carries every project",
              "200 OK" in operator_text and streamed_id in operator_text
              and marker in operator_text, len(operator_sink))
        check("a key's event stream carries only its own project",
              "200 OK" in key_text and streamed_id not in key_text
              and marker not in key_text, len(key_sink))
        status, foreign_audit = call(task_port, "GET", "/api/audit", headers=auth)
        check("a key does not read another project's prompt in the audit log",
              status == 200 and marker not in json.dumps(foreign_audit), status)
        call(task_port, "DELETE", "/api/tasks/%s?workspace_id=%s"
             % (streamed_id, parallel[1]))

        # ---- "not before": a task that waits for its time --------------------
        # '+5' is five hours from now, '23:30' is today at 23:30 (and if that
        # hour has passed, the task may start right away).
        status, later = call(task_port, "POST", "/api/tasks",
                             {"workspace_id": ws_id, "prompt": "later", "not_before": "+5"})
        check("a task can be scheduled",
              status == 200 and later.get("not_before") and later.get("not_before_text") == "+5",
              (status, later))
        check("the schedule is shown in the task's own time",
              bool(later.get("starts_at_local")), later.get("starts_at_local"))
        status, past = call(task_port, "POST", "/api/tasks",
                            {"workspace_id": ws_id, "prompt": "now", "not_before": "00:01"})
        check("a time that has passed today means 'as soon as possible'",
              status == 200 and past.get("not_before"), (status, past))
        now_minute = int(time.time() // 60) * 60
        check("the schedule of a passed hour is now, not tomorrow",
              past.get("not_before") == time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now_minute)),
              past.get("not_before"))
        status, nonsense = call(task_port, "POST", "/api/tasks",
                                {"workspace_id": ws_id, "prompt": "x", "not_before": "whenever"})
        check("a time that means nothing is refused", status == 400, (status, nonsense))
        # The scheduled task must not start, and must not hold the queue.
        time.sleep(5)
        _, listing = call(task_port, "GET", "/api/tasks?workspace_id=" + ws_id)
        still = [t for t in (listing.get("tasks") or []) if t.get("id") == later.get("id")]
        check("a task waits for its time",
              still and still[0].get("status") == "queued"
              and not still[0].get("started_at"), still)
        status, started = call(task_port, "POST", "/api/tasks/%s/start?workspace_id=%s"
                               % (later.get("id"), ws_id))
        check("a scheduled task can still be started by hand",
              status == 200, (status, started))

        # ---- the unified envelope ---------------------------------------------
        status, envelope = call(task_port, "GET", "/api/tasks", raw=True)
        check("every answer has the same shape",
              status == 200 and envelope.get("code") == 0
              and envelope.get("message") == "ok" and "data" in envelope
              and str(envelope.get("request_id") or "").startswith("req_"), envelope)
        status, missing = call(task_port, "GET", "/api/tasks/t_nope", raw=True)
        check("an error has the same shape",
              status == 404 and missing.get("code") == 40401
              and missing.get("message") and "data" in missing
              and str(missing.get("request_id") or "").startswith("req_"), missing)
        check("the error says what went wrong",
              "no task" in (missing.get("message") or ""), missing.get("message"))
        check("the error keeps the machine-readable code",
              (missing.get("data") or {}).get("error", {}).get("code") == "not_found", missing)

        # ---- the event log ----------------------------------------------------
        _, events = call(task_port, "GET", "/api/tasks/%s/events?workspace_id=%s"
                         % (first.get("id"), ws_id))
        check("the task has an event log", len(events.get("events") or []) >= 1, events)
        numbers = [e.get("event_id") for e in (events.get("events") or [])]
        check("the events are numbered", numbers == sorted(numbers) and numbers[0] == 1, numbers)
        if numbers:
            _, after = call(task_port, "GET", "/api/tasks/%s/events?since_event_id=%d&workspace_id=%s"
                            % (first.get("id"), numbers[0], ws_id))
            check("since_event_id skips what the client already has",
                  all(e.get("event_id") > numbers[0] for e in (after.get("events") or [])),
                  after)
        _, full = call(task_port, "GET", "/api/tasks/%s?include=full&workspace_id=%s"
                       % (first.get("id"), ws_id))
        check("include=full brings the whole state",
              isinstance(full.get("events"), list) and "history" in full, sorted(full.keys()))

        # ---- the audit log ----------------------------------------------------
        _, audit = call(task_port, "GET", "/api/audit?task_id=" + first.get("id"))
        check("the audit log records what happened",
              any(e.get("action") == "submit" for e in (audit.get("entries") or [])), audit)

        # ---- the metrics ------------------------------------------------------
        _, health = call(task_port, "GET", "/api/health")
        metrics = health.get("metrics") or {}
        check("the health endpoint has the metrics",
              "queue_length" in metrics and "failure_rate" in metrics, metrics)

        # ---- the result export -------------------------------------------------
        status, document = call(task_port, "GET", "/api/tasks/%s/export?format=json&workspace_id=%s"
                                % (first.get("id"), ws_id))
        check("the export is a document",
              status == 200 and (document.get("manifest") or {}).get("task_id") == first.get("id"),
              (status, str(document)[:120]))
        check("the export carries the answer and the events",
              "final_answer" in document and isinstance(document.get("events"), list), sorted(document.keys()))
        conn = http.client.HTTPConnection("127.0.0.1", task_port, timeout=60)
        conn.request("GET", "/api/tasks/%s/export?format=zip&workspace_id=%s"
                     % (first.get("id"), ws_id))
        resp = conn.getresponse()
        blob = resp.read()
        check("the export is a zip", resp.status == 200 and blob[:2] == b"PK",
              (resp.status, blob[:8]))
        import io
        import zipfile
        names = []
        if blob[:2] == b"PK":
            with zipfile.ZipFile(io.BytesIO(blob)) as z:
                names = z.namelist()
        check("the zip has the manifest, the answer, the conversation and the events",
              all(n in names for n in ("manifest.json", "final_answer.md",
                                       "conversation.jsonl", "events.jsonl")), names)

        # ---- retry ------------------------------------------------------------
        status, failed = call(task_port, "POST", "/api/tasks",
                              {"workspace_id": ws_id, "prompt": "FAILME please"})
        failed = wait_task_status(task_port, ws_id, failed.get("id"), ("done", "failed"),
                                  timeout=90)
        check("a task that cannot be done is failed", failed.get("status") == "failed",
              failed.get("status"))
        status, retried = call(task_port, "POST", "/api/tasks/%s/retry?workspace_id=%s"
                               % (failed.get("id"), ws_id))
        check("a failed task can be retried",
              status == 200 and retried.get("status") == "queued"
              and retried.get("attempt") == 2, (status, retried))
        status, refused = call(task_port, "POST", "/api/tasks/%s/retry?workspace_id=%s"
                               % (first.get("id"), ws_id))
        check("a task that is not failed cannot be retried", status == 409, (status, refused))

        # ---- the list: pagination and the filters ------------------------------
        status, page = call(task_port, "GET", "/api/tasks?workspace_id=%s&page_size=2" % ws_id)
        check("the list can be paginated",
              status == 200 and len(page.get("tasks") or []) <= 2 and page.get("total", 0) >= 2,
              (len(page.get("tasks") or []), page.get("total")))
        status, tagged = call(task_port, "POST", "/api/tasks",
                              {"workspace_id": ws_id, "prompt": "tagged",
                               "meta": {"tags": ["alpha"], "business_id": "B-7"}})
        status, filtered = call(task_port, "GET", "/api/tasks?workspace_id=%s&tag=alpha" % ws_id)
        check("the list can be filtered by tag",
              status == 200 and any(t.get("id") == tagged.get("id")
                                    for t in (filtered.get("tasks") or [])), filtered)
        status, filtered = call(task_port, "GET", "/api/tasks?workspace_id=%s&business_id=B-7" % ws_id)
        check("the list can be filtered by business id",
              status == 200 and any(t.get("id") == tagged.get("id")
                                    for t in (filtered.get("tasks") or [])), filtered)
        status, filtered = call(task_port, "GET", "/api/tasks?workspace_id=%s&created_from=2999" % ws_id)
        check("the list can be filtered by time",
              status == 200 and not (filtered.get("tasks") or []), filtered)

        # ---- a task that was running when the server started ---------------
        # The worker thread died with the process, so the record is not running
        # any more: left as "running" it would sit in the queue for ever, since
        # the scheduler only starts queued tasks.
        old = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 600))
        stale = {
            "id": "t_stale01", "workspace_id": ws_id, "prompt": "x", "title": "僵尸任务",
            "provider": "minimax", "model": "", "session_id": "", "status": "running",
            "progress": 10, "note": "", "steps": [], "needs_confirmation": False,
            "question": "", "error": "", "result": "", "meta": {},
            "created_at": old, "updated_at": old, "started_at": old, "finished_at": "",
        }
        # One ledger, one directory: the record sits directly in the data dir.
        os.makedirs(data_dir, exist_ok=True)
        with open(os.path.join(data_dir, "t_stale01.json"), "w", encoding="utf-8") as f:
            json.dump(stale, f, ensure_ascii=False)
        task_server.terminate()
        try:
            task_server.wait(timeout=10)
        except Exception:
            task_server.kill()
        task_server = subprocess.Popen(task_server_cmd,
                                       stdout=open(os.path.join(tmp, "server2.log"), "w"),
                                       stderr=subprocess.STDOUT, universal_newlines=True)
        wait_health(task_port)
        status, body = call(task_port, "GET", "/api/tasks/t_stale01?workspace_id=" + ws_id)
        check("a task that was running when the server started is marked failed",
              status == 200 and body.get("status") == "failed", (status, body))
        check("the failure says what happened",
              "restart" in (body.get("error") or ""), body.get("error"))
    finally:
        for proc in (task_server, agent):
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
