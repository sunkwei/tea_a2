#!/usr/bin/env python3
"""Smoke test for the plans: one complex job, split into steps.

What this pins:

  * a plan becomes one parent (the container) and one task per step;
  * a step that waits for another one only starts after that one is done;
  * the steps of a plan are one conversation (the router remembers them);
  * when every step is done, the plan's result is written into the parent -
    one model call, and the parent says so;
  * a step that fails does not leave the plan hanging: the parent fails with
    the reason;
  * a plan that is submitted twice with the same key is one plan;
  * the plan file can be cancelled while it is running;
  * a broken plan (a step without content, a dependency that does not exist,
    steps that wait for each other) is refused with a message, not a crash.

Usage: python3 tests/smoke_plan.py [path-to-tea_agent-binary]
"""

import http.client
import json
import os
import random
import shutil
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

import plans  # noqa: E402

SUMMARY = "汇总：三个步骤都做完了，最终交付物在这里。"


class MockAgent(BaseHTTPRequestHandler):
    """A fake agent: it answers the health check, the quota, the turns and
    the one model call the queue makes to summarize a plan."""

    records = {"llm": 0, "chat": 0, "usage": 0, "order": [], "models": [],
               "llm_models": []}
    ws_dir = ""

    def log_message(self, *args):
        pass

    def _json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802 (http.server API)
        if self.path == "/api/health":
            return self._json({"ok": True, "version": "mock"})
        if self.path == "/api/workspaces":
            return self._json({"workspaces": [{"id": "ws_mock", "path": MockAgent.ws_dir,
                                               "name": "mock"}]})
        if self.path.startswith("/api/providers/"):
            MockAgent.records["usage"] += 1
            return self._json({"window": {"remaining_percent": 95.0}})
        return self._json({"error": "unknown path " + self.path}, 404)

    def do_POST(self):  # noqa: N802 (http.server API)
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length) if length else b""
        if self.path == "/api/llm/chat":
            MockAgent.records["llm"] += 1
            try:
                body = json.loads(raw or b"{}")
            except ValueError:
                body = {}
            prompt = body.get("prompt") or ""
            MockAgent.records["llm_models"].append(body.get("model") or "")
            if "各步骤的结果" not in prompt:
                # The trigger judgement - not what this test is about.
                return self._json({"ok": True, "provider": "mock", "model": "mock",
                                   "content": '{"decision": "accept", "reason": "活儿"}',
                                   "reasoning": "", "finish_reason": "stop"})
            return self._json({"ok": True, "provider": "mock", "model": "mock",
                               "content": SUMMARY, "reasoning": "",
                               "finish_reason": "stop"})
        if self.path == "/api/chat":
            MockAgent.records["chat"] += 1
            try:
                body = json.loads(raw or b"{}")
            except ValueError:
                body = {}
            message = body.get("message") or ""
            MockAgent.records["order"].append(message)
            MockAgent.records["models"].append(body.get("model") or "")
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            frames = [{"type": "start", "session_id": "sess_mock"}]
            if "会失败" in message:
                frames.append({"type": "error", "message": "这一步炸了"})
            else:
                frames.append({"type": "done", "content": "「%s」做完了"
                               % message.splitlines()[0][:20]})
            for frame in frames:
                try:
                    self.wfile.write(("data: " + json.dumps(frame) + "\n\n").encode())
                    self.wfile.flush()
                except OSError:
                    return
            return
        if self.path == "/api/workspaces":
            return self._json({"id": "ws_mock"})
        return self._json({"error": "unknown path " + self.path}, 404)


def free_port():
    while True:
        port = random.randint(20000, 60000)
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue


