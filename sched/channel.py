#!/usr/bin/env python3
"""What every chat front end of the task queue does.

The queue (sched/task_server.py) is the work; a chat front end is a way to
ask for it. There are two of them:

  WeChat    sched/wechat_bot.py    the iLink protocol (scan a QR code)
  Telegram  sched/telegram_bot.py  the Telegram Bot API (a token)

They differ in the protocol they speak and in nothing else that matters:
the same commands, the same fixed workspace, the same inbox, the same
notifications. That shared half lives here. The transport is a subclass
that implements `send_text` and `send_file` and hands the incoming
messages over.

Standard library only: these scripts run on the device next to the agent,
where nobody installs anything.
"""

import base64
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

import task_order

DEFAULT_AGENT = "http://127.0.0.1:8282"
DEFAULT_TASKS = "http://127.0.0.1:8283"

# A file bigger than this is not worth moving around.
MAX_FILE_BYTES = 32 * 1024 * 1024
# How many files one task may send back.
MAX_FILES_PER_TASK = 5
# The magic bytes of the pictures we know.
IMAGE_MAGIC = (b"\xff\xd8\xff", b"\x89PNG", b"GIF8", b"RIFF", b"BM")

# The two kinds of attachment a chat front end knows: a document (the agent
# reads it) and everything else - a picture, a voice note (the agent looks
# at it).
KIND_FILE = "file"
KIND_MEDIA = "media"


