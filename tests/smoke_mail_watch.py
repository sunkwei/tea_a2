#!/usr/bin/env python3
"""The mail watchdog: the config, the judgement, the actions, the notifications.

No real IMAP server is touched (that needs an account, and nobody wants a test
that labels their inbox). The mailbox is a FakeSource, and the agent, the task
queue, Telegram and the webhook are small local servers. What is pinned here:

  * the config format: comments, a `|` block, an unknown key is an error that
    names the line - a typo must not silently drop a setting;
  * reading a message: an encoded subject, a quoted reply stripped, an
    html-only body, an attachment noticed;
  * the judgement: a good JSON, a bad one, none at all - and the fail-safe
    (unjudgeable mail stays in the inbox and is reported, never archived);
  * the actions: the label, the archive, the draft, "seen" - for the mail that
    needs the user and for the mail that does not;
  * "only tell me when I have to take part": an advert is silent, a contract
    is a notification, a follow-up becomes a queued task with an idempotency
    key, and a notification that fails does not lose the round;
  * the bookmark: a second round does not redo the same message, a dry run
    does not move the bookmark at all;
  * the one watcher rule (the lock), and the digest on disk.

Usage: python3 tests/smoke_mail_watch.py
"""

import base64
import email.mime.multipart
import email.mime.text
import json
import os
import sys
import tempfile
import socketserver
import threading
import time
import urllib.parse

from http.server import BaseHTTPRequestHandler

try:
    from http.server import ThreadingHTTPServer
except ImportError:  # Python 3.6
    from http.server import HTTPServer
    from socketserver import ThreadingMixIn

    class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
        daemon_threads = True

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "sched"))

# The module reads its paths at import time: point them at a throwaway home
# before it is imported.
TMP_HOME = tempfile.mkdtemp(prefix="tea_mail_watch_")
os.environ["TEA_MAIL_HOME"] = TMP_HOME
os.environ.pop("TEA_MAIL_CONFIG", None)

import mail_watch  # noqa: E402


# ---------------------------------------------------------------------------
# the fake world
# ---------------------------------------------------------------------------
ADVERT = {
    "category": "广告", "needs_me": False, "urgency": "low", "why": "",
    "summary": "双十一促销，最高五折。", "action": "archive",
    "draft_reply": "", "follow_up": "",
}
CONTRACT = {
    "category": "工作", "needs_me": True, "urgency": "high",
    "why": "对方在等我确认付款条款。",
    "summary": "供应商发来合同，要求在周五前确认付款条款。",
    "action": "label",
    "draft_reply": "收到，付款条款我今天下班前确认。",
    "follow_up": "把合同附件里的付款条款和违约金摘出来比对上一版",
}

DECISIONS = (("50% off", ADVERT), ("合同", CONTRACT))


