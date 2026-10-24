#!/usr/bin/env python3
"""The Telegram side of the task queue: talk to it from Telegram.

The agent (tea_agent) is the workspace channel, the task server is the
queue. This bot is the third piece: it turns a Telegram conversation into
queue operations, and it tells the user when their work is done.

  Telegram  <->  this bot  <->  the task server  <->  the agent

The Telegram Bot API (https://core.telegram.org/bots/api) is HTTPS + long
polling, so this needs nothing but the standard library - it runs on the
device next to the agent, where a pip install is not an option.

The commands are the ones the WeChat side has (sched/wechat_bot.py): any
other text is a task to do.

  <any text>        queue it and answer with the task id
  /tasks            the list: queued, running, finished
  /status <id>      one task: state, progress, the latest note
  /result <id>      the final answer and what it produced
  /start <id>       do it now
  /cancel <id>      stop it
  /del <id>         forget it
  /answer <id> <t>  answer a task that waits for the user
  /quota            how much of the subscription is left
  /files <id>       the files the task produced, once more
  /help             this list

A Telegram bot is public: everybody who finds it can write to it. So the bot
answers nobody until it is told who may talk to it (`--allow-chat`).

Usage:

  python3 sched/telegram_bot.py --token 123456:ABC --check
  python3 sched/telegram_bot.py --token 123456:ABC --print-chat-id
  python3 sched/telegram_bot.py --token 123456:ABC --allow-chat 12345
"""

import argparse
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import channel

# The log of every chat front end looks the same.
log = channel.log

DEFAULT_API = "https://api.telegram.org"
DEFAULT_STATE_DIR = os.path.expanduser("~/.tea_agent/telegram")
DEFAULT_WORKSPACE_PATH = os.path.expanduser("~/.tea_agent/telegram-workspace")
# Telegram refuses a text message longer than this, so a long answer goes
# out in pieces.
MAX_MESSAGE_CHARS = 4096
MESSAGE_CHUNK = 3800

# The menu the Telegram client shows when you type "/".
COMMANDS = [
    {"command": "tasks", "description": "任务列表"},
    {"command": "status", "description": "一个任务的状态和进度"},
    {"command": "result", "description": "一个任务的最终回答"},
    {"command": "start", "description": "立刻做这个任务"},
    {"command": "cancel", "description": "停掉这个任务"},
    {"command": "del", "description": "删掉这个任务"},
    {"command": "answer", "description": "回答一个等你确认的任务"},
    {"command": "files", "description": "再要一次这个任务的产出文件"},
    {"command": "retry", "description": "重做一个失败的任务"},
    {"command": "quota", "description": "额度还剩多少"},
    {"command": "help", "description": "这条说明"},
]


def chunks(text, size):
    """The text in pieces of at most `size` characters."""
    text = text or ""
    out = []
    while text:
        out.append(text[:size])
        text = text[size:]
    return out


class State:
    """What has to survive a restart: the update offset.

    The offset tells Telegram which updates we have seen. Without it a
    restart would answer the same message twice.
    """

    def __init__(self, path):
        self.path = os.path.abspath(os.path.expanduser(path))
        self.lock = threading.Lock()
        self.data = {"offset": 0}
        self.load()

    def load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                self.data.update(loaded)
        except (OSError, ValueError):
            pass

    def save(self):
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    @property
    def offset(self):
        with self.lock:
            return int(self.data.get("offset") or 0)

    @offset.setter
    def offset(self, value):
        with self.lock:
            self.data["offset"] = int(value or 0)


def multipart(fields, attachment):
    """A multipart/form-data body: the fields and one file."""
    boundary = "----tea" + os.urandom(12).hex()
    name, filename, data = attachment
    head = []
    for key, value in fields:
        head.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                     % (boundary, key, value)).encode("utf-8"))
    head.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"; filename=\"%s\"\r\n"
                 "Content-Type: application/octet-stream\r\n\r\n"
                 % (boundary, name, filename)).encode("utf-8"))
    tail = ("\r\n--%s--\r\n" % boundary).encode("utf-8")
    return b"".join(head) + data + tail, "multipart/form-data; boundary=%s" % boundary


