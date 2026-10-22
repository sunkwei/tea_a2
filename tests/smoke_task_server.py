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
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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
        # Not JSON (the page): hand back the text.
        return resp.status, raw.decode("utf-8", "replace")


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
    task_server_cmd = [
        sys.executable, SERVER, "--agent", "http://127.0.0.1:%d" % agent_port,
        "--host", "127.0.0.1", "--port", str(task_port), "--data-dir", data_dir,
        "--provider", "minimax", "--interval", "2", "--max-per-round", "4",
    ]
    task_server = subprocess.Popen(task_server_cmd,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

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
        done2 = wait_task_status(task_port, ws_id, task2.get("id"), ("done", "failed"), timeout=90)
        check("the task finishes after the answer",
              done2 and done2.get("status") == "done", done2)

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
              status == 200 and plain.get("workspace_id") == ws_id, (status, plain))
        status, gone = call(task_port, "DELETE", "/api/tasks/" + plain.get("id", ""))
        check("a task can be deleted without naming a workspace",
              status == 200 and gone.get("ok") is True, (status, gone))
        status, _ = call(task_port, "GET", "/api/tasks/" + plain.get("id", ""))
        check("the deleted task is gone", status == 404, status)
        status, body = call(task_port, "POST", "/api/tasks",
                            {"prompt": "x", "workspace_id": "ws_nope"})
        check("a task for a workspace nobody has is refused",
              status == 400, (status, body))

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
        os.makedirs(os.path.join(data_dir, ws_id), exist_ok=True)
        with open(os.path.join(data_dir, ws_id, "t_stale01.json"), "w", encoding="utf-8") as f:
            json.dump(stale, f, ensure_ascii=False)
        task_server.terminate()
        try:
            task_server.wait(timeout=10)
        except Exception:
            task_server.kill()
        task_server = subprocess.Popen(task_server_cmd,
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
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