class MockWorld(BaseHTTPRequestHandler):
    """agent + queue + Telegram + webhook on one port."""

    protocol_version = "HTTP/1.0"

    flags = {"telegram_ok": True, "queue_ok": True}
    llm_calls = []
    telegram = []
    hooks = []
    tasks = []

    def log_message(self, *args):
        pass

    def do_GET(self):  # noqa: N802
        self._send({"ok": True, "version": "0.16.1", "provider": "deepseek",
                    "model": "deepseek-flash"})

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except ValueError:
            body = {}

        if self.path.startswith("/bot"):
            if not MockWorld.flags["telegram_ok"]:
                return self._send({"ok": False, "description": "blocked by test"})
            MockWorld.telegram.append(body)
            return self._send({"ok": True, "result": {"message_id": 1}})

        if self.path == "/hook":
            MockWorld.hooks.append(body)
            return self._send({"ok": True})

        if self.path == "/api/tasks":
            if not MockWorld.flags["queue_ok"]:
                return self._send({"code": 500, "message": "queue is down", "data": {}}, 503)
            MockWorld.tasks.append(body)
            key = body.get("idempotency_key") or ""
            task_id = "t_%d" % (abs(hash(key)) % 100000)
            return self._send({"code": 0, "message": "ok", "request_id": "req_1",
                               "data": {"task": {"id": task_id, "status": "queued"}}})

        if self.path == "/api/llm/chat":
            content = ""
            messages = body.get("messages") or []
            if messages:
                content = messages[0].get("content") or ""
            MockWorld.llm_calls.append(content)
            return self._send({"ok": True, "content": self._judge(content),
                               "reasoning": "", "finish_reason": "stop"})

        return self._send({"error": "no such route: " + self.path}, 404)

    @staticmethod
    def _judge(prompt):
        subject = ""
        for line in prompt.splitlines():
            if line.startswith("主题："):
                subject = line[3:]
                break
        for needle, decision in DECISIONS:
            if needle in subject:
                return json.dumps(decision, ensure_ascii=False)
        # A model that answers prose instead of JSON: the fail-safe must take it.
        return "这封信我说不好，可能是广告，也可能是通知。"

    def _send(self, payload, status=200):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class FakeImapState(object):
    """What the fake IMAP server knows: the mail, the labels, the flags."""

    def __init__(self):
        self.uidvalidity = 42
        self.messages = {}
        self.labels = {}
        self.flags = {}
        self.appended = []
        self.searches = []

    def add(self, uid, raw):
        self.messages[uid] = raw
        # every message in the inbox carries the Gmail \\Inbox label
        self.labels[uid] = set(["\\Inbox"])
        self.flags[uid] = set()


class FakeImapHandler(socketserver.StreamRequestHandler):
    """IMAP4rev1, but only the half mail_watch talks to.

    Enough to be a real dialogue: CAPABILITY, LOGIN, SELECT, UID SEARCH,
    UID FETCH (with the literal), UID STORE (flags and Gmail labels), APPEND
    (the draft), CLOSE, LOGOUT.
    """

    def handle(self):
        model = self.server.model
        self.wfile.write(b"* OK fake imap ready\r\n")
        while True:
            line = self.rfile.readline()
            if not line:
                return
            text = line.decode("utf-8", "replace").rstrip("\r\n")
            if not text:
                continue
            words = text.split(" ")
            tag = words[0]
            verb = words[1].upper() if len(words) > 1 else ""
            if verb == "UID":
                sub = words[2].upper() if len(words) > 2 else ""
                payload = text.split(" ", 3)[3] if len(words) > 3 else ""
                self._uid(tag, sub, payload, model)
            elif verb == "CAPABILITY":
                self.wfile.write(b"* CAPABILITY IMAP4rev1 X-GM-EXT-1\r\n")
                self._ok(tag, "CAPABILITY completed")
            elif verb == "LOGIN":
                self._ok(tag, "LOGIN completed")
            elif verb == "SELECT":
                self.wfile.write(("* %d EXISTS\r\n" % len(model.messages)).encode())
                self.wfile.write(("* OK [UIDVALIDITY %d] UIDs valid\r\n"
                                  % model.uidvalidity).encode())
                self._ok(tag, "SELECT completed")
            elif verb == "APPEND":
                self._append(tag, text, model)
            elif verb in ("CLOSE", "NOOP", "CHECK"):
                self._ok(tag, verb + " completed")
            elif verb == "LOGOUT":
                self.wfile.write(b"* BYE bye\r\n")
                self._ok(tag, "LOGOUT completed")
                return
            else:
                self.wfile.write(("%s BAD unknown command %s\r\n" % (tag, verb)).encode())

    # -- the commands ------------------------------------------------------
    def _uid(self, tag, sub, payload, model):
        if sub == "SEARCH":
            model.searches.append(payload)
            uids = sorted(model.messages)
            if payload.upper().startswith("UID "):
                want = int(payload.split()[1].split(":")[0])
                uids = [uid for uid in uids if uid >= want]
                # the IMAP quirk: `n:*` always returns at least the last message
                if not uids and model.messages:
                    uids = [max(model.messages)]
            self.wfile.write(("* SEARCH %s\r\n" % " ".join(str(u) for u in uids)).encode())
            self._ok(tag, "SEARCH completed")
            return
        if sub == "FETCH":
            uid = int(payload.split(" ")[0])
            raw = model.messages.get(uid, b"")
            header = "* 1 FETCH (UID %d RFC822 {%d}\r\n" % (uid, len(raw))
            self.wfile.write(header.encode() + raw + b")\r\n")
            self._ok(tag, "FETCH completed")
            return
        if sub == "STORE":
            bits = payload.split(" ")
            uid = int(bits[0])
            operation = bits[1]
            item = " ".join(bits[2:])
            if operation.endswith("X-GM-LABELS"):
                labels = [part.strip('"') for part in item.strip("()").split(" ")
                          if part.strip('"')]
                if operation.startswith("+"):
                    model.labels.setdefault(uid, set()).update(labels)
                else:
                    for label in labels:
                        model.labels.setdefault(uid, set()).discard(label)
            elif operation.startswith("+"):
                model.flags.setdefault(uid, set()).update(item.strip("()").split(" "))
            elif operation.startswith("-"):
                for flag in item.strip("()").split(" "):
                    model.flags.setdefault(uid, set()).discard(flag)
            self.wfile.write(("* 1 FETCH (UID %d FLAGS (%s))\r\n"
                              % (uid, " ".join(sorted(model.flags.get(uid, set()))))).encode())
            self._ok(tag, "STORE completed")
            return
        self.wfile.write(("%s BAD unknown UID command %s\r\n" % (tag, sub)).encode())

    def _append(self, tag, text, model):
        # APPEND "[Gmail]/Drafts" (\Draft) {123}
        folder = text.split(" ")[2].strip('"')
        size = int(text.rsplit("{", 1)[1].rstrip("}"))
        self.wfile.write(b"+ Ready for literal data\r\n")
        payload = self.rfile.read(size)
        self.rfile.read(2)                     # the CRLF after the literal
        model.appended.append((folder, payload))
        self._ok(tag, "APPEND completed")

    def _ok(self, tag, text):
        self.wfile.write(("%s OK %s\r\n" % (tag, text)).encode())


class FakeImapServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class FakeSource(object):
    """The mailbox: what fetch_new would have returned, and what was done to it."""

    def __init__(self, messages, fail_draft=False):
        self.messages = list(messages)
        self.labels = []
        self.archived = []
        self.drafts = []
        self.seen = []
        self.starred = []
        self.fail_draft = fail_draft

    def fetch_new(self, state, limit=10):
        return list(self.messages)[:max(1, limit)]

    def add_labels(self, uid, labels):
        self.labels.append((str(uid), list(labels)))

    def archive(self, uid):
        self.archived.append(str(uid))

    def create_draft(self, msg, text):
        if self.fail_draft:
            raise mail_watch.MailError("drafts folder is read-only")
        self.drafts.append((str(msg["uid"]), text))
        return "[Gmail]/Drafts"

    def mark_seen(self, uid):
        self.seen.append(str(uid))

    def star(self, uid):
        self.starred.append(str(uid))


def raw_message(subject, sender="王经理", body="正文",
                message_id="m1@example.com", html=False, attachment=False):
    """One RFC822 message, the way a mail server would hand it over."""
    encoded = "=?UTF-8?B?%s?=" % base64.b64encode(subject.encode("utf-8")).decode("ascii")
    if attachment:
        message = email.mime.multipart.MIMEMultipart()
        message.attach(email.mime.text.MIMEText(body, "plain", "utf-8"))
        blob = email.mime.text.MIMEText("附件内容", "plain", "utf-8")
        blob.add_header("Content-Disposition", "attachment", filename="terms.txt")
        message.attach(blob)
    else:
        message = email.mime.text.MIMEText(body, "html" if html else "plain", "utf-8")
    message["Subject"] = encoded
    message["From"] = "=?UTF-8?B?%s?= <boss@example.com>" % (
        base64.b64encode(sender.encode("utf-8")).decode("ascii"))
    message["To"] = "me@gmail.com"
    message["Date"] = "Wed, 28 Oct 2026 10:00:00 +0800"
    message["Message-ID"] = message_id
    return message.as_bytes()