class Telegram:
    """The Telegram Bot API: long polling, sending, files.

    Only the standard library: the device this runs on has no pip.
    """

    def __init__(self, token, api_base=DEFAULT_API, timeout=90):
        self.token = token or ""
        self.api_base = api_base.rstrip("/")
        self.timeout = timeout

    def url(self, method):
        return "%s/bot%s/%s" % (self.api_base, self.token, method)

    def call(self, method, params=None, attachment=None, timeout=None):
        """One Bot API call. The answer is the "result" it carries."""
        params = params or {}
        if attachment is None:
            # The JSON body keeps the values as they are: a command list is a
            # list, not its text.
            data = json.dumps(params).encode("utf-8")
            headers = {"Content-Type": "application/json"}
        else:
            fields = [(key, str(value)) for key, value in params.items()]
            data, content_type = multipart(fields, attachment)
            headers = {"Content-Type": content_type}
        req = urllib.request.Request(self.url(method), data=data,
                                     headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            raise channel.ChannelError("telegram: HTTP %s: %s" % (exc.code, raw[:200]),
                                       status=exc.code) from None
        except urllib.error.URLError as exc:
            raise channel.ChannelError("cannot reach %s: %s"
                                       % (self.url(method), exc.reason)) from None
        try:
            payload = json.loads(raw) if raw else {}
        except ValueError:
            raise channel.ChannelError("the answer is not JSON: " + raw[:200]) from None
        if not payload.get("ok"):
            raise channel.ChannelError("telegram: %s"
                                       % (payload.get("description") or "failed"),
                                       errcode=int(payload.get("error_code") or 0))
        return payload.get("result")

    # -- the endpoints ----------------------------------------------------
    def get_updates(self, offset=None, timeout=30):
        """The messages that came in since `offset` (the long poll)."""
        params = {"timeout": int(timeout)}
        if offset:
            params["offset"] = int(offset)
        return self.call("getUpdates", params, timeout=timeout + 20) or []

    def send_message(self, chat_id, text):
        return self.call("sendMessage", {"chat_id": chat_id, "text": text})

    def send_document(self, chat_id, path, name=None):
        with open(path, "rb") as f:
            data = f.read()
        filename = channel.safe_file_name(name or os.path.basename(path))
        return self.call("sendDocument", {"chat_id": chat_id},
                         attachment=("document", filename, data))

    def get_file(self, file_id):
        return self.call("getFile", {"file_id": file_id}) or {}

    def download(self, file_path):
        """The bytes behind a file path (the file part of the Bot API)."""
        url = "%s/file/bot%s/%s" % (self.api_base, self.token, file_path)
        req = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            raise channel.ChannelError("telegram: the file download failed (HTTP %s)"
                                       % exc.code, status=exc.code) from None
        except urllib.error.URLError as exc:
            raise channel.ChannelError("cannot reach %s: %s" % (url, exc.reason)) from None

    def get_me(self):
        return self.call("getMe") or {}

    def set_commands(self, commands):
        return self.call("setMyCommands", {"commands": commands})


class TelegramBot(channel.TaskChannel):
    """The bot: poll Telegram, drive the queue, answer the user."""

    META_USER_KEY = "telegram_user_id"

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
            "· /retry <id> 重做一个失败的任务\n"
            "· /files <id> 把这个任务的产出文件发过来\n"
            "· /at <时间> <活儿> 定时做（'+5' 五小时后，'23:30' 今天晚上十点半）\n"
            "· 消息后面加 @23:30 也可以定时\n"
            "· 直接发文件 = 排一个任务，文件会放进工作区\n"
            "· /help 这条说明")

    def __init__(self, args):
        channel.TaskChannel.__init__(self, args)
        self.telegram = Telegram(args.token, api_base=args.api_url)
        self.state = State(os.path.join(args.state_dir, "state.json"))
        self.bot_username = ""
        self.allowed = set(str(chat) for chat in (args.allow_chat or []))
        self.any_chat = bool(getattr(args, "allow_any_chat", False))

    def channel_name(self):
        return "telegram"

    def normalize(self, text):
        """Telegram writes "/tasks@MyBot" when the command comes from a group."""
        text = (text or "").strip()
        if not text.startswith("/"):
            return text
        parts = text.split(" ", 1)
        head = parts[0].split("@")[0]
        if len(parts) == 1:
            return head
        return head + " " + parts[1]

    # -- the transport ----------------------------------------------------
    def send_text(self, user_id, text):
        """One text message to the user (a long one goes out in pieces)."""
        if not text:
            return True
        for part in chunks(text, MESSAGE_CHUNK):
            try:
                self.telegram.send_message(user_id, part)
            except channel.ChannelError as exc:
                log("reply failed: %s" % exc)
                return False
        return True

    def send_file(self, user_id, path, name=None):
        """One file to the user."""
        self.telegram.send_document(user_id, path, name)
        return os.path.getsize(path)

    def allowed_chat(self, chat_id):
        """May this chat talk to the bot?

        A Telegram bot is public: whoever finds it can write to it. So the
        bot answers nobody until it is told who may talk to it.
        """
        if self.any_chat:
            return True
        return chat_id in self.allowed

    # -- the incoming files -----------------------------------------------
    def download_attachment(self, file_id):
        """The bytes of one file the user sent. None if it did not work."""
        if not file_id:
            return None
        try:
            info = self.telegram.get_file(file_id)
        except channel.ChannelError as exc:
            log("cannot ask for the file: %s" % exc)
            return None
        path = info.get("file_path") or ""
        if not path:
            log("the file %s has no path - skipped" % file_id)
            return None
        try:
            data = self.telegram.download(path)
        except channel.ChannelError as exc:
            log("cannot fetch the file: %s" % exc)
            return None
        if len(data) > channel.MAX_FILE_BYTES:
            log("the file is too big (%d bytes) - skipped" % len(data))
            return None
        return data

    def attachments_of(self, message):
        """The files the user sent: [(kind, name, bytes)]."""
        out = []
        photos = message.get("photo") or []
        if photos:
            item = photos[-1]              # the last one is the biggest
            data = self.download_attachment(item.get("file_id"))
            if data:
                out.append((channel.KIND_MEDIA, "image" + channel.image_ext(data), data))
        document = message.get("document") or {}
        if document:
            data = self.download_attachment(document.get("file_id"))
            if data:
                name = document.get("file_name") or "file.bin"
                out.append((channel.KIND_FILE, name, data))
        for key in ("video", "audio", "voice", "animation", "video_note"):
            item = message.get(key) or {}
            if not item:
                continue
            data = self.download_attachment(item.get("file_id"))
            if data:
                name = item.get("file_name") or (key + ".bin")
                out.append((channel.KIND_MEDIA, name, data))
        return out

    # -- the loop ---------------------------------------------------------
    def poll_once(self):
        """One long poll. Returns the updates that came in."""
        updates = self.telegram.get_updates(offset=self.state.offset,
                                            timeout=self.args.poll_timeout)
        newest = self.state.offset
        for update in updates:
            uid = int(update.get("update_id") or 0)
            if uid >= newest:
                newest = uid + 1
        if newest != self.state.offset:
            self.state.offset = newest
            self.state.save()
        return updates

    def process_update(self, update):
        """One update from Telegram: a message (or an edited one)."""
        message = update.get("message") or update.get("edited_message") or {}
        if not message:
            return
        chat = message.get("chat") or {}
        chat_id = str(chat.get("id") or "")
        if not chat_id:
            return
        text = (message.get("text") or message.get("caption") or "").strip()
        has_file = any(message.get(key) for key in
                       ("document", "photo", "video", "audio", "voice",
                        "animation", "video_note"))
        if not text and not has_file:
            return
        if not self.allowed_chat(chat_id):
            log("chat %s is not on the list - ignored (add it with --allow-chat)" % chat_id)
            return
        log("telegram[%s]: %s" % (chat_id, (text or "")[:80] or "（一个文件）"))
        try:
            attachments = self.attachments_of(message)
            answer = self.handle_attachments(text, attachments, chat_id)
        except Exception as exc:  # noqa: BLE001 - a bad message must not kill the bot
            answer = "出错了：%s: %s" % (type(exc).__name__, exc)
        self.send_text(chat_id, answer)

    def run(self):
        if not self.telegram.token:
            log("没有 token：用 --token 或 TEA_TELEGRAM_TOKEN 给我一个")
            return 2
        try:
            me = self.telegram.get_me()
        except channel.ChannelError as exc:
            log("the token does not work: %s" % exc)
            return 2
        self.bot_username = me.get("username") or ""
        log("telegram bot: @%s (%s)"
            % (self.bot_username, me.get("first_name") or ""))
        if self.any_chat:
            log("谁都能用这个 bot（--allow-any-chat）")
        elif self.allowed:
            log("可以用的 chat：%s" % ", ".join(sorted(self.allowed)))
        else:
            log("现在谁都不能用这个 bot：先跑 --print-chat-id 拿到 chat id，"
                "再用 --allow-chat <id> 启动")
        try:
            self.telegram.set_commands(COMMANDS)
        except channel.ChannelError as exc:
            log("the command menu did not go through: %s" % exc)
        self.ensure_workspace()
        threading.Thread(target=self.watch_tasks, daemon=True).start()
        backoff = 1.0
        while True:
            try:
                updates = self.poll_once()
                backoff = 1.0
            except channel.ChannelError as exc:
                log("poll: %s" % exc)
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)
                continue
            for update in updates:
                self.process_update(update)

    # -- the small commands ----------------------------------------------
    def print_chat_ids(self):
        """The chat ids of the messages that are waiting for the bot."""
        try:
            updates = self.telegram.get_updates(offset=None, timeout=0)
        except channel.ChannelError as exc:
            log("cannot read the updates: %s" % exc)
            return 1
        chats = {}
        for update in updates:
            message = update.get("message") or {}
            chat = message.get("chat") or {}
            chat_id = str(chat.get("id") or "")
            if not chat_id:
                continue
            name = (chat.get("title") or chat.get("username")
                    or chat.get("first_name") or "")
            chats[chat_id] = name
        if not chats:
            print("还没有消息。先给 bot 发一句话，再跑一次这个命令。")
            return 0
        for chat_id in sorted(chats):
            print("%s  %s" % (chat_id, chats[chat_id]))
        return 0


