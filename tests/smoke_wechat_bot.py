#!/usr/bin/env python3
"""The WeChat bot: protocol, command mapping, notifications.

The iLink server is mocked (the real one needs a phone and a scan), and so is
the task server. What this pins:

  * the login flow: a QR code, then the status poll, then the credentials on
    disk - and the context_token of every user remembered, because a reply
    without it cannot be routed back to WeChat;
  * the long poll: a message in, a reply out, the reply carrying the
    context_token;
  * the command mapping: plain text queues a task, /tasks /status /result /
    start /cancel /del /answer /quota reach the queue's API;
  * the fixed workspace: the WeChat tasks always go to the same one;
  * the notification: when a task finishes, the user is told.

Usage: python3 tests/smoke_wechat_bot.py
"""

import http.client
import json
import os
import random
import shutil
import socket
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "sched"))

import wechat_bot  # noqa: E402


def free_port():
    while True:
        port = random.randint(20000, 60000)
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue


class MockILink(BaseHTTPRequestHandler):
    """The iLink server: a QR code, a message to hand out, the replies to collect."""

    protocol_version = "HTTP/1.1"
    sent = []            # the messages the bot sent
    pending = []         # the messages the bot should receive
    qr_status = "confirmed"
    token = "bot-token-123"

    def log_message(self, *args):
        pass

    def _json(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if "get_qrcode_status" in self.path:
            return self._json({"status": MockILink.qr_status,
                               "bot_token": MockILink.token,
                               "ilink_bot_id": "bot-1",
                               "ilink_user_id": "me-1",
                               "baseurl": "http://127.0.0.1:%d" % self.server.server_address[1]})
        self._json({"error": "no route"}, 404)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            body = {}
        if "get_bot_qrcode" in self.path:
            return self._json({"qrcode": "qrc_test",
                               "qrcode_img_content": "https://weixin.qq.com/x/test"})
        if "getupdates" in self.path:
            if MockILink.pending:
                msgs = MockILink.pending
                MockILink.pending = []
                return self._json({"ret": 0, "msgs": msgs,
                                   "context_token": "ctx-from-server",
                                   "get_updates_buf": "cursor-%d" % random.randint(1, 999999)})
            # Nothing to say: hold the connection a moment, like the real one.
            time.sleep(0.4)
            return self._json({"ret": 0, "msgs": [], "get_updates_buf": body.get("get_updates_buf", "")})
        if "sendmessage" in self.path:
            MockILink.sent.append({"msg": body.get("msg") or {},
                                   "auth": self.headers.get("Authorization", "")})
            return self._json({"ret": 0})
        if "getconfig" in self.path:
            return self._json({"ret": 0, "typing_ticket": "ticket-1"})
        if "sendtyping" in self.path:
            return self._json({"ret": 0})
        self._json({"error": "no route"}, 404)


class MockQueue(BaseHTTPRequestHandler):
    """The task server's API, only the parts the bot uses."""

    protocol_version = "HTTP/1.1"
    calls = []
    tasks = {}
    counter = 0

    def log_message(self, *args):
        pass

    def _json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        MockQueue.calls.append(("GET", self.path))
        if self.path.startswith("/api/health"):
            return self._json({"ok": True, "provider": "minimax", "quota_percent": 42.0})
        if self.path.startswith("/api/tasks?"):
            return self._json({"tasks": list(MockQueue.tasks.values()),
                               "counts": {"queued": 1, "running": 0, "done": 0}})
        if self.path.startswith("/api/tasks/"):
            task_id = self.path.split("/api/tasks/")[1].split("?")[0]
            task = MockQueue.tasks.get(task_id)
            if task is None:
                return self._json({"error": {"code": "not_found", "message": "no task"}}, 404)
            return self._json(task)
        self._json({"error": {"code": "not_found"}}, 404)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            body = {}
        MockQueue.calls.append(("POST", self.path, body))
        if self.path == "/api/tasks":
            MockQueue.counter += 1
            task_id = "t_mock%03d" % MockQueue.counter
            task = {"id": task_id, "workspace_id": body.get("workspace_id", ""),
                    "prompt": body.get("prompt", ""), "title": body.get("title", ""),
                    "status": "queued", "progress": 0, "note": "在做了", "steps": [],
                    "result": "这是结果",
                    "meta": body.get("meta") or {}, "artifacts": [],
                    "needs_confirmation": False, "question": "",
                    "allow_paths": body.get("allow_paths") or []}
            MockQueue.tasks[task_id] = task
            return self._json(task)
        if "/start" in self.path or "/cancel" in self.path or "/confirm" in self.path:
            task_id = self.path.split("/api/tasks/")[1].split("/")[0]
            task = MockQueue.tasks.get(task_id)
            if task is None:
                return self._json({"error": {"code": "not_found"}}, 404)
            if "/start" in self.path:
                task["status"] = "running"
            elif "/cancel" in self.path:
                task["status"] = "cancelled"
            else:
                task["status"] = "running"
                task["needs_confirmation"] = False
            return self._json(task)
        self._json({"error": {"code": "not_found"}}, 404)

    def do_DELETE(self):  # noqa: N802
        MockQueue.calls.append(("DELETE", self.path))
        task_id = self.path.split("/api/tasks/")[1].split("?")[0]
        MockQueue.tasks.pop(task_id, None)
        self._json({"ok": True, "id": task_id})


class MockAgent(BaseHTTPRequestHandler):
    """The agent's workspace list."""

    protocol_version = "HTTP/1.1"
    workspaces = []

    def log_message(self, *args):
        pass

    def _json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/api/workspaces"):
            return self._json({"workspaces": MockAgent.workspaces})
        self._json({"error": {"code": "not_found"}}, 404)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            body = {}
        entry = {"id": "ws_registered", "path": body.get("path", ""), "name": body.get("name", "")}
        MockAgent.workspaces.append(entry)
        self._json(entry)


def user_message(text, user="u1", context="ctx-1"):
    return {"message_type": 1, "from_user_id": user, "context_token": context,
            "create_time_ms": int(time.time() * 1000),
            "item_list": [{"type": 1, "text_item": {"text": text}}]}


def main():
    failures = []
    checks = []

    def check(name, ok, detail=""):
        checks.append(name)
        print(("PASS " if ok else "FAIL ") + name + (("  " + str(detail)) if not ok else ""))
        if not ok:
            failures.append(name)

    ilink = ThreadingHTTPServer(("127.0.0.1", 0), MockILink)
    ilink.daemon_threads = True
    threading.Thread(target=ilink.serve_forever, daemon=True).start()
    queue = ThreadingHTTPServer(("127.0.0.1", 0), MockQueue)
    queue.daemon_threads = True
    threading.Thread(target=queue.serve_forever, daemon=True).start()
    agent = ThreadingHTTPServer(("127.0.0.1", 0), MockAgent)
    agent.daemon_threads = True
    threading.Thread(target=agent.serve_forever, daemon=True).start()

    tmp = tempfile.mkdtemp(prefix="tea_wechat_")
    ws_path = os.path.join(tmp, "wechat-workspace")
    args = wechat_bot.build_parser().parse_args([
        "--base-url", "http://127.0.0.1:%d" % ilink.server_address[1],
        "--agent", "http://127.0.0.1:%d" % agent.server_address[1],
        "--tasks", "http://127.0.0.1:%d" % queue.server_address[1],
        "--state-dir", os.path.join(tmp, "state"),
        "--workspace-path", ws_path,
        "--allow-path", "/tmp",
    ])
    bot = wechat_bot.WeChatBot(args)

    try:
        # ---- the login flow ---------------------------------------------
        ok = bot.login()
        check("the login flow finishes", ok)
        state = json.load(open(os.path.join(tmp, "state", "credentials.json"), encoding="utf-8"))
        check("the bot token is stored", state.get("token") == "bot-token-123", state.get("token"))
        check("the base url comes from the server",
              "127.0.0.1" in state.get("baseurl", state.get("base_url", "")), state)

        # ---- the workspace ----------------------------------------------
        ws_id = bot.ensure_workspace()
        check("the WeChat side works in a fixed workspace of its own",
              bool(ws_id) and os.path.isdir(ws_path), (ws_id, ws_path))
        check("the workspace is registered with the agent",
              any(w.get("path") == ws_path for w in MockAgent.workspaces), MockAgent.workspaces)

        # ---- plain text queues a task -----------------------------------
        MockQueue.calls = []
        reply = bot.handle("把 README 跑一遍", "u1")
        check("plain text queues a task", "已排队" in reply, reply)
        check("the reply carries the task id", "t_mock" in reply, reply)
        created = [c for c in MockQueue.calls if c[0] == "POST" and c[1] == "/api/tasks"]
        check("the task went to the queue", len(created) == 1, MockQueue.calls)
        body = created[0][2] if created else {}
        check("the task is in the fixed workspace", body.get("workspace_id") == ws_id, body)
        check("the task remembers who queued it",
              (body.get("meta") or {}).get("wechat_user_id") == "u1", body)
        check("the task carries the allowed directory",
              body.get("allow_paths") == ["/tmp"], body)
        # The id comes back in the response; the request body has none.
        task_id = sorted(MockQueue.tasks)[-1] if MockQueue.tasks else ""
        check("the queued task has an id", bool(task_id), MockQueue.tasks)

        # ---- the commands -----------------------------------------------
        MockQueue.calls = []
        check("/tasks lists the queue", "任务" in bot.handle("/tasks", "u1"), bot.handle("/tasks", "u1"))
        check("/status shows one task", task_id in bot.handle("/status " + task_id, "u1"),
              bot.handle("/status " + task_id, "u1"))
        result_reply = bot.handle("/result " + task_id, "u1")
        check("/result shows the result", "最终回答" in result_reply and "这是结果" in result_reply,
              result_reply)
        check("/start starts it", "running" in bot.handle("/start " + task_id, "u1"),
              bot.handle("/start " + task_id, "u1"))
        check("/cancel stops it", "cancelled" in bot.handle("/cancel " + task_id, "u1"),
              bot.handle("/cancel " + task_id, "u1"))
        check("/answer answers a waiting task",
              "已回答" in bot.handle("/answer %s 可以" % task_id, "u1"),
              bot.handle("/answer %s 可以" % task_id, "u1"))
        check("/del deletes it", "删掉了" in bot.handle("/del " + task_id, "u1"),
              bot.handle("/del " + task_id, "u1"))
        check("/quota says how much is left", "42" in bot.handle("/quota", "u1"),
              bot.handle("/quota", "u1"))
        check("/help lists the commands", "用法" in bot.handle("/help", "u1"))
        check("an unknown command says so", "不认识" in bot.handle("/nonsense", "u1"))

        # ---- the poll loop: a message in, a reply out -------------------
        MockILink.sent = []
        MockILink.pending = [user_message("排个活儿")]
        messages = bot.poll_once()
        check("the long poll hands the message over", len(messages) == 1, messages)
        raw = messages[0]
        bot.state.remember_context(raw["from_user_id"], raw["context_token"])
        answer = bot.handle(wechat_bot.text_of(raw), raw["from_user_id"])
        bot.reply(raw["from_user_id"], answer)
        check("the reply went out", len(MockILink.sent) == 1, MockILink.sent)
        if MockILink.sent:
            sent = MockILink.sent[0]["msg"]
            check("the reply carries the context token",
                  sent.get("context_token") == "ctx-1", sent)
            check("the reply goes to the user", sent.get("to_user_id") == "u1", sent)
            check("the reply is a text message",
                  (sent.get("item_list") or [{}])[0].get("text_item", {}).get("text", "") != "",
                  sent)
        check("the context token is remembered",
              bot.state.context_for("u1") == "ctx-1", bot.state.context_for("u1"))

        # ---- the notification when a task finishes ----------------------
        MockILink.sent = []
        bot.owner[task_id] = "u1"
        event = {"type": "task_done", "task": {"id": task_id, "note": "做完了",
                                               "result": "答案", "meta": {}}}
        bot.notify_event(event)
        check("the user is told when the work is done",
              any("做完了" in (m["msg"].get("item_list") or [{}])[0]
                  .get("text_item", {}).get("text", "") for m in MockILink.sent),
              MockILink.sent)
    finally:
        ilink.shutdown()
        queue.shutdown()
        agent.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    print("%d/%d checks passed" % (len(checks) - len(failures), len(checks)))
    if failures:
        print("FAILED:", ", ".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
