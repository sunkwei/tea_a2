#!/usr/bin/env python3
"""Smoke test for the requirement's six remaining gaps (docs/pending_task.md).

What is pinned here:

  * **the versioned routes**: /v1/agent-tasks... works, and the old
    /api/tasks... still works (the version segment was added, nothing was
    removed);
  * **the checkpoint**: a running task carries one (turn, session, trajectory),
    so a restarted queue can pick the work up from there;
  * **the export as a job**: POST .../export answers with a job id, the job
    finishes, and its bytes can be fetched - with a deadline;
  * **the artifacts**: every file the work produced has an id of its own and
    can be downloaded by that id (the key guards it, like the tasks);
  * **the interaction**: a question has an id and a deadline of its own, the
    answer must name it, and a question nobody answers in time fails;
  * **the request id / trace id / rate limit / retention**: the caller's id is
    echoed, the task has a trace id, a busy caller gets 429 with Retry-After,
    and old records are cleaned up;
  * **the tenant and the identity**: a key belongs to a tenant and says who is
    asking; the task records both, and a caller of another tenant is 403.

Usage: python3 tests/smoke_queue_spec.py [path-to-tea_agent-binary]
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


class Provider(BaseHTTPRequestHandler):
    """A mock model that asks for a file, or a question, or nothing."""

    def log_message(self, *args):
        pass

    def _json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/v1/token_plan/remains") or \
                self.path.startswith("/v1/token_plan"):
            row = {
                "model_name": "general",
                "current_interval_remaining_percent": 100.0,
                "current_weekly_remaining_percent": 100.0,
                "start_time": 0, "end_time": 0, "remains_time": 3600000,
                "weekly_start_time": 0, "weekly_end_time": 0,
                "weekly_remains_time": 86400000,
                "current_interval_status": 1, "current_weekly_status": 1,
            }
            self._json(200, {"base_resp": {"status_code": 0, "status_msg": "success"},
                             "model_remains": [row]})
            return
        self._json(404, {"error": "not found"})

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

    def do_POST(self):  # noqa: N802
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
            if "MAKEFILE" in text:
                self._tool_call("toolkit_file", {
                    "op": "write", "path": "report.txt",
                    "content": "hello from the task",
                }, "call_1")
                return
            if "ASKME" in text:
                self._tool_call("toolkit_task", {
                    "op": "update", "progress": 50, "note": "waiting",
                    "needs_confirmation": True, "question": "may I go on?",
                }, "call_1")
                return
            self._text("the answer")
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            return
        self._text("the answer")
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


def call(port, method, path, body=None, headers=None, timeout=30, raw=False):
    head = dict(headers or {})
    payload = None
    if body is not None:
        payload = json.dumps(body).encode()
        head["Content-Type"] = "application/json"
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    conn.request(method, path, body=payload, headers=head)
    resp = conn.getresponse()
    data = resp.read()
    if raw:
        return resp.status, dict(resp.getheaders()), data
    try:
        parsed = json.loads(data or b"null")
    except ValueError:
        return resp.status, dict(resp.getheaders()), data.decode("utf-8", "replace")
    return resp.status, dict(resp.getheaders()), parsed


def data_of(parsed):
    """The body of an envelope (the tests want the data)."""
    if isinstance(parsed, dict) and "code" in parsed and "data" in parsed:
        return parsed.get("data") or {}
    return parsed


def wait_task(port, ws_id, task_id, wanted, timeout=60.0):
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        _, _, parsed = call(port, "GET", "/v1/agent-tasks/%s?workspace_id=%s" % (task_id, ws_id))
        last = data_of(parsed)
        if (last or {}).get("status") in wanted:
            return last
        time.sleep(0.3)
    return last


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

    tmp = tempfile.mkdtemp(prefix="tea_spec_")
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
        supports_tools: true
""" % (provider_port, provider_port))

    agent_port = free_port()
    agent = subprocess.Popen([
        binary, "--tea-home", home, "--host", "127.0.0.1", "--port", str(agent_port),
        "--no-console", "--log-file", os.path.join(home, "logs", "tea_agent.log"),
        "--workspace", ws_dir,
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def start_queue(port, data, extra=()):
        return subprocess.Popen([
            sys.executable, SERVER, "--agent", "http://127.0.0.1:%d" % agent_port,
            "--host", "127.0.0.1", "--port", str(port), "--data-dir", data,
            "--provider", "minimax", "--interval", "1", "--max-per-round", "4",
            "--workspace-path", os.path.join(tmp, "ws-%d" % port),
        ] + list(extra), stdout=open(os.path.join(tmp, "queue-%d.log" % port), "a"),
            stderr=subprocess.STDOUT, universal_newlines=True)

    queue_port = free_port()
    queue = start_queue(queue_port, os.path.join(tmp, "tasks"))
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
        _, _, listing = call(agent_port, "GET", "/api/workspaces")
        ws_id = listing["workspaces"][0]["id"]

        # ---- 1) the versioned routes --------------------------------------
        status, headers, parsed = call(queue_port, "POST", "/v1/agent-tasks",
                                       {"workspace_id": ws_id, "prompt": "hello"})
        check("POST /v1/agent-tasks answers", status == 200, (status, parsed))
        check("the answer is the envelope",
              isinstance(parsed, dict) and parsed.get("code") == 0
              and "request_id" in parsed, parsed)
        task_id = data_of(parsed).get("id")
        check("the task was created", bool(task_id), parsed)
        status, _, parsed = call(queue_port, "GET", "/v1/agent-tasks/%s?workspace_id=%s"
                                 % (task_id, ws_id))
        check("GET /v1/agent-tasks/{id} answers", status == 200, (status, parsed))
        status, _, parsed = call(queue_port, "GET", "/api/tasks/%s?workspace_id=%s"
                                 % (task_id, ws_id))
        check("the old path still works", status == 200, (status, parsed))
        status, _, parsed = call(queue_port, "GET", "/v1/queues/%s/status" % ws_id)
        check("GET /v1/queues/{id}/status answers", status == 200, (status, parsed))
        status, _, parsed = call(queue_port, "GET", "/v1/agent-tasks")
        check("GET /v1/agent-tasks lists", status == 200, (status, parsed))

        # ---- the documentation endpoint -----------------------------------
        status, headers, parsed = call(queue_port, "GET", "/api/doc?format=md", raw=True)
        check("GET /api/doc?format=md answers", status == 200, (status, parsed))
        check("it is served as markdown",
              "text/markdown" in headers.get("Content-Type", ""),
              headers.get("Content-Type"))
        doc_path = os.path.join(REPO_ROOT, "docs", "tasks.md")
        with open(doc_path, "rb") as f:
            on_disk = f.read()
        check("it is the very same file the repository has",
              parsed == on_disk, "the served text differs from docs/tasks.md")
        status, _, parsed = call(queue_port, "GET", "/api/doc", raw=True)
        check("GET /api/doc answers with a page",
              status == 200 and b"<html" in parsed[:200].lower(),
              (status, parsed[:120]))
        status, _, parsed = call(queue_port, "GET", "/api/doc?format=json")
        doc = data_of(parsed)
        check("?format=json carries the version", bool(doc.get("version")), doc.keys())
        check("?format=json carries the markdown",
              (doc.get("markdown") or "") == on_disk.decode("utf-8"),
              len(doc.get("markdown") or ""))
        check("?format=json carries the rendered page",
              "<h" in (doc.get("html") or ""), (doc.get("html") or "")[:80])
        status, _, parsed = call(queue_port, "GET", "/api/doc?format=xml")
        check("an unknown format is refused", status == 400, (status, parsed))
        status, _, parsed = call(queue_port, "GET", "/api/doc/tea_a2_manual.md?format=md",
                                 raw=True)
        check("another document can be asked for by name", status == 200, status)
        with open(os.path.join(REPO_ROOT, "docs", "tea_a2_manual.md"), "rb") as f:
            manual = f.read()
        check("it is that file, byte for byte", parsed == manual,
              "%d vs %d" % (len(parsed or b""), len(manual)))
        status, _, parsed = call(queue_port, "GET", "/api/doc/../server.py", raw=True)
        check("a path that tries to leave the directory is refused",
              status == 404, status)
        # the rewriting happens in the rendered page (the markdown stays as written)
        status, _, parsed = call(queue_port, "GET", "/api/doc?format=json")
        doc = data_of(parsed)
        check("the links in the page point at the served documents",
              '/api/doc/tea_a2_manual.md' in (doc.get("html") or ""),
              "the relative link was not rewritten")

        # ---- the queue says which version it is -------------------------
        status, _, parsed = call(queue_port, "GET", "/api/health")
        health = data_of(parsed)
        check("the queue says which version it is",
              status == 200 and bool(health.get("version")), health)
        check("it says which agent is behind it",
              bool(health.get("agent_version")), health)
        # The queue's own version must not drift from the binary's: a client
        # asks the queue whether the queue's API knows something.
        with open(os.path.join(REPO_ROOT, "src", "version.cpp"), encoding="utf-8") as f:
            source = f.read()
        binary_version = ""
        for line in source.split("\n"):
            if "kVersionString" in line and '"' in line:
                binary_version = line.split('"')[1]
                break
        check("the queue's version is the binary's version",
              health.get("version") == binary_version,
              "the queue says %r, the binary says %r"
              % (health.get("version"), binary_version))

        # ---- 2) the request id and the trace id ---------------------------
        status, headers, parsed = call(queue_port, "GET", "/v1/agent-tasks",
                                       headers={"X-Request-Id": "trace-42"})
        check("the caller's request id is echoed",
              headers.get("X-Request-Id") == "trace-42", headers)
        check("the envelope carries it too",
              isinstance(parsed, dict) and parsed.get("request_id") == "trace-42", parsed)
        done = wait_task(queue_port, ws_id, task_id, ("done", "failed"))
        check("the task has a trace id", bool(done.get("trace_id")), done)

        # ---- 3) the checkpoint --------------------------------------------
        checkpoint = done.get("checkpoint") or {}
        check("the checkpoint names the turn", bool(checkpoint.get("turn_id")), checkpoint)
        check("the checkpoint names the session",
              bool(checkpoint.get("session_id")), checkpoint)
        check("the checkpoint names the trajectory",
              bool(checkpoint.get("trajectory_id")), checkpoint)

        # ---- 4) the result and the artifacts ------------------------------
        status, _, parsed = call(queue_port, "GET", "/v1/agent-tasks/%s/result?workspace_id=%s"
                                 % (task_id, ws_id))
        result = data_of(parsed)
        check("GET .../result answers", status == 200 and "result" in result, (status, result))
        check("the result carries the trace id", bool(result.get("trace_id")), result)

        _, _, parsed = call(queue_port, "POST", "/v1/agent-tasks",
                            {"workspace_id": ws_id, "prompt": "MAKEFILE please"})
        file_task = data_of(parsed).get("id")
        done = wait_task(queue_port, ws_id, file_task, ("done", "failed"))
        check("the file task finished", done.get("status") == "done", done)
        status, _, parsed = call(queue_port, "GET", "/v1/agent-tasks/%s/result?workspace_id=%s"
                                 % (file_task, ws_id))
        result = data_of(parsed)
        artifacts = result.get("artifacts") or []
        check("the artifacts are listed", bool(artifacts), result)
        artifact = artifacts[0] if artifacts else {}
        check("every artifact has an id", bool(artifact.get("artifact_id")), artifact)
        status, _, data = call(queue_port, "GET",
                               "/v1/agent-tasks/%s/artifacts/%s?workspace_id=%s"
                               % (file_task, artifact.get("artifact_id") or "", ws_id),
                               raw=True)
        check("the artifact can be downloaded by its id", status == 200, (status, data[:80]))
        check("the bytes are the file", b"hello from the task" in (data or b""),
              (data or b"")[:80])

        # ---- 5) the export as a job ---------------------------------------
        status, _, parsed = call(queue_port, "POST", "/v1/agent-tasks/%s/export"
                                 % file_task, {"format": "zip", "include_artifacts": True})
        check("POST .../export answers 202", status == 202, (status, parsed))
        job = data_of(parsed)
        job_id = job.get("export_id") or ""
        check("the answer carries the job id", bool(job_id), job)
        deadline = time.time() + 30
        state = {}
        while time.time() < deadline:
            _, _, parsed = call(queue_port, "GET", "/v1/exports/%s" % job_id)
            state = data_of(parsed)
            if state.get("status") in ("done", "failed"):
                break
            time.sleep(0.2)
        check("the export finished", state.get("status") == "done", state)
        check("the job says when it expires", bool(state.get("expires_at")), state)
        status, _, data = call(queue_port, "GET", "/v1/exports/%s/download" % job_id, raw=True)
        check("the export can be downloaded", status == 200, (status, data[:40]))
        check("it is a zip", (data or b"")[:2] == b"PK", (data or b"")[:8])

        # ---- 6) the interaction -------------------------------------------
        _, _, parsed = call(queue_port, "POST", "/v1/agent-tasks",
                            {"workspace_id": ws_id, "prompt": "ASKME please"})
        ask_id = data_of(parsed).get("id")
        waiting = wait_task(queue_port, ws_id, ask_id, ("waiting_confirmation", "done", "failed"))
        check("the task waits for an answer",
              waiting.get("status") == "waiting_confirmation", waiting)
        interaction = waiting.get("interaction") or {}
        check("the question has an id", bool(interaction.get("interaction_id")), interaction)
        check("the question has a deadline", "expires_at" in interaction, interaction)
        status, _, parsed = call(queue_port, "POST", "/v1/agent-tasks/%s/interactions" % ask_id,
                                 {"interaction_id": "in_wrong", "response": "yes"})
        check("answering another question is refused", status == 409, (status, parsed))
        status, _, parsed = call(queue_port, "POST", "/v1/agent-tasks/%s/interactions" % ask_id,
                                 {"interaction_id": interaction.get("interaction_id"),
                                  "response": "yes please"})
        check("the right question can be answered", status == 200, (status, parsed))

        # ---- 7) the tenant and the identity -------------------------------
        status, _, parsed = call(queue_port, "POST", "/v1/projects",
                                 {"workspace_id": ws_id, "tenant_id": "acme",
                                  "identity": "zhang"})
        check("a key can be minted for a tenant", status == 200, (status, parsed))
        key = data_of(parsed).get("key") or ""
        check("the key is there", bool(key), parsed)
        status, _, parsed = call(queue_port, "POST", "/v1/agent-tasks",
                                 {"workspace_id": ws_id, "prompt": "for the tenant"},
                                 headers={"Authorization": "Bearer " + key})
        tenant_task = data_of(parsed)
        check("the task records the tenant",
              tenant_task.get("tenant_id") == "acme", tenant_task)
        check("the task records who asked",
              tenant_task.get("identity") == "zhang", tenant_task)
        status, _, parsed = call(queue_port, "GET", "/v1/agent-tasks",
                                 headers={"Authorization": "Bearer " + key,
                                          "X-Tenant-Id": "another"})
        check("a caller of another tenant is refused", status == 403, (status, parsed))

        # ---- 8) the rate limit --------------------------------------------
        limited_port = free_port()
        limited = start_queue(limited_port, os.path.join(tmp, "tasks-limited"),
                               extra=["--rate-limit-per-minute", "3"])
        if not wait_health(limited_port):
            print("the limited queue did not become healthy")
            return 1
        codes = []
        for _ in range(8):
            status, _, _ = call(limited_port, "GET", "/v1/agent-tasks")
            codes.append(status)
        check("a busy caller is limited", 429 in codes, codes)
        status, headers, parsed = call(limited_port, "GET", "/v1/agent-tasks")
        check("the limit says when to come back",
              status == 429 and headers.get("Retry-After"), (status, headers))
        limited.terminate()
        try:
            limited.wait(timeout=10)
        except Exception:
            limited.kill()

        # ---- 9) the interaction timeout -----------------------------------
        short_port = free_port()
        short = start_queue(short_port, os.path.join(tmp, "tasks-short"),
                            extra=["--interaction-timeout", "2"])
        if not wait_health(short_port):
            print("the short queue did not become healthy")
            return 1
        _, _, parsed = call(short_port, "POST", "/v1/agent-tasks",
                            {"workspace_id": ws_id, "prompt": "ASKME please"})
        timed_id = data_of(parsed).get("id")
        deadline = time.time() + 30
        record = {}
        while time.time() < deadline:
            _, _, parsed = call(short_port, "GET", "/v1/agent-tasks/%s?workspace_id=%s"
                               % (timed_id, ws_id))
            record = data_of(parsed)
            if record.get("status") in ("failed", "done"):
                break
            time.sleep(0.3)
        check("a question nobody answers in time fails",
              record.get("status") == "failed", record)
        check("the reason says it was the clock",
              "time" in str(record.get("error") or "").lower(), record.get("error"))
        short.terminate()
        try:
            short.wait(timeout=10)
        except Exception:
            short.kill()

        # ---- 10) the resume is opt-in --------------------------------------
        # A turn that was cut off half way may already have done half of its
        # work: running it again without being told is worse than stopping.
        # So the default is "it died, here is why" - and the operator has to
        # ask for the resume.
        old = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 600))
        stale = {
            "id": "t_stale01", "workspace_id": ws_id, "prompt": "x", "title": "断了的任务",
            "provider": "minimax", "model": "", "status": "running",
            "session_id": "s_stale01", "turn_id": "t_stale01",
            "checkpoint": {"turn_id": "t_stale01", "session_id": "s_stale01"},
            "progress": 10, "note": "", "steps": [], "needs_confirmation": False,
            "question": "", "error": "", "result": "", "meta": {},
            "created_at": old, "updated_at": old, "started_at": old, "finished_at": "",
        }
        idle_dir = os.path.join(tmp, "tasks-idle")
        os.makedirs(idle_dir, exist_ok=True)
        with open(os.path.join(idle_dir, "t_stale01.json"), "w", encoding="utf-8") as f:
            json.dump(stale, f, ensure_ascii=False)
        idle_port = free_port()
        idle = start_queue(idle_port, idle_dir)
        if not wait_health(idle_port):
            print("the queue did not become healthy")
            return 1
        _, _, parsed = call(idle_port, "GET", "/v1/agent-tasks/t_stale01?workspace_id=%s"
                            % ws_id)
        record = data_of(parsed)
        check("by default the interrupted task is marked failed",
              record.get("status") == "failed", record)
        check("and it says why", "restart" in str(record.get("error") or ""),
              record.get("error"))
        idle.terminate()
        try:
            idle.wait(timeout=10)
        except Exception:
            idle.kill()

        # With the flag, the same task goes back into the queue: the session
        # is still there, so the work can go on from where it stopped.
        resume_dir = os.path.join(tmp, "tasks-resume")
        os.makedirs(resume_dir, exist_ok=True)
        with open(os.path.join(resume_dir, "t_stale02.json"), "w", encoding="utf-8") as f:
            json.dump(dict(stale, id="t_stale02", session_id="s_stale02",
                           turn_id="t_stale02",
                           checkpoint={"turn_id": "t_stale02", "session_id": "s_stale02"}),
                      f, ensure_ascii=False)
        resume_port = free_port()
        resumed = start_queue(resume_port, resume_dir,
                               extra=["--resume-on-restart"])
        if not wait_health(resume_port):
            print("the queue with the resume did not become healthy")
            return 1
        _, _, parsed = call(resume_port, "GET", "/v1/agent-tasks/t_stale02?workspace_id=%s"
                            % ws_id)
        record = data_of(parsed)
        check("with --resume-on-restart the task does not die",
              record.get("status") != "failed" and not record.get("error"), record)
        check("and it goes on (queued with the resume note, or already running)",
              record.get("status") in ("queued", "running", "done"), record)
        resumed.terminate()
        try:
            resumed.wait(timeout=10)
        except Exception:
            resumed.kill()

        # ---- 11) the retention --------------------------------------------
        # The records are kept for a second and a half: an old one is cleaned
        # up, a new one stays.
        keep_port = free_port()
        keep = start_queue(keep_port, os.path.join(tmp, "tasks-keep"),
                           extra=["--retention-days", "0.00005", "--cleanup-interval", "1"])
        if not wait_health(keep_port):
            print("the queue with the retention did not become healthy")
            return 1
        _, _, parsed = call(keep_port, "POST", "/v1/agent-tasks",
                            {"workspace_id": ws_id, "prompt": "old one"})
        old_id = data_of(parsed).get("id")
        time.sleep(6.0)
        status, _, parsed = call(keep_port, "GET", "/v1/agent-tasks/%s?workspace_id=%s"
                                 % (old_id, ws_id))
        check("an old record is cleaned up", status == 404, (status, parsed))
        _, _, parsed = call(keep_port, "POST", "/v1/agent-tasks",
                            {"workspace_id": ws_id, "prompt": "new one"})
        new_id = data_of(parsed).get("id")
        time.sleep(2.0)
        status, _, parsed = call(keep_port, "GET", "/v1/agent-tasks/%s?workspace_id=%s"
                                 % (new_id, ws_id))
        check("a fresh record stays", status == 200, (status, parsed))
        keep.terminate()
        try:
            keep.wait(timeout=10)
        except Exception:
            keep.kill()
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