def read_token(args):
    """The bot token: the flag, the environment or the file."""
    if args.token:
        return args.token.strip()
    path = args.token_file or os.path.join(args.state_dir, "token")
    try:
        with open(os.path.expanduser(path), encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def build_parser():
    parser = argparse.ArgumentParser(description="The Telegram side of the task queue.")
    parser.add_argument("--token", default=os.environ.get("TEA_TELEGRAM_TOKEN", ""),
                        help="the bot token (or TEA_TELEGRAM_TOKEN)")
    parser.add_argument("--token-file", default="",
                        help="a file with the bot token in it (default: <state-dir>/token)")
    parser.add_argument("--api-url", default=os.environ.get("TEA_TELEGRAM_API", DEFAULT_API),
                        help="the Bot API (the default is the real one)")
    parser.add_argument("--agent", default=os.environ.get("TEA_AGENT_URL", channel.DEFAULT_AGENT),
                        help="the agent's HTTP API (for the workspace list)")
    parser.add_argument("--tasks", default=os.environ.get("TEA_TASK_URL", channel.DEFAULT_TASKS),
                        help="the task server's HTTP API")
    parser.add_argument("--state-dir", default=DEFAULT_STATE_DIR,
                        help="where the token and the update offset live")
    parser.add_argument("--workspace-path", default=DEFAULT_WORKSPACE_PATH,
                        help="the workspace the Telegram tasks work in")
    parser.add_argument("--workspace-name", default="Telegram", help="its name in the agent")
    parser.add_argument("--provider", default=os.environ.get("TEA_TASK_PROVIDER", ""),
                        help="the provider whose quota gates the work (default: the queue's)")
    parser.add_argument("--allow-path", action="append", default=[],
                        help="a directory the Telegram tasks may touch (repeatable)")
    parser.add_argument("--allow-chat", action="append", default=[],
                        help="a chat id that may talk to the bot (repeatable)")
    parser.add_argument("--allow-any-chat", action="store_true",
                        help="answer everybody (the bot is public - think twice)")
    parser.add_argument("--poll-timeout", type=float, default=30.0,
                        help="how long one long poll waits (default: 30 s)")
    parser.add_argument("--print-chat-id", action="store_true",
                        help="print the chat ids of the waiting messages and exit")
    parser.add_argument("--check", action="store_true",
                        help="check the token (who am I) and exit")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.token = read_token(args)

    bot = TelegramBot(args)
    if args.check:
        try:
            me = bot.telegram.get_me()
        except channel.ChannelError as exc:
            log("the token does not work: %s" % exc)
            return 1
        print("@%s  %s" % (me.get("username") or "", me.get("first_name") or ""))
        return 0
    if args.print_chat_id:
        return bot.print_chat_ids()
    try:
        return bot.run()
    except KeyboardInterrupt:
        log("stopped")
        return 0


if __name__ == "__main__":
    sys.exit(main())
