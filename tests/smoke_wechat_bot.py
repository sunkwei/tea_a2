#!/usr/bin/env python3
"""The WeChat bot: protocol, command mapping, notifications, files.

The iLink server is mocked (the real one needs a phone and a scan), and so is
the task server and the WeChat CDN. What this pins:

  * the login flow: a QR code, then the status poll, then the credentials on
    disk - and the context_token of every user remembered, because a reply
    without it cannot be routed back to WeChat;
  * the long poll: a message in, a reply out, the reply carrying the
    context_token;
  * the command mapping: plain text queues a task, /tasks /status /result /
    start /cancel /del /answer /quota reach the queue's API;
  * the fixed workspace: the WeChat tasks always go to the same one;
  * the notification: when a task finishes, the user is told;
  * the files, both ways: a file the task produced is encrypted (AES-128-ECB,
    openssl), uploaded to the CDN and sent as a file message; a file the user
    sends is fetched from the CDN, decrypted, and put in the workspace's inbox
    - where the task can read it. The three key formats of the protocol are
    all understood, and a file name from the outside cannot escape the inbox.

Usage: python3 tests/smoke_wechat_bot.py
"""

import base64
import hashlib
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
import urllib.parse
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
    upload_tickets = []     # the getuploadurl requests the bot made

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
        if "getuploadurl" in self.path:
            MockILink.upload_tickets.append(body)
            return self._json({"ret": 0, "upload_param": "up-%d" % len(MockILink.upload_tickets)})
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