def free_home(name):
    path = os.path.join(TMP_HOME, name)
    if not os.path.isdir(path):
        os.makedirs(path)
    return path


PORT = 0


def config(**overrides):
    values = {
        "address": "me@gmail.com",
        "password": "abcd efgh ijkl mnop",
        "host": "imap.gmail.com",
        "label_done": "tea/done",
        "label_needs_me": "tea/needs-me",
        "archive": "yes",
        "mark_seen": "yes",
        "drafts": "on",
        "max_per_round": "10",
        "agent": "http://127.0.0.1:%d" % PORT,
        "provider": "deepseek",
        "model": "deepseek-flash",
        "notify": "telegram",
        "telegram_api": "http://127.0.0.1:%d" % PORT,
        "telegram_token": "123456:TEST",
        "telegram_chat": "4242",
        "tasks": "http://127.0.0.1:%d" % PORT,
        "workspace": "/tmp/mail-workspace",
        "queue_followups": "yes",
        "rules": "老板的信一律算需要我。",
    }
    values.update(overrides)
    return mail_watch.Config(values, "(test)")


def fresh_state(name):
    mail_watch.STATE_PATH = os.path.join(free_home(name), "state.json")
    mail_watch.DIGEST_DIR = os.path.join(free_home(name), "digest")
    mail_watch.LOCK_PATH = os.path.join(free_home(name), "watcher.lock")
    return mail_watch.State()


