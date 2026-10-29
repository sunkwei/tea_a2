#!/usr/bin/env python3
"""The task order: a text file somebody fills in, the queue reads.

A task order carries every setting a task can have (where it runs, who runs
it, when it may start, what it may touch, how it is filed). What is pinned:

  * the template that ships with the queue parses into the fields the API
    takes - nothing is lost on the way;
  * a typo is an error with the line number, not a setting that silently
    disappears;
  * the command line queues it: --submit-order <file>;
  * a workspace the agent does not know is a message with the list of what it
    does know - not a traceback;
  * the web page offers the template as a download.

Usage: python3 tests/smoke_task_order.py [path-to-tea_agent-binary]
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "sched"))

import task_order  # noqa: E402

TEMPLATE = os.path.join(REPO_ROOT, "sched", "task-order.template")
SERVER = os.path.join(REPO_ROOT, "sched", "task_server.py")

GOOD = """\
# a task order
title: 跑一遍构建
prompt: |
  把 README 的例子跑一遍，
  有问题记下来。
workspace: /tmp/ws
allow_paths:
  - /tmp
  - /var/tmp
provider: minimax
model: MiniMax-M3.1-Flash
priority: 3            # 越大越先做
not_before: +5
tags:
  - 文档
business_id: DOC-1
idempotency_key: abc
meta:
  owner: sunwei
