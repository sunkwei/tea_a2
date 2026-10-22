#!/usr/bin/env python3
"""The WeChat side of the task queue: talk to it from WeChat.

The agent (tea_agent) is the workspace channel, the task server is the queue.
This bot is the third piece: it turns a WeChat conversation into queue
operations, and it tells the user when their work is done.

  WeChat  <->  this bot  <->  the task server  <->  the agent

The iLink protocol (https://www.wechatbot.dev/zh/protocol) is HTTP + long
polling, so this needs nothing but the standard library - it runs on the device
next to the agent, where a pip install is not an option.

The commands (any other text is a task to do):

  <any text>        queue it and answer with the task id
  /tasks            the list: queued, running, finished
  /status <id>      one task: state, progress, the latest note
  /result <id>      the final answer and what it produced
  /start <id>       do it now
  /cancel <id>      stop it
  /del <id>         forget it
  /answer <id> <t>  answer a task that waits for the user
  /quota            how much of the subscription is left
  /help             this list

Usage:

  python3 sched/wechat_bot.py --login          # scan the QR code
  python3 sched/wechat_bot.py                  # run it
"""

import argparse
import base64
import json
import os
import re
import struct
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com"
DEFAULT_AGENT = "http://127.0.0.1:8282"
DEFAULT_TASKS = "http://127.0.0.1:8283"
DEFAULT_STATE_DIR = os.path.expanduser("~/.tea_agent/wechat")
DEFAULT_WORKSPACE_PATH = os.path.expanduser("~/.tea_agent/wechat-workspace")
CHANNEL_VERSION = "2.0.0"
# The protocol wants a client version; the SDK encodes it as 0x00MMNNPP.
CLIENT_VERSION = "0.10.0"

# The protocol's enums (iLink Bot API).
MSG_TYPE_USER = 1
MSG_TYPE_BOT = 2
MSG_STATE_FINISH = 2
ITEM_TEXT = 1