def call(port, method, path, body=None, timeout=30, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    data = json.dumps(body).encode() if body is not None else None
    head = {"Content-Type": "application/json"} if data else {}
    if headers:
        head.update(headers)
    conn.request(method, path, body=data, headers=head)
    resp = conn.getresponse()
    raw = resp.read().decode("utf-8", "replace")
    conn.close()
    try:
        return resp.status, json.loads(raw)
    except ValueError:
        return resp.status, {"raw": raw}


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


def wait_task(port, task_id, timeout=60.0):
    """The task as it ends up."""
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        status, body = call(port, "GET", "/api/tasks/" + task_id)
        task = (body or {}).get("data") or {}
        last = task
        if task.get("status") in ("done", "failed", "cancelled"):
            return task
        time.sleep(0.3)
    return last


def main():
    binary = (
        sys.argv[1] if len(sys.argv) > 1 else os.path.join(REPO_ROOT, "build", "bin", "tea_agent")
    )
    if not os.path.exists(binary):
        print("binary not found:", binary)
        return 1

    failures = []

    def check(name, ok, detail=""):
        print(("PASS " if ok else "FAIL ") + name + (("  " + str(detail)) if not ok else ""))
        if not ok:
            failures.append(name)

    # ---- the plan itself (no network) ------------------------------------
    try:
        plans.normalize_steps([])
        check("a plan without steps is refused", False)
    except plans.PlanError as exc:
        check("a plan without steps is refused", "至少" in str(exc), exc)
    try:
        plans.normalize_steps([{"title": "空的"}])
        check("a step without content is refused", False)
    except plans.PlanError as exc:
        check("a step without content is refused", "prompt" in str(exc), exc)
    try:
        plans.normalize_steps([{"key": "a", "prompt": "x", "depends_on": ["b"]},
                               {"key": "b", "prompt": "y", "depends_on": ["a"]}])
        check("steps that wait for each other are refused", False)
    except plans.PlanError as exc:
        check("steps that wait for each other are refused", "等" in str(exc), exc)
    try:
        plans.normalize_steps([{"key": "a", "prompt": "x", "depends_on": ["nope"]}])
        check("a dependency that does not exist is refused", False)
    except plans.PlanError as exc:
        check("a dependency that does not exist is refused", "不存在" in str(exc), exc)
    good = plans.normalize_steps([{"key": "a", "title": "第一步", "prompt": "做 A"},
                                  {"key": "b", "prompt": "做 B", "depends_on": ["a"]}])
    check("a good plan is normalized",
          len(good) == 2 and good[0]["key"] == "a" and good[1]["depends_on"] == ["a"], good)

    tmp = tempfile.mkdtemp(prefix="tea_plan_")
    ws_dir = os.path.join(tmp, "ws")
    os.makedirs(ws_dir)
    MockAgent.ws_dir = ws_dir

    agent_port = free_port()
    queue_port = free_port()
    agent = ThreadingHTTPServer(("127.0.0.1", agent_port), MockAgent)
    threading.Thread(target=agent.serve_forever, daemon=True).start()

    queue = subprocess.Popen(
        [
            sys.executable,
            SERVER,
            "--agent",
            "http://127.0.0.1:%d" % agent_port,
            "--host",
            "127.0.0.1",
            "--port",
            str(queue_port),
            "--provider",
            "mock",
            "--data-dir",
            os.path.join(tmp, "tasks"),
            "--workspace",
            "ws_mock",
            "--interval",
            "1",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        if not wait_health(queue_port):
            print("the queue did not become healthy")
            return 1

        # ---- a plan becomes a parent and some steps ----------------------
        body = {
            "title": "把三个文件分类",
            "goal": "最后给出一份清单",
            "steps": [
                {"key": "read", "title": "读文件", "prompt": "第一步：读文件"},
                {"key": "classify", "title": "分类", "prompt": "第二步：分类",
                 "depends_on": ["read"]},
                {"key": "list", "title": "列清单", "prompt": "第三步：列清单"},
            ],
        }
        status, answer = call(queue_port, "POST", "/api/plans", body)
        check("the plan is accepted", status == 200, answer)
        data = (answer or {}).get("data") or {}
        plan = data.get("plan") or {}
        parent = data.get("parent") or {}
        steps = data.get("steps") or []
        check("the plan has an id", bool(plan.get("id")), plan)
        check("the plan has a parent task", bool(parent.get("id")), parent)
        check("the parent is a container", parent.get("kind") == "plan", parent.get("kind"))
        check("the parent knows its three steps",
              len(parent.get("children") or []) == 3, parent.get("children"))
        check("every step became a task", len(steps) == 3, steps)
        step_tasks = dict((s.get("key"), (s.get("task") or {})) for s in steps)
        check("the steps know their parent",
              all((step_tasks.get(k) or {}).get("parent_id") == parent.get("id")
                  for k in step_tasks), step_tasks)
        check("the waiting step names the task it waits for",
              (step_tasks.get("classify") or {}).get("depends_on") ==
              [(step_tasks.get("read") or {}).get("id")],
              step_tasks.get("classify"))

        # ---- the steps are one conversation ------------------------------
        thread_id = plan.get("thread_id")
        status, answer = call(queue_port, "GET", "/api/threads/" + str(thread_id))
        thread = (answer or {}).get("data") or {}
        check("the steps share one thread",
              len(thread.get("task_ids") or []) == 4, thread.get("task_ids"))

        # ---- the steps run in the right order ----------------------------
        parent_task = wait_task(queue_port, parent.get("id"), timeout=90)
        check("the plan is done", parent_task.get("status") == "done", parent_task.get("status"))
        check("the parent holds the summary",
              (parent_task.get("result") or "") == SUMMARY, parent_task.get("result"))
        order = MockAgent.records["order"]
        try:
            at_read = order.index("第一步：读文件")
            at_classify = order.index("第二步：分类")
        except ValueError:
            at_read = at_classify = -1
        check("the waiting step ran after the step it waits for",
              at_read >= 0 and at_classify > at_read, order)
        check("the summary was made with one model call",
              MockAgent.records["llm"] == 1, MockAgent.records["llm"])

        # ---- the model of a task is really used --------------------------
        status, answer = call(queue_port, "POST", "/api/tasks",
                              {"prompt": "用指定的模型跑", "model": "mock-model-x"})
        asked = ((answer or {}).get("data") or {})
        asked_id = asked.get("id")
        wait_task(queue_port, asked_id, timeout=60)
        check("the model of a task reaches the agent",
              "mock-model-x" in MockAgent.records["models"],
              MockAgent.records["models"][-3:])

        # ---- the plan record ---------------------------------------------
        status, answer = call(queue_port, "GET", "/api/plans/" + str(plan.get("id")))
        view = (answer or {}).get("data") or {}
        check("the plan is in the plan list", view.get("plan", {}).get("status") == "done",
              view.get("plan", {}).get("status"))
        check("the plan view knows the tasks", len(view.get("steps") or []) == 3, view)
        status, answer = call(queue_port, "GET", "/api/plans")
        check("the plans are listed", (answer or {}).get("data", {}).get("count", 0) >= 1, answer)

        # ---- the same key is the same plan -------------------------------
        keyed = {"title": "带键的计划", "goal": "只排一次",
                 "steps": [{"key": "one", "title": "第一步", "prompt": "带键的第一步"}]}
        status, answer = call(queue_port, "POST", "/api/plans", keyed,
                              headers={"Idempotency-Key": "plan-1"})
        first = (answer or {}).get("data") or {}
        status, answer = call(queue_port, "POST", "/api/plans", keyed,
                              headers={"Idempotency-Key": "plan-1"})
        again = (answer or {}).get("data") or {}
        check("the same key does not queue the plan twice",
              again.get("duplicate") is True, again.get("duplicate"))
        check("the same key returns the same plan",
              (again.get("plan") or {}).get("id") == (first.get("plan") or {}).get("id"),
              (again.get("plan") or {}).get("id"))

        # ---- a step that fails does not leave the plan hanging ----------
        status, answer = call(queue_port, "POST", "/api/plans", {
            "title": "会失败的计划",
            "goal": "看看失败怎么办",
            "steps": [{"key": "boom", "title": "会炸", "prompt": "这一步会失败"}],
        })
        broken = (answer or {}).get("data") or {}
        broken_parent = (broken.get("parent") or {}).get("id")
        parent_task = wait_task(queue_port, broken_parent, timeout=90)
        check("a plan with a failed step fails", parent_task.get("status") == "failed",
              parent_task.get("status"))
        check("the parent says which step failed",
              "会炸" in (parent_task.get("error") or ""), parent_task.get("error"))

        # ---- a plan can be cancelled -------------------------------------
        status, answer = call(queue_port, "POST", "/api/plans", {
            "title": "要取消的计划",
            "goal": "取消它",
            "steps": [{"key": "one", "title": "第一步", "prompt": "取消前的第一步"}],
        })
        to_cancel = (answer or {}).get("data") or {}
        cancel_id = (to_cancel.get("plan") or {}).get("id")
        status, answer = call(queue_port, "POST", "/api/plans/%s/cancel" % cancel_id)
        check("the plan can be cancelled", status == 200, answer)
        cancelled = ((answer or {}).get("data") or {}).get("plan") or {}
        check("the cancelled plan says so", cancelled.get("status") == "cancelled",
              cancelled.get("status"))

        # ---- a broken plan is refused, not a crash -----------------------
        status, answer = call(queue_port, "POST", "/api/plans", {"title": "坏的", "steps": []})
        check("a plan without steps is refused by the API", status == 400, status)
        status, answer = call(queue_port, "POST", "/api/plans", {
            "title": "环", "steps": [{"key": "a", "prompt": "x", "depends_on": ["b"]},
                                     {"key": "b", "prompt": "y", "depends_on": ["a"]}]})
        check("a plan with a circle is refused by the API", status == 400, status)
        status, answer = call(queue_port, "GET", "/api/plans/pl_nope")
        check("an unknown plan is a 404", status == 404, status)
    finally:
        queue.terminate()
        try:
            queue.wait(timeout=10)
        except subprocess.TimeoutExpired:
            queue.kill()
        try:
            agent.shutdown()
        except Exception:  # noqa: BLE001
            pass
        shutil.rmtree(tmp, ignore_errors=True)

    if failures:
        print("FAIL")
        for f in failures:
            print(" -", f)
        return 1
    print("PASS: a plan becomes steps, the steps wait for each other, the plan is summarized")
    return 0


if __name__ == "__main__":
    sys.exit(main())