def log(message):
    print("%s  %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), message), flush=True)


class ChannelError(Exception):
    """Something on the way out answered badly: the queue, the agent, the chat."""

    def __init__(self, message, errcode=0, status=0):
        super().__init__(message)
        self.errcode = errcode
        self.status = status


def safe_file_name(name):
    """A file name we can put on disk: no directories, no surprises."""
    base = os.path.basename((name or "").replace("\\", "/")).strip()
    base = re.sub(r"[^0-9A-Za-z._\-\u4e00-\u9fff ]", "_", base)
    base = base.lstrip(".") or "file.bin"
    if len(base) > 80:
        stem, dot, ext = base.rpartition(".")
        base = (stem[:60] + dot + ext[:15]) if dot else base[:80]
    return base


def looks_like_image(data):
    """Is this a picture we can look at?"""
    return bool(data) and any(data.startswith(magic) for magic in IMAGE_MAGIC)


def image_ext(data):
    """The file extension that goes with the picture we got."""
    if data.startswith(b"\x89PNG"):
        return ".png"
    if data.startswith(b"GIF8"):
        return ".gif"
    if data.startswith(b"RIFF"):
        return ".webp"
    if data.startswith(b"BM"):
        return ".bmp"
    return ".jpg"


def idempotency_key(user_id, text):
    """A key that says "this is the same work".

    The queue keeps one task per key, so a command that is repeated (or a
    request that is sent again because the answer was lost) does not queue
    the work twice. The key is stable for the same text within a short window.
    """
    stamp = int(time.time() // 300)      # five minutes
    raw = ("%s|%s|%s" % (user_id, text.strip(), stamp)).encode("utf-8")
    return base64.b32encode(hashlib.sha256(raw).digest()[:8]).decode("ascii").rstrip("=")


# A message may end with "@<time>" to say when the work may start: "把 README
# 跑一遍 @23:30" is the task "把 README 跑一遍", not before 23:30.
_SCHEDULE_TAIL = re.compile(r"\s*@(\+\d+[hmd]?|\d{1,2}:\d{2})\s*$")


def split_schedule(text):
    """('把 README 跑一遍 @23:30') -> ('把 README 跑一遍', '23:30')."""
    match = _SCHEDULE_TAIL.search(text or "")
    if not match:
        return (text or "").strip(), ""
    return (text or "")[:match.start()].strip(), match.group(1)


def is_task_order(name):
    """Does this file say it is a task order?

    A task order is a text file with the work in it ("*.order", or a name that
    says 任务单). Anything else the user sends is an ordinary attachment.
    """
    base = os.path.basename(name or "").lower()
    return base.endswith(".order") or "任务单" in base


def rel_path(path, root):
    """The path relative to the workspace root, with slashes.

    These paths go into the task's prompt and into its record, so they look
    the same on every platform (a Windows inbox is "inbox/file.txt", not
    "inbox\\file.txt").
    """
    return os.path.relpath(path, root).replace(os.sep, "/")


def fmt_task(task):
    """One line about a task."""
    status = task.get("status", "?")
    line = "%s  %s  %d%%  %s" % (task.get("id", "?"), status, int(task.get("progress") or 0),
                                 task.get("title") or (task.get("prompt") or "")[:30])
    position = int(task.get("queue_position") or 0)
    if position:
        line += "  [队列第 %d 位]" % position
    when = task.get("starts_at_local") or ""
    if when:
        line += "  [%s 之后才能开工]" % when
    note = task.get("note") or ""
    if note:
        line += "  — " + note[:60]
    if task.get("needs_confirmation"):
        line += "  [等你回答：" + (task.get("question") or "") + "]"
    return line


class Queue:
    """The task server's API - the queue the chat drives.

    A chat front end works in ONE project (its own workspace), so it carries
    that project's key: the queue then hands it nothing but its own tasks,
    even if the machine has other projects in the queue.
    """

    def __init__(self, base=DEFAULT_TASKS, timeout=30):
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.key = ""

    def _request(self, method, path, body=None, timeout=None, headers=None):
        url = self.base + path
        data = None
        head = {}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            head["Content-Type"] = "application/json"
        if self.key:
            head["Authorization"] = "Bearer " + self.key
        head.update(headers or {})
        req = urllib.request.Request(url, data=data, headers=head, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            message = raw
            try:
                message = (json.loads(raw).get("error") or {}).get("message") or raw
            except ValueError:
                pass
            raise ChannelError("task server: HTTP %s: %s" % (exc.code, message[:200]),
                               status=exc.code) from None
        except urllib.error.URLError as exc:
            raise ChannelError("cannot reach the task server %s: %s" % (url, exc.reason)) from None
        try:
            body = json.loads(raw) if raw else {}
        except ValueError:
            return {}
        # The queue answers in one shape: {code, message, request_id, data}.
        if isinstance(body, dict) and "code" in body and "data" in body:
            if body.get("code"):
                detail = ((body.get("data") or {}).get("error") or {}).get("message") \
                    or body.get("message") or "failed"
                raise ChannelError("task server: %s" % detail)
            return body.get("data") or {}
        return body

    def health(self):
        return self._request("GET", "/api/health", timeout=10)

    def project_key(self, workspace_id):
        """The key of our own project (the local operator may ask for one)."""
        try:
            answer = self._request("POST", "/api/projects", {"workspace_id": workspace_id})
        except ChannelError as exc:
            log("no key for the workspace: %s" % exc)
            return ""
        key = str(answer.get("key") or "")
        if key:
            self.key = key
        return key

    def create(self, prompt, workspace_id=None, title="", provider="", allow_paths=None,
               meta=None, key="", not_before="", model="", priority=0, extra=None):
        body = {"prompt": prompt}
        if workspace_id:
            body["workspace_id"] = workspace_id
        if title:
            body["title"] = title
        if provider:
            body["provider"] = provider
        if allow_paths:
            body["allow_paths"] = list(allow_paths)
        if meta:
            body["meta"] = meta
        if not_before:
            # "not before": the queue may not start the task before this time
            # ("+5" = five hours from now, "23:30" = today at 23:30).
            body["not_before"] = not_before
        if model:
            body["model"] = model
        if priority:
            body["priority"] = priority
        for key_name, value in (extra or {}).items():
            if value not in (None, "", [], {}):
                body[key_name] = value
        # The same key means the same task: a command that is repeated (or a
        # request that times out and is sent again) must not queue the work
        # twice.
        headers = {"Idempotency-Key": key} if key else None
        return self._request("POST", "/api/tasks", body, headers=headers)

    def list(self, workspace_id=None, status=None):
        query = {}
        if workspace_id:
            query["workspace_id"] = workspace_id
        if status:
            query["status"] = status
        path = "/api/tasks" + ("?" + urllib.parse.urlencode(query) if query else "")
        return self._request("GET", path)

    def get(self, task_id, workspace_id=None):
        query = {"workspace_id": workspace_id} if workspace_id else {}
        path = "/api/tasks/%s" % urllib.parse.quote(task_id)
        if query:
            path += "?" + urllib.parse.urlencode(query)
        return self._request("GET", path)

    def delete(self, task_id, workspace_id=None):
        query = {"workspace_id": workspace_id} if workspace_id else {}
        path = "/api/tasks/%s" % urllib.parse.quote(task_id)
        if query:
            path += "?" + urllib.parse.urlencode(query)
        return self._request("DELETE", path)

    def retry(self, task_id, workspace_id=None):
        """Try a failed task again."""
        return self.action(task_id, "retry", workspace_id=workspace_id)

    def action(self, task_id, action, body=None, workspace_id=None):
        query = {"workspace_id": workspace_id} if workspace_id else {}
        path = "/api/tasks/%s/%s" % (urllib.parse.quote(task_id), action)
        if query:
            path += "?" + urllib.parse.urlencode(query)
        return self._request("POST", path, body or {})

    def events(self, workspace_id=None, timeout=3600):
        """The queue's event stream (SSE), as the parsed frames.

        The key goes with the request: without it the stream answers as the
        local operator and hands the bot every project's events - the bot
        works in one project and must not hear the others.
        """
        query = {"workspace_id": workspace_id} if workspace_id else {}
        url = self.base + "/api/events" + ("?" + urllib.parse.urlencode(query) if query else "")
        head = {}
        if self.key:
            head["Authorization"] = "Bearer " + self.key
        req = urllib.request.Request(url, headers=head, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            for raw_line in resp:
                line = raw_line.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                try:
                    yield json.loads(line[5:].strip())
                except ValueError:
                    continue


class Agent:
    """The agent, just enough of it: the workspace list (and the registry)."""

    def __init__(self, base=DEFAULT_AGENT, timeout=15):
        self.base = base.rstrip("/")
        self.timeout = timeout

    def _request(self, method, path, body=None):
        url = self.base + path
        data = None
        headers = {}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            raise ChannelError("agent: HTTP %s" % exc.code, status=exc.code) from None
        except urllib.error.URLError as exc:
            raise ChannelError("cannot reach the agent %s: %s" % (url, exc.reason)) from None
        try:
            return json.loads(raw) if raw else {}
        except ValueError:
            return {}

    def workspaces(self):
        return self._request("GET", "/api/workspaces").get("workspaces") or []

    def register_workspace(self, path, name=""):
        return self._request("POST", "/api/workspaces", {"path": path, "name": name})


def resolve_workspace(agent, path, name=""):
    """The id of the workspace at `path` - registering it if the agent does not
    know it yet. A chat front end always works in the same one directory."""
    path = os.path.abspath(os.path.expanduser(path))
    os.makedirs(path, exist_ok=True)
    for ws in agent.workspaces():
        if os.path.abspath(ws.get("path") or "") == path:
            return ws.get("id", "")
    created = agent.register_workspace(path, name)
    return created.get("id", "")


class TaskChannel:
    """The queue, seen from a chat.

    A subclass is the transport: it says how a text goes out (`send_text`)
    and how a file goes out (`send_file`), and hands the incoming messages
    over. Everything else - the commands, the workspace, the inbox, the
    notifications - is the same for every chat.
    """

    # The key the user's id is stored under in the task's meta.
    META_USER_KEY = "user_id"
    HELP = ("用法：\n"
            "· 任何一句话 = 排一个任务（额度够就自动开工）\n"
            "· /tasks 任务列表\n"
            "· /status <id> 看进度\n"
            "· /result <id> 看结果\n"
            "· /start <id> 立刻做\n"
            "· /cancel <id> 停掉\n"
            "· /del <id> 删掉\n"
            "· /answer <id> <内容> 回答一个等你确认的任务\n"
            "· /quota 额度还剩多少\n"
            "· /help 这条说明")

    def __init__(self, args):
        self.args = args
        self.queue = Queue(args.tasks)
        self.agent = Agent(args.agent)
        self.workspace_id = ""
        self.owner = {}          # task id -> the user who queued it

    # -- the transport ----------------------------------------------------
    def send_text(self, user_id, text):
        """One text message to the user. The transport decides how."""
        raise NotImplementedError

    def send_file(self, user_id, path, name=None):
        """One file to the user. The transport decides how."""
        raise NotImplementedError

    def channel_name(self):
        """The name of the chat, for the log."""
        return "chat"

    def normalize(self, text):
        """The text as the channel understands it (the transport may rewrite it)."""
        return text or ""

    # -- the workspace ----------------------------------------------------
    def ensure_workspace(self):
        if not self.workspace_id:
            self.workspace_id = resolve_workspace(self.agent, self.args.workspace_path,
                                                  self.args.workspace_name)
            log("%s workspace: %s (%s)"
                % (self.channel_name(), self.args.workspace_path, self.workspace_id))
            # The key of our own project: from now on the queue only tells us
            # about our own tasks.
            if not self.queue.key:
                self.queue.project_key(self.workspace_id)
        return self.workspace_id

    # -- the inbox --------------------------------------------------------
    def inbox_dir(self):
        """Where the files the user sends us land.

        Inside the workspace on purpose: the agent works there, so a task can
        just read the file the user sent.
        """
        path = os.path.join(os.path.abspath(os.path.expanduser(self.args.workspace_path)), "inbox")
        os.makedirs(path, exist_ok=True)
        return path

    def inbox_path(self, name):
        """A free path in the inbox for that name (no overwriting)."""
        path = os.path.join(self.inbox_dir(), name)
        stem, dot, ext = name.rpartition(".")
        attempt = 1
        while os.path.exists(path):
            path = os.path.join(self.inbox_dir(),
                                ("%s-%d%s" % (stem, attempt, dot + ext)) if dot else name)
            attempt += 1
        return path

    # -- the task order ---------------------------------------------------
    def queue_order(self, path, user_id):
        """A task order the user sent: queue what it says."""
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except OSError as exc:
            return "任务单读不了：%s" % exc
        except UnicodeDecodeError:
            return "任务单不是文本文件，读不了。"
        try:
            body = task_order.parse_order(text)
        except task_order.OrderError as exc:
            return "任务单有问题：\n%s" % exc
        if not body.get("prompt"):
            return "任务单里没有 prompt（要做什么），我不懂该干什么。"
        workspace_id = self.ensure_workspace()
        note = ""
        wanted = str(body.pop("workspace_id", "") or "")
        if wanted:
            found = ""
            for ws in self.agent.workspaces() or []:
                if ws.get("id") == wanted or \
                        os.path.abspath(ws.get("path") or "") == os.path.abspath(os.path.expanduser(wanted)):
                    found = ws.get("id", "")
                    break
            if found:
                workspace_id = found
            else:
                note = "（工作区 %s 不在 agent 里，用了%s自己的工作区）" % (wanted, self.channel_name())
        try:
            task = self.queue.create(
                body["prompt"], workspace_id=workspace_id,
                title=body.get("title") or "", provider=body.get("provider") or "",
                allow_paths=body.get("allow_paths") or None,
                meta=body.get("meta") or None,
                key=body.get("idempotency_key") or "",
                not_before=body.get("not_before") or "",
                model=body.get("model") or "", priority=int(body.get("priority") or 0),
                extra={"tags": body.get("tags")} if body.get("tags") else None)
        except ChannelError as exc:
            return "排队失败：%s" % exc
        task_id = task.get("id", "?")
        self.owner[task_id] = user_id
        when = task.get("starts_at_local") or ""
        if when:
            return "任务单收到，已排队 %s（%s 之后开工）%s" % (task_id, when, note)
        return "任务单收到，已排队 %s%s" % (task_id, note)

    # -- the files --------------------------------------------------------
    def handle_attachments(self, text, attachments, user_id):
        """A message that carries files (or pictures) as well as text.

        `attachments` are (kind, name, bytes) triples: the transport has
        already fetched them, this is where they land on disk and become a
        task.
        """
        if not attachments:
            return self.handle(text, user_id)
        saved = []
        for kind, name, data in attachments:
            if not data:
                continue
            if len(data) > MAX_FILE_BYTES:
                log("the file is too big (%d bytes) - skipped" % len(data))
                continue
            path = self.inbox_path(safe_file_name(name or "file.bin"))
            with open(path, "wb") as f:
                f.write(data)
            log("received %s: %s (%d bytes)" % (kind, os.path.basename(path), len(data)))
            saved.append((kind, path))
        if not saved:
            return "文件没能取下来，再发一次？"
        # A task order is not an attachment: it says what to do and how.
        for kind, path in saved:
            if kind == KIND_FILE and is_task_order(path):
                return self.queue_order(path, user_id)
        root = os.path.abspath(os.path.expanduser(self.args.workspace_path))
        rel_files = [rel_path(p, root) for kind, p in saved if kind == KIND_FILE]
        rel_media = [rel_path(p, root) for kind, p in saved if kind != KIND_FILE]
        parts = []
        if rel_files:
            parts.append("文件：%s" % "、".join(rel_files))
        if rel_media:
            parts.append("图片：%s" % "、".join(rel_media))
        listed = "；".join(parts)
        note = "用户发来了这些文件，已经放在工作区里：%s。" % listed
        if rel_media:
            note += ("图片用 toolkit_load_image 看（路径相对于工作区根目录），"
                     "看完里面的内容再办事。")
        else:
            note += "请看一下它们是什么，再按里面的内容办事。"
        prompt = ("%s\n\n（%s）" % (text, note)) if text else note
        try:
            task = self.queue.create(
                prompt, workspace_id=self.ensure_workspace(),
                title=(text or (rel_files + rel_media)[0])[:40],
                provider=self.args.provider or None,
                allow_paths=self.args.allow_path or None,
                meta={self.META_USER_KEY: user_id, "attachments": rel_files + rel_media})
        except ChannelError as exc:
            return "排队失败：%s" % exc
        task_id = task.get("id", "?")
        self.owner[task_id] = user_id
        return ("已收到文件 %s，已排队 %s\n「%s」"
                % (listed, task_id, (text or "看看这些文件")[:60]))

    def send_task_files(self, user_id, task):
        """The files a finished task produced, as file messages."""
        sent = 0
        root = os.path.abspath(os.path.expanduser(self.args.workspace_path))
        for art in (task.get("artifacts") or [])[:MAX_FILES_PER_TASK]:
            rel = str(art.get("path") or "")
            if not rel:
                continue
            path = os.path.join(root, rel)
            if not os.path.isfile(path):
                continue
            try:
                self.send_file(user_id, path, os.path.basename(path))
                sent += 1
            except ChannelError as exc:
                log("cannot send %s: %s" % (rel, exc))
        return sent

    # -- the command mapping ----------------------------------------------
    def handle(self, text, user_id):
        """What a message means. Returns the reply."""
        text = self.normalize((text or "").strip())
        if not text:
            return "（没看懂：发一句话就是排一个任务，/help 有全部命令）"
        if text in ("/help", "help", "帮助"):
            return self.HELP
        if text.startswith("/"):
            return self.command(text, user_id)
        # Anything else: a task to do. The key is derived from the user and the
        # text, so the same command twice in a row does not queue it twice.
        text, not_before = split_schedule(text)
        if not text:
            return "（没看懂：发一句话就是排一个任务，/help 有全部命令）"
        try:
            task = self.queue.create(
                text, workspace_id=self.ensure_workspace(), title=text[:40],
                provider=self.args.provider or None,
                allow_paths=self.args.allow_path or None,
                meta={self.META_USER_KEY: user_id},
                key=idempotency_key(user_id, text),
                not_before=not_before)
        except ChannelError as exc:
            return "排队失败：%s" % exc
        task_id = task.get("id", "?")
        self.owner[task_id] = user_id
        when = task.get("starts_at_local") or ""
        if when:
            return ("已排队 %s\n「%s」\n%s 之后才能开工，额度有余量就自动做。"
                    % (task_id, text[:60], when))
        return ("已排队 %s\n「%s」\n额度有余量就自动开工，做完我告诉你。"
                % (task_id, text[:60]))

    def command(self, text, user_id):
        parts = text.split()
        cmd = parts[0].lower()
        rest = parts[1:]
        try:
            if cmd in ("/tasks", "/list", "任务"):
                listing = self.queue.list(workspace_id=self.ensure_workspace())
                tasks = listing.get("tasks") or []
                if not tasks:
                    return "队列是空的。"
                lines = [fmt_task(t) for t in tasks[-12:]]
                return "任务（%d）：\n%s" % (len(tasks), "\n".join(lines))
            if cmd in ("/status", "/状态"):
                if not rest:
                    return "用法：/status <任务id>"
                task = self.queue.get(rest[0], workspace_id=self.ensure_workspace())
                return fmt_task(task)
            if cmd in ("/result", "/结果"):
                if not rest:
                    return "用法：/result <任务id>"
                task = self.queue.get(rest[0], workspace_id=self.ensure_workspace())
                out = [fmt_task(task)]
                result = (task.get("result") or "").strip()
                if result:
                    out.append("最终回答：\n" + result[:1500])
                arts = task.get("artifacts") or []
                if arts:
                    out.append("产出：\n" + "\n".join("· " + str(a.get("path")) for a in arts[:10]))
                if task.get("needs_confirmation"):
                    out.append("等你回答：" + (task.get("question") or ""))
                return "\n".join(out)
            if cmd in ("/files", "/文件"):
                if not rest:
                    return "用法：/files <任务id>"
                task = self.queue.get(rest[0], workspace_id=self.ensure_workspace())
                try:
                    count = self.send_task_files(user_id, task)
                except ChannelError as exc:
                    return "文件发不出去：%s" % exc
                if not count:
                    return "这个任务没有可发的文件。"
                return "发了 %d 个文件。" % count
            if cmd in ("/start", "/开始"):
                if not rest:
                    return "用法：/start <任务id>"
                task = self.queue.action(rest[0], "start", workspace_id=self.ensure_workspace())
                return "开始 %s：%s" % (rest[0], task.get("status", "?"))
            if cmd in ("/cancel", "/取消"):
                if not rest:
                    return "用法：/cancel <任务id>"
                task = self.queue.action(rest[0], "cancel", workspace_id=self.ensure_workspace())
                return "已停掉 %s：%s" % (rest[0], task.get("status", "?"))
            if cmd in ("/del", "/delete", "/删除"):
                if not rest:
                    return "用法：/del <任务id>"
                self.queue.delete(rest[0], workspace_id=self.ensure_workspace())
                return "删掉了 %s" % rest[0]
            if cmd in ("/answer", "/回答"):
                if len(rest) < 2:
                    return "用法：/answer <任务id> <你要回答的内容>"
                task = self.queue.action(rest[0], "confirm", {"message": " ".join(rest[1:])},
                                        workspace_id=self.ensure_workspace())
                # The answer goes back into the queue: it does not interrupt
                # whatever is running now.
                return ("已回答 %s：回到队列里了（%s），前面的活儿做完它接着做。"
                        % (rest[0], task.get("status", "?")))
            if cmd in ("/at", "/定时", "/晚点"):
                # /at 23:30 把 README 跑一遍
                parts = text.split(None, 2)
                if len(parts) < 3:
                    return ("用法：/at <时间> <要做的活儿>\n"
                            "时间可以是 '+5'（五小时后）或 '23:30'（今天晚上十点半，"
                            "已经过了就尽快做）")
                when, what = parts[1], parts[2]
                try:
                    task = self.queue.create(
                        what, workspace_id=self.ensure_workspace(), title=what[:40],
                        provider=self.args.provider or None,
                        allow_paths=self.args.allow_path or None,
                        meta={self.META_USER_KEY: user_id},
                        key=idempotency_key(user_id, what + "@" + when),
                        not_before=when)
                except ChannelError as exc:
                    return "排队失败：%s" % exc
                task_id = task.get("id", "?")
                self.owner[task_id] = user_id
                started = task.get("starts_at_local") or when
                return ("已排队 %s\n「%s」\n%s 之后才能开工。" % (task_id, what[:60], started))
            if cmd in ("/retry", "/重试"):
                if not rest:
                    return "用法：/retry <任务id>"
                task = self.queue.retry(rest[0], workspace_id=self.ensure_workspace())
                return ("重试 %s：%s（第 %s 次）"
                        % (rest[0], task.get("status", "?"), task.get("attempt") or 1))
            if cmd in ("/quota", "/额度"):
                health = self.queue.health()
                left = health.get("quota_percent")
                return ("额度还剩 %s%%（provider %s）" % (left, health.get("provider") or "?")) \
                    if left is not None else "额度查不到（provider 没声明额度端点）"
        except ChannelError as exc:
            return "出了点问题：%s" % exc
        return "不认识的命令：%s\n%s" % (cmd, self.HELP)

    # -- the notifications ------------------------------------------------
    def notify_event(self, event):
        """Tell the user about one task event. Returns True when a message
        went out."""
        kind = event.get("type", "")
        if kind not in ("task_done", "task_failed", "task_waiting_confirmation"):
            return False
        task = event.get("task") or {}
        task_id = task.get("id", "")
        user_id = self.owner.get(task_id) or (task.get("meta") or {}).get(self.META_USER_KEY)
        if not user_id:
            return False
        if kind == "task_done":
            text = "做完了 %s\n%s" % (task_id, (task.get("note") or
                                            (task.get("result") or "")[:300]))
            sent = self.send_text(user_id, text)
            # The files the task produced go out with the answer: a result the
            # user cannot open on their phone is only half an answer.
            try:
                count = self.send_task_files(user_id, task)
            except ChannelError as exc:
                log("cannot send the files: %s" % exc)
                count = 0
            if count:
                log("sent %d file(s) to %s" % (count, user_id))
            return sent
        elif kind == "task_failed":
            text = "%s 失败了：%s" % (task_id, task.get("error") or "未知原因")
        else:
            text = ("%s 在等你回答：%s\n（/answer %s 你的回答）"
                    % (task_id, task.get("question") or "", task_id))
        return self.send_text(user_id, text)

    def watch_tasks(self):
        """Tell the user when their work is done."""
        while True:
            try:
                for event in self.queue.events(workspace_id=self.ensure_workspace()):
                    self.notify_event(event)
            except Exception as exc:  # noqa: BLE001 - the watcher may not die
                log("task watcher: %s" % exc)
                time.sleep(5)