class MockCDN(BaseHTTPRequestHandler):
    """The WeChat CDN: the encrypted bytes live here.

    /upload takes the ciphertext and answers with the x-encrypted-param header
    (the download reference); /download hands the ciphertext back.
    """

    protocol_version = "HTTP/1.1"
    files = {}      # download reference -> ciphertext
    uploads = []    # what the bot pushed up

    def log_message(self, *args):
        pass

    def _query(self):
        if "?" not in self.path:
            return {}
        return urllib.parse.parse_qs(self.path.split("?", 1)[1])

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        query = self._query()
        param = (query.get("encrypted_query_param") or [""])[0]
        filekey = (query.get("filekey") or [""])[0]
        reference = "download-" + param
        MockCDN.files[reference] = raw
        MockCDN.uploads.append({"param": param, "filekey": filekey,
                                 "size": len(raw), "data": raw})
        self.send_response(200)
        self.send_header("x-encrypted-param", reference)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):  # noqa: N802
        query = self._query()
        param = (query.get("encrypted_query_param") or [""])[0]
        data = MockCDN.files.get(param)
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
    cdn = ThreadingHTTPServer(("127.0.0.1", 0), MockCDN)
    cdn.daemon_threads = True
    threading.Thread(target=cdn.serve_forever, daemon=True).start()

    tmp = tempfile.mkdtemp(prefix="tea_wechat_")
    ws_path = os.path.join(tmp, "wechat-workspace")
    args = wechat_bot.build_parser().parse_args([
        "--base-url", "http://127.0.0.1:%d" % ilink.server_address[1],
        "--agent", "http://127.0.0.1:%d" % agent.server_address[1],
        "--tasks", "http://127.0.0.1:%d" % queue.server_address[1],
        "--state-dir", os.path.join(tmp, "state"),
        "--workspace-path", ws_path,
        "--allow-path", "/tmp",
        "--cdn-url", "http://127.0.0.1:%d" % cdn.server_address[1],
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
        check("a queued task shows where it stands",
              "队列" in wechat_bot.fmt_task({"id": "t_x", "status": "queued",
                                             "progress": 0, "title": "t",
                                             "queue_position": 3}),
              wechat_bot.fmt_task({"id": "t_x", "status": "queued", "progress": 0,
                                   "title": "t", "queue_position": 3}))
        check("the task carries the allowed directory",
              body.get("allow_paths") == ["/tmp"], body)
        # The id comes back in the response; the request body has none.
        task_id = sorted(MockQueue.tasks)[-1] if MockQueue.tasks else ""
        check("the queued task has an id", bool(task_id), MockQueue.tasks)

        # ---- the same command twice is one task ---------------------------
        MockQueue.calls = []
        first_reply = bot.handle("同一个活儿", "u1")
        second_reply = bot.handle("同一个活儿", "u1")
        created = [c for c in MockQueue.calls if c[0] == "POST" and c[1] == "/api/tasks"]
        check("the request carries an idempotency key",
              created and created[0][3], created[0] if created else None)
        check("the same command carries the same key",
              len(created) == 2 and created[0][3] == created[1][3],
              [c[3] for c in created])
        check("the reply says it is queued",
              "已排队" in first_reply and "已排队" in second_reply,
              (first_reply, second_reply))

        # ---- a task that waits for its time -------------------------------
        MockQueue.calls = []
        reply = bot.handle("/at 23:30 把 README 跑一遍", "u1")
        created = [c for c in MockQueue.calls if c[0] == "POST" and c[1] == "/api/tasks"]
        check("/at queues the task with a time",
              created and created[0][2].get("not_before") == "23:30", created)
        check("/at says when the task may start", "已排队" in reply, reply)
        MockQueue.calls = []
        reply = bot.handle("把 README 跑一遍 @+5", "u1")
        created = [c for c in MockQueue.calls if c[0] == "POST" and c[1] == "/api/tasks"]
        check("a trailing @time is split off the task",
              created and created[0][2].get("not_before") == "+5"
              and created[0][2].get("prompt") == "把 README 跑一遍", created)
        check("the split-off time is not part of the prompt",
              "把 README 跑一遍" in reply, reply)

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
        live_id = sorted(MockQueue.tasks)[-1] if MockQueue.tasks else ""
        check("/retry asks the queue to try again",
              "重试" in bot.handle("/retry " + live_id, "u1"),
              bot.handle("/retry " + live_id, "u1"))
        check("/answer says the task goes back into the queue",
              "队列" in bot.handle("/answer %s 可以" % live_id, "u1"),
              bot.handle("/answer %s 可以" % live_id, "u1"))
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

        # ---- out: the files a task produced go to the phone --------------
        produced = os.path.join(ws_path, "notes.txt")
        content = "这是任务写出来的文件\n第二行\n".encode("utf-8")
        with open(produced, "wb") as f:
            f.write(content)
        MockILink.sent = []
        MockILink.upload_tickets = []
        MockCDN.uploads = []
        bot.owner[task_id] = "u1"
        bot.notify_event({"type": "task_done", "task": {
            "id": task_id, "note": "做完了", "result": "答案", "meta": {},
            "artifacts": [{"path": "notes.txt", "bytes": len(content),
                           "kind": "document", "tool": "toolkit_file"}]}})
        file_msgs = [m for m in MockILink.sent
                     if any(i.get("type") == 4 for i in (m["msg"].get("item_list") or []))]
        check("the produced file is sent as a file message", len(file_msgs) == 1,
              MockILink.sent)
        item = {}
        if file_msgs:
            item = [i for i in file_msgs[0]["msg"]["item_list"] if i.get("type") == 4][0]
            info = item.get("file_item") or {}
            media = info.get("media") or {}
            check("the file message carries the name", info.get("file_name") == "notes.txt", info)
            check("the file message carries the size", info.get("len") == str(len(content)), info)
            check("the file message carries a download reference",
                  bool(media.get("encrypt_query_param")), media)
            check("the file message carries the aes key",
                  bool(media.get("aes_key")), media)
        check("the file was uploaded to the CDN", len(MockCDN.uploads) == 1, MockCDN.uploads)
        check("the upload asked for a ticket", len(MockILink.upload_tickets) == 1,
              MockILink.upload_tickets)
        if MockILink.upload_tickets and file_msgs:
            ticket = MockILink.upload_tickets[0]
            check("the ticket names the media type of a file", ticket.get("media_type") == 3, ticket)
            check("the ticket carries the raw size", ticket.get("rawsize") == len(content), ticket)
            check("the ticket carries the md5 of the file",
                  ticket.get("rawfilemd5") == hashlib.md5(content).hexdigest(), ticket)
            check("the ticket carries the encrypted size",
                  ticket.get("filesize") == len(MockCDN.uploads[0]["data"]), ticket)
            key_hex = ticket.get("aeskey") or ""
            check("the ticket carries the key as 32 hex characters",
                  len(key_hex) == 32 and all(c in "0123456789abcdef" for c in key_hex), key_hex)
            plain = wechat_bot.aes_decrypt(MockCDN.uploads[0]["data"],
                                           bytes.fromhex(key_hex))
            check("what the CDN got is the file, encrypted",
                  plain == content, plain[:40])
            check("the key in the message is the one of the ticket",
                  wechat_bot.decode_aes_key(
                      (item.get("file_item") or {}).get("media", {}).get("aes_key", "")
                  ).hex() == key_hex, item)

        # ---- in: a file the user sends arrives in the workspace ---------
        incoming = "用户发来的内容\n".encode("utf-8")
        in_key = os.urandom(16)
        in_cipher = wechat_bot.aes_encrypt(incoming, in_key)
        MockCDN.files["download-inbound"] = in_cipher
        raw = {"message_type": 1, "from_user_id": "u1", "context_token": "ctx-1",
               "create_time_ms": int(time.time() * 1000),
               "item_list": [{"type": 4, "file_item": {
                   "media": {"encrypt_query_param": "download-inbound",
                             "aes_key": base64.b64encode(in_key.hex().encode()).decode()},
                   "file_name": "../../evil.sh", "md5": hashlib.md5(incoming).hexdigest(),
                   "len": str(len(incoming))}}]}
        MockQueue.calls = []
        answer = bot.handle_message(raw, "", wechat_bot.files_of(raw), "u1")
        check("the bot says it got the file", "已收到文件" in answer, answer)
        inbox = os.path.join(ws_path, "inbox")
        saved = os.path.join(inbox, "evil.sh")
        check("the file is saved in the workspace", os.path.isfile(saved), os.listdir(inbox)
              if os.path.isdir(inbox) else inbox)
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

        # ---- in: the other two key formats work too ----------------------
        for label, key_text in (("hex", in_key.hex()),
                                ("base64 of the raw bytes", base64.b64encode(in_key).decode())):
            ref = "download-" + label.replace(" ", "-")
            MockCDN.files[ref] = in_cipher
            raw2 = {"message_type": 1, "from_user_id": "u1", "context_token": "ctx-1",
                    "item_list": [{"type": 4, "file_item": {
                        "media": {"encrypt_query_param": ref, "aes_key": key_text},
                        "file_name": "k-%s.bin" % label.split()[0], "len": str(len(incoming))}}]}
            bot.handle_message(raw2, "", wechat_bot.files_of(raw2), "u1")
            got = os.path.join(inbox, "k-%s.bin" % label.split()[0])
            check("a key given as %s is understood" % label, os.path.isfile(got), got)

        # ---- the loop handles a file message the same way -----------------
        loop_key = os.urandom(16)
        loop_cipher = wechat_bot.aes_encrypt(b"loop file\n", loop_key)
        MockCDN.files["download-loop"] = loop_cipher
        raw_loop = {"message_type": 1, "from_user_id": "u2", "context_token": "ctx-2",
                    "item_list": [{"type": 4, "file_item": {
                        "media": {"encrypt_query_param": "download-loop",
                                  "aes_key": base64.b64encode(loop_key.hex().encode()).decode()},
                        "file_name": "loop.txt", "len": str(len(b"loop file\n"))}}]}
        MockILink.sent = []
        bot.process_message(raw_loop)
        check("a message with only a file is answered",
              any("已收到文件" in (m["msg"].get("item_list") or [{}])[0]
                  .get("text_item", {}).get("text", "") for m in MockILink.sent),
              MockILink.sent)
        check("the file of that message is on disk",
              os.path.isfile(os.path.join(ws_path, "inbox", "loop.txt")),
              os.listdir(os.path.join(ws_path, "inbox")))
        check("the context token of that user is remembered",
              bot.state.context_for("u2") == "ctx-2", bot.state.context_for("u2"))

        # ---- /files sends them again on demand ---------------------------
        MockILink.sent = []
        MockCDN.uploads = []
        # The task that was deleted above is gone; use the one the file made.
        files_task = sorted(MockQueue.tasks)[-1]
        MockQueue.tasks[files_task]["artifacts"] = [
            {"path": "notes.txt", "bytes": len(content), "kind": "document"}]
        reply = bot.handle("/files " + files_task, "u1")
        check("/files sends the produced file again",
              len([m for m in MockILink.sent
                   if any(i.get("type") == 4 for i in (m["msg"].get("item_list") or []))]) == 1,
              (reply, MockILink.sent))
    finally:
        ilink.shutdown()
        queue.shutdown()
        agent.shutdown()
        cdn.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    print("%d/%d checks passed" % (len(checks) - len(failures), len(checks)))
    if failures:
        print("FAILED:", ", ".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
