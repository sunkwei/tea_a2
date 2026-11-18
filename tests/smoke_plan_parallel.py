#!/usr/bin/env python3
"""Smoke test for the queue's parallel read-only steps.

The queue is serial *per project*: a project (a workspace) works on one task
at a time. That rule has one exception now, and it is switched on
deliberately (--parallel-ro-steps):

  * two **read-only** tasks may run side by side - they change nothing;
  * a **read-write** task still runs alone - the file it is rewriting is
    not a file another task may read (the write is not atomic).

What this pins:

  * with the switch off, nothing changes: one task per project, as always;
  * with the switch on, two read-only steps of one plan really overlap;
  * a read-write step does not overlap with them;
  * a read-only step runs under the read-only role (the queue installs it
    into the workspace and sends it with the turn);
  * a plan's step may not lie: "ro" with write paths, or two "rw" steps
    writing the same place, are refused.

Usage: python3 tests/smoke_plan_parallel.py [path-to-task_server.py]
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

# How long a step takes. Long enough that two of them overlap.
STEP_DELAY = 2.0


class MockAgent(BaseHTTPRequestHandler):
    """An agent that takes a moment per turn and remembers what it was asked."""

    records = []       # one entry per turn
    roles = []         # the roles the queue wrote into the workspace
    lock = threading.Lock()

    def log_message(self, *args):
        pass

    def _json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self):
        if self.path.startswith("/api/workspaces/"):
            # The roles of the workspace: none yet, so the queue has to
            # install the read-only one itself.
            return self._json({"roles": [], "current": "", "current_id": ""})
        if self.path.startswith("/api/health"):
            return self._json({"ok": True})
        return self._not_found()

    def _not_found(self):
        # The shape the agent uses for an error: {"error": {"code", "message"}}.
        return self._json({"error": {"code": "not_found",
                                      "message": "unknown path " + self.path}}, 404)

    def do_PUT(self):
        if "/roles/" in self.path:
            body = self._read_json()
            with MockAgent.lock:
                MockAgent.roles.append({"path": self.path,
                                        "content": body.get("content") or ""})
            return self._json({"ok": True})
        return self._not_found()

    def do_POST(self):
        if self.path == "/api/llm/chat":
            raw = self._read_json()
            prompt = raw.get("prompt") or ""
            content = "SUMMARY: the steps are done"
            if "各步骤的结果" not in prompt:
                content = '{"decision": "accept", "reason": "活儿"}'
            return self._json({"ok": True, "provider": "mock", "model": "mock",
                               "content": content, "reasoning": "",
                               "finish_reason": "stop"})
        if self.path == "/api/chat":
            raw = self._read_json()
            message = raw.get("message") or ""
            started = time.time()
            with MockAgent.lock:
                MockAgent.records.append({
                    "message": message,
                    "role": raw.get("role") or "",
                    "started": started,
                    "finished": None,
                })
            record = MockAgent.records[-1]
            time.sleep(STEP_DELAY)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for frame in ({"type": "start", "session_id": "sess_mock"},
                          {"type": "done", "content": "「%s」做完了"
                           % message.splitlines()[0][:20]}):
                try:
                    self.wfile.write(("data: " + json.dumps(frame) + "\n\n").encode())
                    self.wfile.flush()
                except OSError:
                    return
            with MockAgent.lock:
                record["finished"] = time.time()
            return
        return self._not_found()


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


def call(port, method, path, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    headers = {"Content-Type": "application/json"} if body is not None else {}
    conn.request(method, path, body=json.dumps(body) if body is not None else None,
                 headers=headers)
    resp = conn.getresponse()
    raw = resp.read()
    try:
        return resp.status, json.loads(raw or b"null")
    except ValueError:
        return resp.status, {"raw": raw.decode("utf-8", "replace")}


def unwrap(body):
    if isinstance(body, dict) and "data" in body and "code" in body:
        if body.get("code"):
            return {"error": body.get("message") or "the queue refused"}
        return body.get("data") or {}
    return body


def turns():
    with MockAgent.lock:
        return list(MockAgent.records)


def overlaps(first, second):
    a, b = first["started"], second["started"]
    a_end = first["finished"] or time.time()
    b_end = second["finished"] or time.time()
    return a < b_end and b < a_end


def wait_for(predicate, timeout=60.0, step=0.3):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(step)
    return None


def start_queue(tmp, agent_port, parallel):
    queue_port = free_port()
    argv = [
        sys.executable, SERVER,
        "--agent", "http://127.0.0.1:%d" % agent_port,
        "--host", "127.0.0.1",
        "--port", str(queue_port),
        "--provider", "mock",
        "--data-dir", os.path.join(tmp, "tasks_%s" % ("p" if parallel else "s")),
        "--workspace", "ws_mock",
        "--interval", "1",
        "--allow-without-quota",
    ]
    if parallel:
        argv += ["--parallel-ro-steps", "1"]
    proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return proc, queue_port


def stop(proc):
    try:
        proc.terminate()
        proc.wait(timeout=10)
    except Exception:
        proc.kill()


def main():
    if not os.path.exists(SERVER):
        print("queue not found:", SERVER)
        return 1

    MockAgent.records = []
    MockAgent.roles = []
    agent = ThreadingHTTPServer(("127.0.0.1", 0), MockAgent)
    threading.Thread(target=agent.serve_forever, daemon=True).start()
    agent_port = agent.server_address[1]

    tmp = tempfile.mkdtemp(prefix="tea_plan_par_")
    failures = []
    checks = []

    def check(name, ok, detail=""):
        checks.append(name)
        print(("PASS " if ok else "FAIL ") + name + (("  " + str(detail)) if not ok else ""))
        if not ok:
            failures.append(name)

    # ---- the plan that is handed to the queue --------------------------
    plan = {
        "title": "两个只读，一个要改",
        "goal": "把三件事一起办了",
        "aggregate_prompt": "put it together",
        "steps": [
            {"key": "s1", "title": "查", "prompt": "第一步：查", "mode": "ro"},
            {"key": "s2", "title": "再查", "prompt": "第二步：再查", "mode": "ro"},
            {"key": "s3", "title": "改", "prompt": "第三步：改", "mode": "rw",
             "write_paths": ["out/"]},
        ],
    }

    try:
        # ---- the switch off: one task per project, as always ------------
        proc, queue_port = start_queue(tmp, agent_port, parallel=False)
        try:
            if not wait_health(queue_port):
                print("the queue did not become healthy")
                return 1
            status, answer = call(queue_port, "POST", "/api/plans", plan)
            check("the plan is accepted", status == 200, answer)
            done = wait_for(lambda: len(turns()) >= 3, timeout=90.0)
            check("every step ran", bool(done), len(turns()))
            records = turns()
            if len(records) >= 3:
                check("without the switch the steps do not overlap",
                      not overlaps(records[0], records[1]),
                      [(r["started"], r["finished"]) for r in records[:2]])
        finally:
            stop(proc)

        # ---- the switch on: the read-only steps run side by side -------
        MockAgent.records = []
        MockAgent.roles = []
        proc, queue_port = start_queue(tmp, agent_port, parallel=True)
        try:
            if not wait_health(queue_port):
                print("the queue did not become healthy")
                return 1
            status, answer = call(queue_port, "POST", "/api/plans", plan)
            check("the plan is accepted (switch on)", status == 200, answer)
            done = wait_for(lambda: len(turns()) >= 3, timeout=90.0)
            check("every step ran (switch on)", bool(done), len(turns()))
            records = turns()
            if len(records) >= 3:
                ro = [r for r in records if r["message"].startswith("第一步")
                      or r["message"].startswith("第二步")]
                rw = [r for r in records if r["message"].startswith("第三步")]
                check("the two read-only steps overlapped",
                      len(ro) >= 2 and overlaps(ro[0], ro[1]),
                      [(r["message"][:6], r["started"], r["finished"]) for r in records])
                check("the read-write step did not overlap with them",
                      len(rw) >= 1 and len(ro) >= 2 and
                      not overlaps(rw[0], ro[0]) and not overlaps(rw[0], ro[1]),
                      [(r["message"][:6], r["started"], r["finished"]) for r in records])

            # ---- the read-only role -------------------------------------
            roles = [r for r in MockAgent.roles if "readonly" in (r.get("path") or "")]
            check("the queue installs the read-only role", bool(roles), MockAgent.roles)
            if roles:
                content = roles[0].get("content") or ""
                check("the role is read-only", "read_only: true" in content, content[:200])
                check("the role has no command tool",
                      "toolkit_exec" not in content, content[:200])
            ro_turns = [r for r in turns() if r["message"].startswith("第一步")
                        or r["message"].startswith("第二步")]
            check("the read-only step runs under the role",
                  bool(ro_turns) and all(r["role"] == "readonly" for r in ro_turns),
                  [(r["message"][:6], r["role"]) for r in ro_turns])
            rw_turns = [r for r in turns() if r["message"].startswith("第三步")]
            check("the read-write step runs under no role",
                  bool(rw_turns) and all(not r["role"] for r in rw_turns),
                  [(r["message"][:6], r["role"]) for r in rw_turns])

            # ---- a plan may not lie --------------------------------------
            status, answer = call(queue_port, "POST", "/api/plans", {
                "title": "矛盾", "goal": "g",
                "steps": [{"key": "s1", "title": "只读却要写", "prompt": "x",
                           "mode": "ro", "write_paths": ["src/"]}]})
            check("a read-only step with write paths is refused", status == 400,
                  (status, answer))
            status, answer = call(queue_port, "POST", "/api/plans", {
                "title": "打架", "goal": "g",
                "steps": [{"key": "s1", "title": "a", "prompt": "x", "mode": "rw",
                           "write_paths": ["out/"]},
                          {"key": "s2", "title": "b", "prompt": "y", "mode": "rw",
                           "write_paths": ["out/"]}]})
            check("two read-write steps writing the same place are refused",
                  status == 400, (status, answer))
            status, answer = call(queue_port, "POST", "/api/tasks", {
                "prompt": "x", "mode": "nonsense"})
            check("a task with a wrong mode is refused", status == 400, (status, answer))
        finally:
            stop(proc)
    finally:
        agent.shutdown()

    print()
    print("%d/%d checks passed" % (len(checks) - len(failures), len(checks)))
    if failures:
        print("FAILED:", ", ".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