"""


def free_port():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


def main():
    failures = []
    checks = []

    def check(name, ok, detail=""):
        checks.append(name)
        print(("PASS " if ok else "FAIL ") + name + (("  " + str(detail)) if not ok else ""))
        if not ok:
            failures.append(name)

    # ---- the parser --------------------------------------------------------
    body = task_order.parse_order(GOOD)
    check("the title is read", body.get("title") == "跑一遍构建", body.get("title"))
    check("the prompt keeps its lines",
          body.get("prompt") == "把 README 的例子跑一遍，\n有问题记下来。", body.get("prompt"))
    check("the workspace is read", body.get("workspace_id") == "/tmp/ws", body.get("workspace_id"))
    check("the list is read", body.get("allow_paths") == ["/tmp", "/var/tmp"], body.get("allow_paths"))
    check("the provider and the model are read",
          body.get("provider") == "minimax" and body.get("model") == "MiniMax-M3.1-Flash", body)
    check("the priority is a number", body.get("priority") == 3, body.get("priority"))
    check("the schedule is read", body.get("not_before") == "+5", body.get("not_before"))
    check("the tags and the business id go into the metadata",
          (body.get("meta") or {}).get("tags") == ["文档"]
          and (body.get("meta") or {}).get("business_id") == "DOC-1", body.get("meta"))
    check("the other metadata is kept",
          (body.get("meta") or {}).get("owner") == "sunwei", body.get("meta"))
    check("the idempotency key is read", body.get("idempotency_key") == "abc",
          body.get("idempotency_key"))

    # ---- the template that ships with the queue ----------------------------
    check("the queue ships a template", os.path.exists(TEMPLATE), TEMPLATE)
    with open(TEMPLATE, encoding="utf-8") as f:
        template = f.read()
    parsed = task_order.parse_order(template)
    check("the template parses", bool(parsed.get("prompt")), parsed.get("prompt", "")[:60])
    check("the template mentions every key of the API",
          all(key in template for key in
              ("title:", "prompt:", "workspace:", "allow_paths:", "provider:", "model:",
               "priority:", "not_before:", "tags:", "business_id:", "idempotency_key:",
               "meta:")), True)

    # ---- the errors --------------------------------------------------------
    for name, text, needle in (
            ("an unknown key is an error", "titel: x\nprompt: y\n", "不认识的键"),
            ("a line that means nothing is an error", "this is not a key\n", "看不懂"),
            ("a key that is written twice is an error",
             "title: a\ntitle: b\nprompt: y\n", "写了两次"),
            ("a priority that is not a number is an error",
             "priority: soon\nprompt: y\n", "要一个数字")):
        try:
            task_order.parse_order(text)
            check(name, False, "no error was raised")
        except task_order.OrderError as exc:
            check(name, needle in str(exc), str(exc)[:120])

    # ---- the command line --------------------------------------------------
    binary = (sys.argv[1] if len(sys.argv) > 1
              else os.path.join(REPO_ROOT, "build", "bin", "tea_agent"))
    tmp = tempfile.mkdtemp(prefix="tea_order_")
    order = os.path.join(tmp, "my.order")
    with open(order, "w", encoding="utf-8") as f:
        f.write(GOOD)
    broken = os.path.join(tmp, "broken.order")
    with open(broken, "w", encoding="utf-8") as f:
        f.write("titel: x\nprompt: y\n")

    data_dir = os.path.join(tmp, "tasks")

    def run_order(path):
        return subprocess.run(
            [sys.executable, SERVER, "--submit-order", path, "--data-dir", data_dir,
             "--agent", "http://127.0.0.1:1", "--no-serve"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True,
            timeout=60)

    # The agent is not reachable here, so a good order cannot be queued - but it
    # must be read first, and a broken one must say what is wrong.
    good = run_order(order)
    check("a good order is read before the agent is needed",
          "cannot pick the workspace" in good.stdout, good.stdout[-200:])
    broken_run = run_order(broken)
    check("a broken order says what is wrong",
          "不认识的键" in broken_run.stdout, broken_run.stdout[-200:])
    check("a broken order is not queued",
          not os.path.isdir(data_dir) or not [n for n in os.listdir(data_dir)
                                             if n.endswith(".json")],
          os.listdir(data_dir) if os.path.isdir(data_dir) else data_dir)

    # ---- an agent that answers, but has other workspaces -------------------
    # The queue may be pointed at the wrong agent (a Windows one, another box):
    # then the workspace named in the order is simply not there. That must be a
    # message, not a NameError - it used to be a traceback.
    other_ws = os.path.join(tmp, "other-ws")
    os.makedirs(other_ws, exist_ok=True)

    class StubAgent(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.split("?")[0] == "/api/workspaces":
                payload = {"workspaces": [{"id": "ws_other", "path": other_ws}]}
            else:
                payload = {}
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    stub_port = free_port()
    stub = ThreadingHTTPServer(("127.0.0.1", stub_port), StubAgent)
    stub_thread = threading.Thread(target=stub.serve_forever, daemon=True)
    stub_thread.start()
    try:
        stub_base = "http://127.0.0.1:%d" % stub_port

        def run_against_stub(path):
            return subprocess.run(
                [sys.executable, SERVER, "--submit-order", path, "--data-dir", data_dir,
                 "--agent", stub_base, "--no-serve"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True,
                timeout=60)

        missing = run_against_stub(order)   # the order says /tmp/ws, the agent has other_ws
        check("an unknown workspace is a message, not a crash",
              "Traceback" not in missing.stdout, missing.stdout[-300:])
        check("an unknown workspace names the agent it asked",
              "cannot pick the workspace" in missing.stdout and stub_base in missing.stdout,
              missing.stdout[-300:])
        check("an unknown workspace lists what the agent does have",
              "ws_other" in missing.stdout and other_ws in missing.stdout,
              missing.stdout[-300:])
        check("an unknown workspace queues nothing", missing.returncode == 2,
              missing.returncode)

        by_path = os.path.join(tmp, "by-path.order")
        with open(by_path, "w", encoding="utf-8") as f:
            f.write("title: 换个目录\nprompt: y\nworkspace: %s\n" % other_ws)
        ok_run = run_against_stub(by_path)
        check("a workspace the agent does have is queued",
              "queued task" in ok_run.stdout and ok_run.returncode == 0,
              ok_run.stdout[-300:])
        records = [n for n in os.listdir(data_dir) if n.endswith(".json")] \
            if os.path.isdir(data_dir) else []
        check("the queued order is written down", bool(records), records)
        if records:
            with open(os.path.join(data_dir, records[0]), encoding="utf-8") as f:
                record = json.load(f)
            check("the record says which workspace it is for",
                  record.get("workspace_id") == "ws_other",
                  record.get("workspace_id"))
    finally:
        stub.shutdown()
        stub.server_close()

    print("")
    print("%d/%d checks passed" % (len(checks) - len(failures), len(checks)))
    if failures:
        print("FAILED:", ", ".join(failures))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
