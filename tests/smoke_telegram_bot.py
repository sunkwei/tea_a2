#!/usr/bin/env python3
"""The Telegram bot: the protocol, the command mapping, the files.

The Telegram Bot API is mocked (the real one needs a token), and so is the
task server. What this pins:

  * the token: getMe works, the command menu goes through, the chat ids of
    the waiting messages can be printed - a bot that answers nobody is a
    bot that does nothing, and a public bot that answers everybody is worse;
  * the long poll: a message in, a reply out;
  * the command mapping: plain text queues a task, /tasks /status /result /
    start /cancel /del /answer /quota reach the queue's API - the same
    commands the WeChat side has;
  * the fixed workspace: the Telegram tasks always go to the same one;
  * the notification: when a task finishes, the user is told;
  * the files, both ways: a file the task produced is sent as a document, a
    file the user sends is fetched and put in the workspace's inbox - where
    the task can read it - and a file name from the outside cannot escape
    the inbox;
  * the offset: the update offset survives a restart, so a message is not
    answered twice.

Usage: python3 tests/smoke_telegram_bot.py
"""

import json
import os
import random
import shutil
import socket
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
sys.path.insert(0, os.path.join(REPO_ROOT, "sched"))

import channel  # noqa: E402
import telegram_bot  # noqa: E402


def free_port():
    while True:
        port = random.randint(20000, 60000)
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue


class MockTelegram(BaseHTTPRequestHandler):
    """The Bot API: the updates to hand out, the messages to collect."""

    protocol_version = "HTTP/1.1"
    sent = []            # the messages the bot sent
    documents = []       # the files the bot sent
    pending = []         # the updates the bot should receive
    files = {}           # file_id -> the bytes behind it
    commands = []        # the command menu the bot set
    bot_username = "tea_test_bot"

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
        # The file download: /file/bot<token>/<path>
        if "/file/bot" in self.path:
            name = self.path.rsplit("/", 1)[-1]
            data = MockTelegram.files.get(name)
            if data is None:
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self._json({"ok": False, "error_code": 404, "description": "no route"})

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        method = self.path.rsplit("/", 1)[-1]
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except ValueError:
            body = {}
        if method == "getUpdates":
            if MockTelegram.pending:
                updates = MockTelegram.pending
                MockTelegram.pending = []
                return self._json({"ok": True, "result": updates})
            return self._json({"ok": True, "result": []})
        if method == "sendMessage":
            MockTelegram.sent.append(body)
            return self._json({"ok": True, "result": {"message_id": len(MockTelegram.sent)}})
        if method == "sendDocument":
            MockTelegram.documents.append({
                "body": raw,
                "content_type": self.headers.get("Content-Type", "")})
            return self._json({"ok": True, "result": {"document": {"file_id": "sent-1"}}})
        if method == "getFile":
            file_id = body.get("file_id") or ""
            return self._json({"ok": True,
                               "result": {"file_id": file_id,
                                          "file_path": "files/%s" % file_id}})
        if method == "getMe":
            return self._json({"ok": True,
                               "result": {"id": 42,
                                          "username": MockTelegram.bot_username,
                                          "first_name": "Tea"}})
        if method == "setMyCommands":
            MockTelegram.commands = body.get("commands") or []
            return self._json({"ok": True, "result": True})
        self._json({"ok": False, "error_code": 404, "description": "no route"})