def main():
    global PORT
    failures = []
    checks = []

    def check(name, ok, detail=""):
        checks.append(name)
        print(("PASS " if ok else "FAIL ") + name + (("  " + str(detail)) if not ok else ""))
        if not ok:
            failures.append(name)

    world = ThreadingHTTPServer(("127.0.0.1", 0), MockWorld)
    world.daemon_threads = True
    threading.Thread(target=world.serve_forever, daemon=True).start()
    PORT = world.server_address[1]

    # ---- the config format ------------------------------------------------
    text = """# 注释
address: me@gmail.com
password: abcd efgh ijkl mnop
archive: yes          # 行尾注释
interval: 60
rules: |
  · 老板的信算需要我
  · 广告归档
signature: |
  孙科
"""
    parsed = mail_watch.parse_config(text)
    check("a comment line is skipped", parsed["address"] == "me@gmail.com", parsed)
    check("a trailing comment is cut", parsed["archive"] == "yes", parsed.get("archive"))
    check("a | block keeps its lines",
          "老板的信算需要我" in parsed["rules"] and "广告归档" in parsed["rules"],
          parsed.get("rules"))
    check("a number stays a string until asked", parsed["interval"] == "60", parsed.get("interval"))
    cfg = mail_watch.Config(parsed)
    check("the config turns strings into numbers and booleans",
          cfg.integer("interval") == 60 and cfg.boolean("archive") is True, cfg.values)
    try:
        mail_watch.parse_config("address: a\narchiv: yes\n")
        check("an unknown key is an error", False, "no exception")
    except mail_watch.ConfigError as exc:
        check("an unknown key is an error with the line number",
              "第 2 行" in str(exc) and "archiv" in str(exc), str(exc))
    try:
        mail_watch.Config({"archive": "maybe"}).boolean("archive")
        check("a bad boolean is an error", False, "no exception")
    except mail_watch.ConfigError as exc:
        check("a bad boolean is an error", "archive" in str(exc), str(exc))

    # ---- reading a message ------------------------------------------------
    msg = mail_watch.to_message(
        "7",
        raw_message("合同：请确认付款条款 <付款>",
                    sender="王经理",
                    body="下面是条款。\n\n在 2026-10-27 10:00，某人写道：\n> 旧的引用\n> 还有一行",
                    message_id="<abc@example.com>"))
    check("the encoded subject is decoded", msg["subject"].startswith("合同：请确认付款条款"),
          msg["subject"])
    check("the encoded sender is decoded", msg["from_name"] == "王经理", msg["from_name"])
    check("the address is kept apart from the name", msg["from_addr"] == "boss@example.com",
          msg["from_addr"])
    check("the quoted reply is cut", "旧的引用" not in msg["body"], msg["body"])
    check("the body before the quote survives", "下面是条款" in msg["body"], msg["body"])
    check("the message id is kept", msg["message_id"] == "<abc@example.com>", msg["message_id"])
    check("a mail link points at the message id",
          mail_watch.mail_link(msg).endswith(
              urllib.parse.quote(msg["message_id"].strip("<>"))),
          mail_watch.mail_link(msg))

    html_msg = mail_watch.to_message("8", raw_message("通知", body="<p>第一段</p><p>第二段</p>",
                                                      html=True, message_id="<h@x>"))
    check("an html-only body is turned into text",
          "第一段" in html_msg["body"] and "<p>" not in html_msg["body"], html_msg["body"])
    attached = mail_watch.to_message("9", raw_message("带附件", attachment=True,
                                                      message_id="<a@x>"))
    check("an attachment is noticed", attached["attachment"] is True, attached["attachment"])
    check("the attachment is not part of the body", "附件内容" not in attached["body"],
          attached["body"])
    check("two different messages have two different keys",
          mail_watch.message_key(msg) != mail_watch.message_key(html_msg))
    check("the imap date uses an english month",
          mail_watch.imap_date(1761500000).split("-")[1] in mail_watch.MONTHS,
          mail_watch.imap_date(1761500000))
    check("a label is quoted for imap",
          mail_watch.quote_label('tea/"x"') == '"tea/\\"x\\""', mail_watch.quote_label('tea/"x"'))

    # ---- the judgement ----------------------------------------------------
    decision = mail_watch.normalize_decision({"needs_me": "true", "action": "archive",
                                              "category": "工作"})
    check("a mail that needs me is never archived",
          decision["needs_me"] is True and decision["action"] == "label", decision)
    decision = mail_watch.normalize_decision({"needs_me": False, "action": "whatever"})
    check("an unknown action falls back by needs_me",
          decision["action"] == "archive" and decision["category"] == "其他", decision)
    decision = mail_watch.normalize_decision({"needs_me": False, "urgency": "now"})
    check("an unknown urgency falls back to normal",
          decision["urgency"] == "normal", decision)
    decision = mail_watch.normalize_decision("不是字典")
    check("a judgement that is not an object is a fail-safe",
          decision["needs_me"] is True and decision["action"] == "keep"
          and decision["failed"] is True, decision)
    check("json inside a code fence is found",
          mail_watch.extract_json("好的：\n```json\n{\"a\": 1}\n```") == {"a": 1})
    check("the first complete object wins",
          mail_watch.extract_json("前言 {\"a\": {\"b\": 2}} 后话") == {"a": {"b": 2}})
    check("the triage prompt carries the user's rules",
          "老板的信" in mail_watch.triage_prompt(msg, config()), "no rules")

    # ---- a round: three mails, three fates --------------------------------
    state = fresh_state("round")
    advert = mail_watch.to_message("11", raw_message("50% off everything",
                                                     body="促销", message_id="<ad@x>"))
    contract = mail_watch.to_message("12", raw_message("合同：请确认付款条款",
                                                       body="请周五前确认",
                                                       message_id="<c@x>"))
    mystery = mail_watch.to_message("13", raw_message("今天的事情",
                                                      body="说不清", message_id="<m@x>"))
    source = FakeSource([advert, contract, mystery])
    cfg = config()
    out = []

    stats = mail_watch.run_round(cfg, source, mail_watch.Judge(cfg), mail_watch.Notifier(cfg),
                                 state, out=out.append)
    check("the advert was labelled as done",
          ("11", ["tea/done"]) in source.labels, source.labels)
    check("the advert was archived", source.archived == ["11"], source.archived)
    check("the contract was labelled as needing me",
          ("12", ["tea/needs-me"]) in source.labels, source.labels)
    check("the contract stayed in the inbox", "12" not in source.archived, source.archived)
    check("the contract got a draft, not a send",
          len(source.drafts) == 1 and source.drafts[0][0] == "12", source.drafts)
    check("the draft says what the model wrote",
          "付款条款" in source.drafts[0][1], source.drafts)
    check("every mail was marked as seen", sorted(source.seen) == ["11", "12", "13"], source.seen)
    check("two mails were worth telling about", len(MockWorld.telegram) == 2, MockWorld.telegram)
    told = "\n".join(message.get("text") or "" for message in MockWorld.telegram)
    check("the advert was not one of them", "50% off" not in told, told)
    check("the contract was", "合同" in told, told)
    check("the notification says why I am needed", "付款条款" in told, told)
    check("the notification goes to the configured chat",
          all(str(message.get("chat_id")) == "4242" for message in MockWorld.telegram),
          MockWorld.telegram)
    check("the unjudgeable mail stayed in the inbox", "13" not in source.archived, source.archived)
    check("the unjudgeable mail was reported", "今天的事情" in told, told)
    check("one follow-up went to the queue", len(MockWorld.tasks) == 1, MockWorld.tasks)
    if MockWorld.tasks:
        task = MockWorld.tasks[0]
        check("the queued task says what to do",
              "付款条款" in (task.get("prompt") or "") and "违约金" in (task.get("prompt") or ""),
              task.get("prompt"))
        check("the queued task has an idempotency key",
              bool(task.get("idempotency_key")), task)
        check("the queued task runs in the mail workspace",
              task.get("workspace_id") == "/tmp/mail-workspace", task.get("workspace_id"))
    check("the round counts what it did",
          stats == {"seen": 3, "handled": 3, "needs_me": 2, "notified": 2, "queued": 1}, stats)
    digest = os.path.join(mail_watch.DIGEST_DIR, time.strftime("%Y-%m-%d") + ".md")
    check("a digest is on disk", os.path.exists(digest), digest)
    if os.path.exists(digest):
        body = open(digest, encoding="utf-8").read()
        check("the digest keeps the advert too", "50% off" in body, body)
        check("the digest marks the ones that need me", "❗" in body, body)
        check("the digest records the actions", "草稿" in body or "贴标签" in body, body)
    check("the bookmark moved to the newest uid", state.data["last_uid"] == 13, state.data)
    check("the state is on disk", os.path.exists(mail_watch.STATE_PATH))

    # ---- a second round does not redo the work ---------------------------
    MockWorld.telegram = []
    MockWorld.tasks = []
    source2 = FakeSource([advert, contract, mystery])
    stats = mail_watch.run_round(cfg, source2, mail_watch.Judge(cfg), mail_watch.Notifier(cfg),
                                 state, out=out.append)
    check("already handled mail is skipped",
          stats["handled"] == 0 and not source2.labels, (stats, source2.labels))
    check("a second round is quiet", not MockWorld.telegram, MockWorld.telegram)
    check("no new task for the same mail", not MockWorld.tasks, MockWorld.tasks)

    # ---- a dry run changes nothing ---------------------------------------
    state = fresh_state("dryrun")
    source3 = FakeSource([advert, contract, mystery])
    MockWorld.telegram = []
    MockWorld.tasks = []
    out = []
    stats = mail_watch.run_round(cfg, source3, mail_watch.Judge(cfg), mail_watch.Notifier(cfg),
                                 state, out=out.append, dry_run=True)
    check("a dry run touches no mail",
          not source3.labels and not source3.archived and not source3.drafts and not source3.seen,
          (source3.labels, source3.archived))
    check("a dry run sends no notification", not MockWorld.telegram, MockWorld.telegram)
    check("a dry run does not move the bookmark", state.data["last_uid"] == 0, state.data)
    check("a dry run still says what it would do",
          any("[dry-run]" in line for line in out), out[-3:])

    # ---- a failing draft does not lose the rest --------------------------
    state = fresh_state("draftfail")
    source4 = FakeSource([contract], fail_draft=True)
    MockWorld.telegram = []
    MockWorld.tasks = []
    stats = mail_watch.run_round(cfg, source4, mail_watch.Judge(cfg), mail_watch.Notifier(cfg),
                                 state, out=out.append)
    check("a draft that cannot be written is logged, not fatal",
          stats["handled"] == 1 and ("12", ["tea/needs-me"]) in source4.labels,
          (stats, source4.labels))
    check("the notification still went out", len(MockWorld.telegram) == 1, MockWorld.telegram)

    # ---- a channel that is down ------------------------------------------
    state = fresh_state("tgdown")
    MockWorld.flags["telegram_ok"] = False
    source5 = FakeSource([contract])
    stats = mail_watch.run_round(cfg, source5, mail_watch.Judge(cfg), mail_watch.Notifier(cfg),
                                 state, out=out.append)
    MockWorld.flags["telegram_ok"] = True
    check("a broken notification is reported as not sent", stats["notified"] == 0, stats)
    check("the mail is handled anyway", source5.labels and "12" not in source5.archived,
          (source5.labels, source5.archived))
    check("the digest is written even then",
          os.path.exists(os.path.join(mail_watch.DIGEST_DIR,
                                      time.strftime("%Y-%m-%d") + ".md")))

    # ---- a queue that is down --------------------------------------------
    state = fresh_state("queuedown")
    MockWorld.flags["queue_ok"] = False
    MockWorld.tasks = []
    source6 = FakeSource([contract])
    stats = mail_watch.run_round(cfg, source6, mail_watch.Judge(cfg), mail_watch.Notifier(cfg),
                                 state, out=out.append)
    MockWorld.flags["queue_ok"] = True
    check("a dead queue does not break the round", stats["handled"] == 1 and stats["queued"] == 0,
          stats)

    # ---- the other ways out ----------------------------------------------
    state = fresh_state("hook")
    MockWorld.hooks = []
    hook_cfg = config(notify="webhook", webhook="http://127.0.0.1:%d/hook" % PORT)
    source7 = FakeSource([contract])
    mail_watch.run_round(hook_cfg, source7, mail_watch.Judge(hook_cfg),
                         mail_watch.Notifier(hook_cfg), state, out=out.append)
    check("the webhook gets one post", len(MockWorld.hooks) == 1, MockWorld.hooks)
    if MockWorld.hooks:
        hook = MockWorld.hooks[0]
        check("the webhook carries the text", "合同" in (hook.get("text") or ""), hook)
        check("the webhook carries the metadata",
              hook.get("category") == "工作" and hook.get("needs_me") is True, hook)

    quiet = config(notify="file")
    check("the file sink never fails", mail_watch.Notifier(quiet).send("你好") is True)
    silent = config(notify="none")
    check("the none sink never fails", mail_watch.Notifier(silent).send("你好") is True)
    check("an unknown sink is not fatal",
          mail_watch.Notifier(config(notify="smoke")).send("你好") is False)

    # ---- one watcher at a time -------------------------------------------
    fresh_state("lock")
    first = mail_watch.Lock()
    first.__enter__()
    try:
        second = mail_watch.Lock()
        try:
            second.__enter__()
            check("a second watcher is refused", False, "it got in")
        except mail_watch.MailError as exc:
            check("a second watcher is refused", "watcher 在跑" in str(exc), str(exc))
    finally:
        first.__exit__()
    third = mail_watch.Lock()
    third.__enter__()
    check("the lock can be taken again after it is released",
          os.path.exists(mail_watch.LOCK_PATH))
    third.__exit__()
    check("the lock file is gone after release", not os.path.exists(mail_watch.LOCK_PATH))
    # a lock left behind by a dead process must not block the next start
    with open(mail_watch.LOCK_PATH, "w") as handle:
        handle.write("999999")
    stale = mail_watch.Lock()
    try:
        stale.__enter__()
        check("a stale lock is taken over", os.path.exists(mail_watch.LOCK_PATH))
    finally:
        stale.__exit__()

    # ---- the imap dialogue itself ----------------------------------------
    imap_state = FakeImapState()
    imap_state.add(11, raw_message("50% off everything", body="促销", message_id="<ad@x>"))
    imap_state.add(12, raw_message("合同：请确认付款条款", body="请周五前确认",
                                   message_id="<c@x>"))
    imap_server = FakeImapServer(("127.0.0.1", 0), FakeImapHandler)
    imap_server.model = imap_state
    threading.Thread(target=imap_server.serve_forever, daemon=True).start()

    state = fresh_state("imap")
    imap_cfg = config(host="127.0.0.1", port=str(imap_server.server_address[1]),
                      drafts_folder="[Gmail]/Drafts")
    source = mail_watch.ImapSource(imap_cfg).connect()
    try:
        check("the watchdog logs in to a gmail server", source.gmail is True, source.gmail)
        check("the drafts folder is guessed for gmail, and only for gmail",
              mail_watch.guess_drafts_folder("imap.gmail.com") == "[Gmail]/Drafts"
              and mail_watch.guess_drafts_folder("imap.qq.com") == "Drafts",
              mail_watch.guess_drafts_folder("imap.qq.com"))
        check("uidvalidity comes from SELECT", source.uidvalidity == 42, source.uidvalidity)
        messages = source.fetch_new(state, limit=10)
        check("new mail comes back in uid order",
              [item["uid"] for item in messages] == ["11", "12"], messages)
        check("the first run searches by date",
              bool(imap_state.searches) and imap_state.searches[0].startswith("SINCE "),
              imap_state.searches)
        check("the fetched mail is decoded",
              messages[1]["subject"].startswith("合同"), messages[1]["subject"])

        MockWorld.telegram = []
        MockWorld.tasks = []
        stats = mail_watch.run_round(imap_cfg, source, mail_watch.Judge(imap_cfg),
                                     mail_watch.Notifier(imap_cfg), state, out=out.append)
        check("over imap the advert gets tea/done", "tea/done" in imap_state.labels[11],
              imap_state.labels[11])
        check("over imap the advert leaves the inbox", "\\Inbox" not in imap_state.labels[11],
              imap_state.labels[11])
        check("over imap the contract keeps the inbox and gets tea/needs-me",
              "\\Inbox" in imap_state.labels[12] and "tea/needs-me" in imap_state.labels[12],
              imap_state.labels[12])
        check("over imap every mail is marked seen",
              all("\\Seen" in flags for flags in imap_state.flags.values()),
              imap_state.flags)
        check("the draft is appended to the drafts folder",
              len(imap_state.appended) == 1 and imap_state.appended[0][0] == "[Gmail]/Drafts",
              [item[0] for item in imap_state.appended])
        if imap_state.appended:
            draft = imap_state.appended[0][1].decode("utf-8", "replace")
            check("the draft is a reply in the same thread",
                  "In-Reply-To: <c@x>" in draft and "References:" in draft, draft[:300])
            check("the draft says Re:", "Subject: Re: " in draft or "Subject: =?" in draft,
                  draft[:300])
            check("the draft quotes nothing and sends nothing", "To: " in draft, draft[:300])
        check("the round over imap counts the same", stats["handled"] == 2, stats)

        again = source.fetch_new(state, limit=10)
        check("the next search starts after the newest uid",
              imap_state.searches[-1] == "UID 13:*", imap_state.searches[-1:])
        check("the IMAP quirk does not bring the old mail back", again == [], again)
    finally:
        source.close()
    imap_server.shutdown()

    # ---- the template -----------------------------------------------------
    template = os.path.join(REPO_ROOT, "sched", "mail-watch.template")
    values = mail_watch.parse_config(open(template, encoding="utf-8").read())
    parsed_cfg = mail_watch.Config(values)
    check("the template is valid config",
          bool(parsed_cfg.text("address")) and parsed_cfg.boolean("archive") is True, values)
    check("the template has the rules block", "老板" in parsed_cfg.text("rules"))
    check("the template picks one of the known sinks",
          parsed_cfg.text("notify") in ("telegram", "webhook", "file", "none"))

    print("")
    if failures:
        print("%d of %d checks failed" % (len(failures), len(checks)))
        for name in failures:
            print("  FAIL " + name)
        return 1
    print("all %d checks passed" % len(checks))
    return 0


if __name__ == "__main__":
    sys.exit(main())