def log(message):
    print("%s  %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), message), flush=True)


class ILinkError(Exception):
    """The iLink server answered, but not with what we asked for."""

    def __init__(self, message, errcode=0, status=0):
        super().__init__(message)
        self.errcode = errcode
        self.status = status


class ILink:
    """The raw iLink protocol: login, long polling, sending.

    Only the standard library: the device this runs on has no pip.
    """

    def __init__(self, base_url=DEFAULT_BASE_URL, client_version=CLIENT_VERSION):
        self.base_url = base_url.rstrip("/")
        parts = (client_version or "0.0.0").split(".")
        try:
            major, minor, patch = (int(p) & 0xFF for p in (parts + ["0", "0"])[:3])
        except ValueError:
            major = minor = patch = 0
        self.client_version = str((major << 16) | (minor << 8) | patch)

    # -- plumbing ---------------------------------------------------------
    def _common_headers(self):
        return {"iLink-App-Id": "bot", "iLink-App-ClientVersion": self.client_version}

    def _auth_headers(self, token):
        # X-WECHAT-UIN: base64 of the decimal string of a random uint32.
        uin = base64.b64encode(str(struct.unpack(">I", os.urandom(4))[0]).encode()).decode()
        headers = self._common_headers()
        headers.update({
            "Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "Authorization": "Bearer " + (token or ""),
            "X-WECHAT-UIN": uin,
        })
        return headers

    def _base_info(self):
        return {"channel_version": CHANNEL_VERSION, "bot_agent": "tea_a2/wechat-bot/" + CLIENT_VERSION}

    def _request(self, method, path, body=None, token=None, timeout=45, query=None):
        url = self.base_url + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = None
        headers = self._auth_headers(token) if token else self._common_headers()
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            raise ILinkError("HTTP %s: %s" % (exc.code, raw[:200]), status=exc.code) from None
        except urllib.error.URLError as exc:
            raise ILinkError("cannot reach %s: %s" % (url, exc.reason)) from None
        try:
            payload = json.loads(raw) if raw else {}
        except ValueError:
            raise ILinkError("the answer is not JSON: " + raw[:200]) from None
        ret = payload.get("ret")
        errcode = payload.get("errcode")
        if (isinstance(ret, int) and ret != 0) or (isinstance(errcode, int) and errcode != 0):
            code = errcode if isinstance(errcode, int) and errcode != 0 else (ret or 0)
            raise ILinkError(payload.get("errmsg") or ("failed (ret=%s errcode=%s)" % (ret, errcode)),
                             errcode=code)
        return payload

    def _post(self, path, body, token, timeout=45):
        return self._request("POST", path, body, token, timeout)

    # -- the endpoints ----------------------------------------------------
    def get_qrcode(self, local_tokens=None):
        body = {"local_token_list": list(local_tokens or [])}
        return self._request("POST", "/ilink/bot/get_bot_qrcode?bot_type=3", body, None, 20)

    def qr_status(self, qrcode, verify_code=""):
        query = {"qrcode": qrcode}
        if verify_code:
            query["verify_code"] = verify_code
        return self._request("GET", "/ilink/bot/get_qrcode_status", None, None, 20, query)

    def updates(self, cursor, token, timeout=45):
        body = {"get_updates_buf": cursor or "", "base_info": self._base_info()}
        return self._post("/ilink/bot/getupdates", body, token, timeout)

    def send_text(self, user_id, context_token, text, token):
        msg = {
            "from_user_id": "",
            "to_user_id": user_id,
            "client_id": "tea-a2-" + base64.b32encode(os.urandom(8)).decode().strip("="),
            "message_type": MSG_TYPE_BOT,
            "message_state": MSG_STATE_FINISH,
            "context_token": context_token,
            "item_list": [{"type": ITEM_TEXT, "text_item": {"text": text}}],
        }
        return self._post("/ilink/bot/sendmessage", {"msg": msg, "base_info": self._base_info()}, token)

    def typing_ticket(self, user_id, context_token, token):
        body = {"ilink_user_id": user_id, "context_token": context_token, "base_info": self._base_info()}
        return self._post("/ilink/bot/getconfig", body, token)

    def send_typing(self, user_id, ticket, status, token):
        body = {"ilink_user_id": user_id, "typing_ticket": ticket, "status": status,
                "base_info": self._base_info()}
        return self._post("/ilink/bot/sendtyping", body, token)


class State:
    """The credentials and the per-user context tokens, on disk.

    The context_token is the one thing that must survive a restart: without it
    a reply cannot be routed back to the right WeChat conversation.
    """

    def __init__(self, path):
        self.path = os.path.abspath(os.path.expanduser(path))
        self.lock = threading.Lock()
        self.data = {"token": "", "base_url": "", "ilink_bot_id": "", "ilink_user_id": "",
                     "cursor": "", "contexts": {}}
        self.load()

    def load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                self.data.update(loaded)
                self.data.setdefault("contexts", {})
        except (OSError, ValueError):
            pass

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def remember_context(self, user_id, context_token):
        with self.lock:
            self.data.setdefault("contexts", {})[user_id] = context_token
            self.save()

    def context_for(self, user_id):
        with self.lock:
            return (self.data.get("contexts") or {}).get(user_id, "")

    @property
    def token(self):
        return self.data.get("token", "")

    @property
    def base_url(self):
        return self.data.get("base_url") or DEFAULT_BASE_URL

    @property
    def cursor(self):
        return self.data.get("cursor", "")


class Queue:
    """The task server's API - the queue the bot drives."""

    def __init__(self, base=DEFAULT_TASKS, timeout=30):
        self.base = base.rstrip("/")
        self.timeout = timeout

    def _request(self, method, path, body=None, timeout=None):
        url = self.base + path
        data = None
        headers = {}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
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
            raise ILinkError("task server: HTTP %s: %s" % (exc.code, message[:200]), status=exc.code) from None
        except urllib.error.URLError as exc:
            raise ILinkError("cannot reach the task server %s: %s" % (url, exc.reason)) from None
        try:
            return json.loads(raw) if raw else {}
        except ValueError:
            return {}

    def health(self):
        return self._request("GET", "/api/health", timeout=10)

    def create(self, prompt, workspace_id=None, title="", provider="", allow_paths=None, meta=None):
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
        return self._request("POST", "/api/tasks", body)

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

    def action(self, task_id, action, body=None, workspace_id=None):
        query = {"workspace_id": workspace_id} if workspace_id else {}
        path = "/api/tasks/%s/%s" % (urllib.parse.quote(task_id), action)
        if query:
            path += "?" + urllib.parse.urlencode(query)
        return self._request("POST", path, body or {})

    def events(self, workspace_id=None, timeout=3600):
        """The queue's event stream (SSE), as (event, data) pairs."""
        query = {"workspace_id": workspace_id} if workspace_id else {}
        url = self.base + "/api/events" + ("?" + urllib.parse.urlencode(query) if query else "")
        req = urllib.request.Request(url, method="GET")
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
            raise ILinkError("agent: HTTP %s" % exc.code, status=exc.code) from None
        except urllib.error.URLError as exc:
            raise ILinkError("cannot reach the agent %s: %s" % (url, exc.reason)) from None
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
    know it yet. The WeChat side always works in the same one directory."""
    path = os.path.abspath(os.path.expanduser(path))
    os.makedirs(path, exist_ok=True)
    for ws in agent.workspaces():
        if os.path.abspath(ws.get("path") or "") == path:
            return ws.get("id", "")
    created = agent.register_workspace(path, name)
    return created.get("id", "")


def text_of(raw):
    """The text of an incoming message."""
    parts = []
    for item in raw.get("item_list") or []:
        if item.get("type") == ITEM_TEXT:
            parts.append((item.get("text_item") or {}).get("text", ""))
    return "\n".join(p for p in parts if p)


def fmt_task(task):
    """One line about a task."""
    status = task.get("status", "?")
    line = "%s  %s  %d%%  %s" % (task.get("id", "?"), status, int(task.get("progress") or 0),
                                 task.get("title") or (task.get("prompt") or "")[:30])
    note = task.get("note") or ""
    if note:
        line += "  — " + note[:60]
    if task.get("needs_confirmation"):
        line += "  [等你回答：" + (task.get("question") or "") + "]"
    return line


class WeChatBot:
    """The bot: poll WeChat, drive the queue, answer the user."""

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
        self.state = State(os.path.join(args.state_dir, "credentials.json"))
        self.ilink = ILink(args.base_url)
        self.queue = Queue(args.tasks)
        self.agent = Agent(args.agent)
        self.workspace_id = ""
        self.owner = {}          # task id -> the WeChat user who queued it

    # -- the workspace ----------------------------------------------------
    def ensure_workspace(self):
        if not self.workspace_id:
            self.workspace_id = resolve_workspace(self.agent, self.args.workspace_path,
                                                  self.args.workspace_name)
            log("wechat workspace: %s (%s)" % (self.args.workspace_path, self.workspace_id))
        return self.workspace_id

    # -- login ------------------------------------------------------------
    def login(self, verify_code=""):
        """Show a QR code and wait for the scan."""
        qr = self.ilink.get_qrcode()
        code = qr.get("qrcode") or qr.get("qrcode_id") or ""
        image = qr.get("qrcode_img_content") or qr.get("qrcode_url") or ""
        print("")
        print("用微信扫这个码登录：")
        print("  " + (image or code))
        if image and image.startswith("http"):
            print("  （浏览器打开上面的链接，手机微信扫它）")
        print("")
        deadline = time.time() + self.args.login_timeout
        while time.time() < deadline:
            time.sleep(2)
            try:
                status = self.ilink.qr_status(code, verify_code)
            except ILinkError as exc:
                log("qr: %s" % exc)
                continue
            state = status.get("status", "")
            if state == "wait":
                continue
            if state == "scaned":
                log("已扫码，等你确认…")
                continue
            if state == "expired":
                log("二维码过期，重新来一次")
                return False
            if state == "confirmed":
                self.state.data.update({
                    "token": status.get("bot_token", ""),
                    "base_url": status.get("baseurl") or self.args.base_url,
                    "ilink_bot_id": status.get("ilink_bot_id", ""),
                    "ilink_user_id": status.get("ilink_user_id", ""),
                })
                self.state.save()
                log("登录成功，凭证已存到 %s" % self.state.path)
                return True
        log("登录超时")
        return False

    # -- the command mapping ----------------------------------------------
    def handle(self, text, user_id):
        """What a message means. Returns the reply."""
        text = (text or "").strip()
        if not text:
            return "（没看懂：发一句话就是排一个任务，/help 有全部命令）"
        if text in ("/help", "help", "帮助"):
            return self.HELP
        if text.startswith("/"):
            return self.command(text, user_id)
        # Anything else: a task to do.
        try:
            task = self.queue.create(
                text, workspace_id=self.ensure_workspace(), title=text[:40],
                provider=self.args.provider or None,
                allow_paths=self.args.allow_path or None,
                meta={"wechat_user_id": user_id})
        except ILinkError as exc:
            return "排队失败：%s" % exc
        task_id = task.get("id", "?")
        self.owner[task_id] = user_id
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
                return "已回答 %s：%s" % (rest[0], task.get("status", "?"))
            if cmd in ("/quota", "/额度"):
                health = self.queue.health()
                left = health.get("quota_percent")
                return ("额度还剩 %s%%（provider %s）" % (left, health.get("provider") or "?")) \
                    if left is not None else "额度查不到（provider 没声明额度端点）"
        except ILinkError as exc:
            return "出了点问题：%s" % exc
        return "不认识的命令：%s\n%s" % (cmd, self.HELP)

    # -- the loop ----------------------------------------------------------
    def reply(self, user_id, text):
        context = self.state.context_for(user_id)
        if not context:
            log("no context token for %s - cannot reply" % user_id)
            return False
        try:
            self.ilink.send_text(user_id, context, text, self.state.token)
            return True
        except ILinkError as exc:
            log("reply failed: %s" % exc)
            return False

    def poll_once(self):
        """One long poll. Returns the messages that came in."""
        payload = self.ilink.updates(self.state.cursor, self.state.token)
        cursor = payload.get("get_updates_buf")
        if cursor and cursor != self.state.cursor:
            self.state.data["cursor"] = cursor
            self.state.save()
        return payload.get("msgs") or []

    def run(self):
        if not self.state.token:
            log("还没有凭证，先跑 --login 扫码")
            return 2
        self.ensure_workspace()
        threading.Thread(target=self.watch_tasks, daemon=True).start()
        backoff = 1.0
        while True:
            try:
                messages = self.poll_once()
                backoff = 1.0
            except ILinkError as exc:
                if exc.errcode == -14:
                    log("会话过期了，需要重新扫码登录（--login）")
                    self.state.data["token"] = ""
                    self.state.save()
                    return 3
                log("poll: %s" % exc)
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)
                continue
            for raw in messages:
                if raw.get("message_type") != MSG_TYPE_USER:
                    continue
                user_id = raw.get("from_user_id", "")
                context = raw.get("context_token", "")
                if user_id and context:
                    self.state.remember_context(user_id, context)
                text = text_of(raw)
                if not text:
                    continue
                log("wechat[%s]: %s" % (user_id, text[:80]))
                try:
                    answer = self.handle(text, user_id)
                except Exception as exc:  # noqa: BLE001 - a bad message must not kill the bot
                    answer = "出错了：%s: %s" % (type(exc).__name__, exc)
                self.reply(user_id, answer)

    def notify_event(self, event):
        """Tell the user about one task event. Returns True when a message
        went out."""
        kind = event.get("type", "")
        if kind not in ("task_done", "task_failed", "task_waiting_confirmation"):
            return False
        task = event.get("task") or {}
        task_id = task.get("id", "")
        user_id = self.owner.get(task_id) or (task.get("meta") or {}).get("wechat_user_id")
        if not user_id:
            return False
        if kind == "task_done":
            text = "做完了 %s\n%s" % (task_id, (task.get("note") or
                                            (task.get("result") or "")[:300]))
        elif kind == "task_failed":
            text = "%s 失败了：%s" % (task_id, task.get("error") or "未知原因")
        else:
            text = ("%s 在等你回答：%s\n（/answer %s 你的回答）"
                    % (task_id, task.get("question") or "", task_id))
        return self.reply(user_id, text)

    def watch_tasks(self):
        """Tell the user when their work is done."""
        while True:
            try:
                for event in self.queue.events(workspace_id=self.ensure_workspace()):
                    self.notify_event(event)
            except Exception as exc:  # noqa: BLE001
                log("task watcher: %s" % exc)
                time.sleep(5)


def build_parser():
    parser = argparse.ArgumentParser(description="The WeChat side of the task queue.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="the iLink server")
    parser.add_argument("--agent", default=os.environ.get("TEA_AGENT_URL", DEFAULT_AGENT),
                        help="the agent's HTTP API (for the workspace list)")
    parser.add_argument("--tasks", default=os.environ.get("TEA_TASK_URL", DEFAULT_TASKS),
                        help="the task server's HTTP API")
    parser.add_argument("--state-dir", default=DEFAULT_STATE_DIR,
                        help="where the credentials and the context tokens live")
    parser.add_argument("--workspace-path", default=DEFAULT_WORKSPACE_PATH,
                        help="the workspace the WeChat tasks work in")
    parser.add_argument("--workspace-name", default="微信", help="its name in the agent")
    parser.add_argument("--provider", default=os.environ.get("TEA_TASK_PROVIDER", ""),
                        help="the provider whose quota gates the work (default: the queue's)")
    parser.add_argument("--allow-path", action="append", default=[],
                        help="a directory the WeChat tasks may touch (repeatable)")
    parser.add_argument("--login", action="store_true", help="scan the QR code and exit")
    parser.add_argument("--login-timeout", type=float, default=300.0,
                        help="how long to wait for the scan (default: 300 s)")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)

    bot = WeChatBot(args)
    if args.login:
        return 0 if bot.login() else 1
    try:
        return bot.run()
    except KeyboardInterrupt:
        log("stopped")
        return 0


if __name__ == "__main__":
    sys.exit(main())