class MockQueue(BaseHTTPRequestHandler):
    """The task server's API, only the parts the bot uses."""

    protocol_version = "HTTP/1.1"
    calls = []
    tasks = {}
    counter = 0

    def log_message(self, *args):
        pass

    def _json(self, payload, status=200):
        # The queue answers in one shape: {code, message, request_id, data}.
        body = json.dumps({"code": 0, "message": "ok", "request_id": "req_mock",
                           "data": payload}, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status, code, message):
        body = json.dumps({"code": 40401, "message": message, "request_id": "req_mock",
                           "data": {"error": {"code": code, "message": message}}}).encode()
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
        self._error(404, "not_found", "no such endpoint")

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            body = {}
        MockQueue.calls.append(("POST", self.path, body,
                                self.headers.get("Idempotency-Key", "")))
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
        if "/start" in self.path or "/cancel" in self.path or "/confirm" in self.path \
                or "/retry" in self.path:
            task_id = self.path.split("/api/tasks/")[1].split("/")[0]
            task = MockQueue.tasks.get(task_id)
            if task is None:
                return self._json({"error": {"code": "not_found"}}, 404)
            if "/start" in self.path:
                task["status"] = "running"
            elif "/cancel" in self.path:
                task["status"] = "cancelled"
            elif "/retry" in self.path:
                task["status"] = "queued"
                task["attempt"] = int(task.get("attempt") or 1) + 1
            else:
                task["status"] = "running"
                task["needs_confirmation"] = False
            return self._json(task)
        self._error(404, "not_found", "no such endpoint")

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
        self._error(404, "not_found", "no such endpoint")

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


def message(text, chat="4242", update_id=1, **extra):
    """One Telegram message."""
    msg = {"message_id": update_id, "date": int(time.time()),
           "chat": {"id": int(chat), "type": "private"},
           "from": {"id": int(chat), "first_name": "Tester"},
           "text": text}
    msg.update(extra)
    return {"update_id": update_id, "message": msg}


def main():
    failures = []
    checks = []

    def check(name, ok, detail=""):
        checks.append(name)
        print(("PASS " if ok else "FAIL ") + name + (("  " + str(detail)) if not ok else ""))
        if not ok:
            failures.append(name)

    tg = ThreadingHTTPServer(("127.0.0.1", 0), MockTelegram)
    tg.daemon_threads = True
    threading.Thread(target=tg.serve_forever, daemon=True).start()
    queue = ThreadingHTTPServer(("127.0.0.1", 0), MockQueue)
    queue.daemon_threads = True
    threading.Thread(target=queue.serve_forever, daemon=True).start()
    agent = ThreadingHTTPServer(("127.0.0.1", 0), MockAgent)
    agent.daemon_threads = True
    threading.Thread(target=agent.serve_forever, daemon=True).start()

    tmp = tempfile.mkdtemp(prefix="tea_telegram_")
    ws_path = os.path.join(tmp, "telegram-workspace")
    api = "http://127.0.0.1:%d" % tg.server_address[1]
    args = telegram_bot.build_parser().parse_args([
        "--token", "123456:ABCDEF",
        "--api-url", api,
        "--agent", "http://127.0.0.1:%d" % agent.server_address[1],
        "--tasks", "http://127.0.0.1:%d" % queue.server_address[1],
        "--state-dir", os.path.join(tmp, "state"),
        "--workspace-path", ws_path,
        "--allow-path", "/tmp",
        "--allow-chat", "4242",
    ])
    bot = telegram_bot.TelegramBot(args)

    try:
        # ---- the token ---------------------------------------------------
        me = bot.telegram.get_me()
        check("the token works", me.get("username") == "tea_test_bot", me)
        bot.telegram.set_commands(telegram_bot.COMMANDS)
        check("the command menu goes through",
              any(c.get("command") == "tasks" for c in MockTelegram.commands),
              MockTelegram.commands)

        # ---- the chat ids ------------------------------------------------
        MockTelegram.pending = [message("你好", chat="4242", update_id=7)]
        rc = bot.print_chat_ids()
        check("the chat id of a waiting message is printed", rc == 0, rc)
        MockTelegram.pending = []

        # ---- the workspace ------------------------------------------------
        ws_id = bot.ensure_workspace()
        check("the Telegram side works in a fixed workspace of its own",
              bool(ws_id) and os.path.isdir(ws_path), (ws_id, ws_path))
        check("the workspace is registered with the agent",
              any(w.get("path") == ws_path for w in MockAgent.workspaces), MockAgent.workspaces)

        # ---- the long poll: a message in, a reply out --------------------
        MockTelegram.sent = []
        MockTelegram.pending = [message("排个活儿", chat="4242", update_id=10)]
        updates = bot.poll_once()
        check("the long poll hands the update over", len(updates) == 1, updates)
        for update in updates:
            bot.process_update(update)
        check("the reply went out", len(MockTelegram.sent) == 1, MockTelegram.sent)
        if MockTelegram.sent:
            sent = MockTelegram.sent[0]
            check("the reply goes to the chat", str(sent.get("chat_id")) == "4242", sent)
            check("the reply carries the text",
                  "已排队" in (sent.get("text") or ""), sent)
        check("the offset is remembered", bot.state.offset == 11, bot.state.offset)
        state_file = os.path.join(tmp, "state", "state.json")
        check("the offset is on disk",
              os.path.isfile(state_file)
              and json.load(open(state_file, encoding="utf-8")).get("offset") == 11,
              state_file)

        # ---- a chat that is not on the list is ignored ---------------------
        MockTelegram.sent = []
        bot.process_update(message("我也要用", chat="9999", update_id=11))
        check("a chat that is not allowed gets no answer", not MockTelegram.sent,
              MockTelegram.sent)

        # ---- plain text queues a task ------------------------------------
        MockQueue.calls = []
        reply = bot.handle("把 README 跑一遍", "4242")
        check("plain text queues a task", "已排队" in reply, reply)
        check("the reply carries the task id", "t_mock" in reply, reply)
        created = [c for c in MockQueue.calls if c[0] == "POST" and c[1] == "/api/tasks"]
        check("the task went to the queue", len(created) == 1, MockQueue.calls)
        body = created[0][2] if created else {}
        check("the task is in the fixed workspace", body.get("workspace_id") == ws_id, body)
        check("the task remembers who queued it",
              (body.get("meta") or {}).get("telegram_user_id") == "4242", body)
        check("the task carries the allowed directory",
              body.get("allow_paths") == ["/tmp"], body)
        task_id = sorted(MockQueue.tasks)[-1] if MockQueue.tasks else ""

        # ---- the same command twice is one task ---------------------------
        MockQueue.calls = []
        bot.handle("同一个活儿", "4242")
        bot.handle("同一个活儿", "4242")
        created = [c for c in MockQueue.calls if c[0] == "POST" and c[1] == "/api/tasks"]
        check("the request carries an idempotency key",
              created and created[0][3], created[0] if created else None)
        check("the same command carries the same key",
              len(created) == 2 and created[0][3] == created[1][3],
              [c[3] for c in created])

        # ---- a task that waits for its time -------------------------------
        MockQueue.calls = []
        bot.handle("/at 23:30 把 README 跑一遍", "4242")
        created = [c for c in MockQueue.calls if c[0] == "POST" and c[1] == "/api/tasks"]
        check("/at queues the task with a time",
              created and created[0][2].get("not_before") == "23:30", created)
        MockQueue.calls = []
        bot.handle("把 README 跑一遍 @+5", "4242")
        created = [c for c in MockQueue.calls if c[0] == "POST" and c[1] == "/api/tasks"]
        check("a trailing @time is split off the task",
              created and created[0][2].get("not_before") == "+5"
              and created[0][2].get("prompt") == "把 README 跑一遍", created)

        # ---- the commands ------------------------------------------------
        check("/tasks lists the queue", "任务" in bot.handle("/tasks", "4242"),
              bot.handle("/tasks", "4242"))
        check("/status shows one task", task_id in bot.handle("/status " + task_id, "4242"),
              bot.handle("/status " + task_id, "4242"))
        result_reply = bot.handle("/result " + task_id, "4242")
        check("/result shows the result", "最终回答" in result_reply and "这是结果" in result_reply,
              result_reply)
        check("/start starts it", "running" in bot.handle("/start " + task_id, "4242"),
              bot.handle("/start " + task_id, "4242"))
        check("/cancel stops it", "cancelled" in bot.handle("/cancel " + task_id, "4242"),
              bot.handle("/cancel " + task_id, "4242"))
        check("/answer answers a waiting task",
              "已回答" in bot.handle("/answer %s 可以" % task_id, "4242"),
              bot.handle("/answer %s 可以" % task_id, "4242"))
        check("/quota says how much is left", "42" in bot.handle("/quota", "4242"),
              bot.handle("/quota", "4242"))
        live_id = sorted(MockQueue.tasks)[-1] if MockQueue.tasks else ""
        check("/retry asks the queue to try again",
              "重试" in bot.handle("/retry " + live_id, "4242"),
              bot.handle("/retry " + live_id, "4242"))
        check("/help lists the commands", "用法" in bot.handle("/help", "4242"))
        check("an unknown command says so", "不认识" in bot.handle("/nonsense", "4242"))

        # ---- the command Telegram writes with the bot's name --------------
        check("a command with @botname is understood",
              "任务" in bot.handle("/tasks@tea_test_bot", "4242"),
              bot.handle("/tasks@tea_test_bot", "4242"))
        check("the @botname is taken off the command",
              bot.normalize("/status@tea_test_bot t1") == "/status t1",
              bot.normalize("/status@tea_test_bot t1"))

        # ---- the notification when a task finishes ------------------------
        MockTelegram.sent = []
        bot.owner[task_id] = "4242"
        bot.notify_event({"type": "task_done", "task": {"id": task_id, "note": "做完了",
                                                        "result": "答案", "meta": {}}})
        check("the user is told when the work is done",
              any("做完了" in (m.get("text") or "") for m in MockTelegram.sent),
              MockTelegram.sent)

        # ---- out: the files a task produced go to the phone --------------
        produced = os.path.join(ws_path, "notes.txt")
        content = "这是任务写出来的文件\n第二行\n".encode("utf-8")
        with open(produced, "wb") as f:
            f.write(content)
        MockTelegram.sent = []
        MockTelegram.documents = []
        bot.owner[task_id] = "4242"
        bot.notify_event({"type": "task_done", "task": {
            "id": task_id, "note": "做完了", "result": "答案", "meta": {},
            "artifacts": [{"path": "notes.txt", "bytes": len(content),
                           "kind": "document", "tool": "toolkit_file"}]}})
        check("the produced file is sent as a document", len(MockTelegram.documents) == 1,
              MockTelegram.documents)
        if MockTelegram.documents:
            body = MockTelegram.documents[0]["body"]
            check("the document is a multipart body",
                  "multipart/form-data" in MockTelegram.documents[0]["content_type"]
                  and b"filename=\"notes.txt\"" in body,
                  MockTelegram.documents[0]["content_type"])
            check("the document carries the bytes", content in body, body[:120])
            check("the document names the chat", b'name="chat_id"' in body
                  and b"4242" in body, body[:200])

        # ---- in: a file the user sends arrives in the workspace -----------
        incoming = "用户发来的内容\n".encode("utf-8")
        MockTelegram.files["f_doc"] = incoming
        update = message("", chat="4242", update_id=20,
                         document={"file_id": "f_doc", "file_name": "../../evil.sh"})
        MockQueue.calls = []
        MockTelegram.sent = []
        bot.process_update(update)
        check("the bot says it got the file",
              any("已收到文件" in (m.get("text") or "") for m in MockTelegram.sent),
              MockTelegram.sent)
        inbox = os.path.join(ws_path, "inbox")
        saved = os.path.join(inbox, "evil.sh")
        check("the file is saved in the workspace", os.path.isfile(saved),
              os.listdir(inbox) if os.path.isdir(inbox) else inbox)
        if os.path.isfile(saved):
            with open(saved, "rb") as f:
                check("the saved file is the one the user sent", f.read() == incoming, saved)
        check("the file name cannot escape the inbox",
              not os.path.exists(os.path.join(ws_path, "evil.sh")), ws_path)
        created = [c for c in MockQueue.calls if c[0] == "POST" and c[1] == "/api/tasks"]
        check("the file becomes a task", len(created) == 1, MockQueue.calls)
        if created:
            prompt = created[0][2].get("prompt") or ""
            check("the task prompt names the file", "inbox/evil.sh" in prompt, prompt)
            check("the task remembers the attachment",
                  (created[0][2].get("meta") or {}).get("attachments") == ["inbox/evil.sh"],
                  created[0][2].get("meta"))

        # ---- in: a picture ------------------------------------------------
        picture = b"\x89PNG\r\n\x1a\n" + b"0" * 32
        MockTelegram.files["f_pic"] = picture
        update = message("看看这张图", chat="4242", update_id=21,
                         photo=[{"file_id": "f_pic_small", "width": 90},
                                {"file_id": "f_pic", "width": 800}])
        MockQueue.calls = []
        MockTelegram.sent = []
        bot.process_update(update)
        created = [c for c in MockQueue.calls if c[0] == "POST" and c[1] == "/api/tasks"]
        check("a picture becomes a task", len(created) == 1, MockQueue.calls)
        if created:
            prompt = created[0][2].get("prompt") or ""
            check("the prompt names the picture", "inbox/image.png" in prompt, prompt)
            check("the prompt tells the agent to look at it",
                  "toolkit_load_image" in prompt, prompt)
        check("the picture is on disk",
              os.path.isfile(os.path.join(inbox, "image.png")),
              os.listdir(inbox) if os.path.isdir(inbox) else inbox)

        # ---- in: a task order the user sends ------------------------------
        order_text = ("title: Telegram 来的任务单\n"
                      "prompt: |\n"
                      "  把这个跑一遍。\n"
                      "not_before: +5\n").encode("utf-8")
        MockTelegram.files["f_order"] = order_text
        update = message("", chat="4242", update_id=22,
                         document={"file_id": "f_order", "file_name": "build.order"})
        MockQueue.calls = []
        MockTelegram.sent = []
        bot.process_update(update)
        check("the bot says it got the task order",
              any("任务单" in (m.get("text") or "") for m in MockTelegram.sent),
              MockTelegram.sent)
        created = [c for c in MockQueue.calls if c[0] == "POST" and c[1] == "/api/tasks"]
        check("the task order becomes one task", len(created) == 1, MockQueue.calls)
        if created:
            body = created[0][2]
            check("the order's prompt is the task",
                  "把这个跑一遍" in (body.get("prompt") or ""), body.get("prompt"))
            check("the order's schedule came through", body.get("not_before") == "+5", body)
            check("the order's title came through",
                  body.get("title") == "Telegram 来的任务单", body)

        # ---- a long answer goes out in pieces -----------------------------
        MockTelegram.sent = []
        bot.send_text("4242", "长" * 9000)
        check("a long text is sent in pieces", len(MockTelegram.sent) > 1,
              len(MockTelegram.sent))
        check("every piece is within the limit",
              all(len(m.get("text") or "") <= 4096 for m in MockTelegram.sent),
              [len(m.get("text") or "") for m in MockTelegram.sent])

        # ---- a restart does not answer the same message twice -------------
        MockTelegram.sent = []
        MockTelegram.pending = [message("再问一次", chat="4242", update_id=30)]
        again = telegram_bot.TelegramBot(args)
        check("the offset survives the restart",
              again.state.offset == bot.state.offset and again.state.offset > 0,
              (again.state.offset, bot.state.offset))
        updates = again.poll_once()
        check("the waiting message is handed to the restarted bot", len(updates) == 1, updates)
        check("the offset moves on", again.state.offset == 31, again.state.offset)

        # ---- the file the task produced can be asked for again -------------
        MockTelegram.documents = []
        files_task = sorted(MockQueue.tasks)[-1]
        MockQueue.tasks[files_task]["artifacts"] = [
            {"path": "notes.txt", "bytes": len(content), "kind": "document"}]
        reply = bot.handle("/files " + files_task, "4242")
        check("/files sends the produced file again",
              len(MockTelegram.documents) == 1, (reply, MockTelegram.documents))
    finally:
        tg.shutdown()
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
