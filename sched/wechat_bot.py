#!/usr/bin/env python3
# -*- coding: utf-8 -*-
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
import binascii
import hashlib
import http.client
import io
import json
import os
import re
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import channel
import task_order
import tunnel_cmd


if sys.version_info[0] < 3:
    # 这台机器的 `python` 是 2.7（`python3` 才是 3.6）。这个脚本是 Python 3 的，
    # 在 2 里连解析都过不去 —— 与其让用户看 "Non-ASCII character"，
    # 不如直接说该用哪个解释器。
    sys.stderr.write("这个脚本要 Python 3：请用 python3 跑"
                     "（这台机器的 python 是 2.7）。\n")
    sys.exit(2)

# The queue and the agent are the ones every chat front end talks to; the
# WeChat side only adds the protocol on top (sched/channel.py).
DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com"
DEFAULT_AGENT = channel.DEFAULT_AGENT
DEFAULT_TASKS = channel.DEFAULT_TASKS
# One WeChat account = one directory: its own credentials, its own cursor,
# its own context tokens. The default account keeps the old place
# (~/.tea_agent/wechat), a named one lives in ~/.tea_agent/wechat/<name>.
# The tokens of every account are known to the machine - the protocol wants
# them in `local_token_list`, so a second scan of an account that is already
# bound here is recognized instead of making a second bot for it.
DEFAULT_STATE_DIR = os.path.expanduser("~/.tea_agent/wechat")
DEFAULT_WORKSPACE_PATH = os.path.expanduser("~/.tea_agent/wechat-workspace")
# The pairing page (sched/wechat_pair.py): the place where a person scans
# the code and the service starts. The log points at it.
PAIRING_HINT = "http://127.0.0.1:8391/"
# The WeChat CDN: the media (images, video, files, voice) live there, encrypted
# with AES-128-ECB. The bot talks to it directly - the iLink server only hands
# out the tickets.
CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c"
CHANNEL_VERSION = "2.0.0"
# The protocol wants a client version; the SDK encodes it as 0x00MMNNPP.
CLIENT_VERSION = "0.10.0"

# The protocol's enums (iLink Bot API).
MSG_TYPE_USER = 1
# A wechat message that is too long is refused by the far side, so a long
# answer goes out in pieces of this size.
MESSAGE_CHUNK = 1800

MSG_TYPE_BOT = 2
MSG_STATE_FINISH = 2
ITEM_TEXT = 1
ITEM_IMAGE = 2
ITEM_FILE = 4
# The media kinds. The item *number* is not documented on our side, so the
# media of an incoming message is found by the key the item carries
# ("file_item", "image_item", ...). That way a picture is neither mistaken
# for a file nor dropped on the floor.
MEDIA_ITEM_KEYS = ("file_item", "image_item", "video_item", "voice_item")
# The magic bytes of the pictures we know.
IMAGE_MAGIC = channel.IMAGE_MAGIC
# The media_type the upload API wants for a plain file.
MEDIA_IMAGE = 1
MEDIA_FILE = 3
# A file bigger than this is not worth sending (and WeChat may refuse it).
MAX_FILE_BYTES = channel.MAX_FILE_BYTES
# How many files one task may send back.
MAX_FILES_PER_TASK = channel.MAX_FILES_PER_TASK

# The shared half of every chat front end - the commands, the workspace, the
# inbox, the notifications - lives in sched/channel.py. These names stay here
# so the old imports keep working.
log = channel.log
Queue = channel.Queue
Agent = channel.Agent
safe_file_name = channel.safe_file_name
looks_like_image = channel.looks_like_image
image_ext = channel.image_ext
idempotency_key = channel.idempotency_key
split_schedule = channel.split_schedule
is_task_order = channel.is_task_order
fmt_task = channel.fmt_task
resolve_workspace = channel.resolve_workspace

# The iLink protocol's error is the channel error: one class for everything
# that answers badly on the way out.
ILinkError = channel.ChannelError


def _openssl(args, data):
    """Run openssl with the bytes on stdin, return the bytes on stdout.

    The media path needs AES-128-ECB, and the standard library does not have
    it - the device this runs on has no pip, so openssl does the work.

    Two ways to call it: `openssl` on the PATH (every normal Linux), and
    `busybox openssl` on the small devices, where openssl is an applet of
    busybox rather than a program of its own.
    """
    last = None
    for command in (["openssl"], ["busybox", "openssl"]):
        try:
            proc = subprocess.run(command + args, input=data,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300)
        except FileNotFoundError:
            last = command[0]
            continue
        except subprocess.TimeoutExpired:
            raise ILinkError("openssl timed out") from None
        if proc.returncode != 0:
            detail = proc.stderr.decode("utf-8", "replace").strip().replace("\n", " ")[:160]
            raise ILinkError("openssl failed: " + (detail or "exit %d" % proc.returncode))
        return proc.stdout
    raise ILinkError("%s is not installed - sending files needs it" % (last or "openssl"))


def aes_encrypt(data, key):
    """AES-128-ECB + PKCS7, the way the WeChat CDN stores media.

    `key` is the 16 raw bytes; the ciphertext is ceil((len+1)/16)*16 long.
    """
    return _openssl(["enc", "-aes-128-ecb", "-K", key.hex(), "-nosalt"], data)


def aes_decrypt(data, key):
    """The other half: AES-128-ECB, padding removed."""
    if len(data) % 16 != 0:
        raise ILinkError("the encrypted file is not a multiple of 16 bytes (%d)" % len(data))
    return _openssl(["enc", "-d", "-aes-128-ecb", "-K", key.hex(), "-nosalt"], data)


def decode_aes_key(encoded):
    """The protocol has three ways of writing the same 16 bytes.

      * 32 hex characters            (image_item.aeskey)
      * base64 of the 16 raw bytes   (CDNMedia.aes_key, format A)
      * base64 of the hex string     (CDNMedia.aes_key, format B)
    """
    text = (encoded or "").strip()
    if not text:
        raise ILinkError("the message carries no aes key")
    if len(text) == 32 and all(c in "0123456789abcdefABCDEF" for c in text):
        return binascii.unhexlify(text)
    try:
        raw = base64.b64decode(text + "=" * (-len(text) % 4))
    except (ValueError, binascii.Error):
        raise ILinkError("cannot decode the aes key") from None
    if len(raw) == 16:
        return raw
    if len(raw) == 32:
        try:
            as_text = raw.decode("ascii")
        except UnicodeDecodeError:
            as_text = ""
        if len(as_text) == 32 and all(c in "0123456789abcdefABCDEF" for c in as_text):
            return binascii.unhexlify(as_text)
    raise ILinkError("the aes key has an unexpected length (%d bytes)" % len(raw))


def encode_aes_key(key):
    """The key as the CDN media reference wants it: base64 of the hex string."""
    return base64.b64encode(key.hex().encode("ascii")).decode("ascii")


# -- the network ---------------------------------------------------------
# The WeChat CDN (novac2c.cdn.weixin.qq.com) also has IPv6 addresses, and
# this machine cannot reach them: the SYN goes out and no answer ever comes
# back. Python's socket.create_connection tries every address of a host in
# turn, so an upload first waited out the whole timeout (120 s) for each
# dead IPv6 address before it fell back to IPv4 - a push of five files took
# twenty minutes, and the upload ticket was stale by then ("prepare failed").
# IPv4 first, IPv6 only as a backup: the CDN answers on IPv4 in 50 ms.


def connect_ipv4_first(address, timeout=None, source_address=None):
    """One connection to the address - IPv4 before IPv6."""
    host, port = address
    infos = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    infos.sort(key=lambda one: 0 if one[0] == socket.AF_INET else 1)
    err = None
    for af, socktype, proto, canonname, sa in infos:
        sock = socket.socket(af, socktype, proto)
        if timeout is not None:
            sock.settimeout(timeout)
        if source_address:
            sock.bind(source_address)
        try:
            sock.connect(sa)
            return sock
        except OSError as exc:
            sock.close()
            err = exc
    if err is not None:
        raise err
    raise OSError("no address for %s" % (host,))


class IPv4FirstConnection(http.client.HTTPSConnection):
    """HTTPS that dials IPv4 before IPv6.

    The TLS handshake is the one of HTTPSConnection - only the socket
    underneath is ours.
    """

    def connect(self):
        self.sock = connect_ipv4_first((self.host, self.port), self.timeout,
                                        self.source_address)
        if self._tunnel_host:
            self._tunnel()
        server_hostname = self._tunnel_host or self.host
        self.sock = self._context.wrap_socket(self.sock,
                                              server_hostname=server_hostname)


class IPv4FirstHandler(urllib.request.HTTPSHandler):
    """The handler that uses that connection."""

    def https_open(self, req):
        return self.do_open(IPv4FirstConnection, req,
                            context=self._context, check_hostname=self._check_hostname)


_OPENER = None
_OPENER_LOCK = threading.Lock()


def http_open(req, timeout=None):
    """Open a request - over IPv4 whenever the host has one."""
    global _OPENER
    with _OPENER_LOCK:
        if _OPENER is None:
            _OPENER = urllib.request.build_opener(IPv4FirstHandler)
    return _OPENER.open(req, timeout=timeout)


class ILink:
    """The raw iLink protocol: login, long polling, sending.

    Only the standard library: the device this runs on has no pip.
    """

    def __init__(self, base_url=DEFAULT_BASE_URL, client_version=CLIENT_VERSION,
                 cdn_base=CDN_BASE_URL):
        self.base_url = base_url.rstrip("/")
        self.cdn_base = cdn_base.rstrip("/")
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
            with http_open(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            raise ILinkError("HTTP %s: %s" % (exc.code, raw[:200]), status=exc.code) from None
        except urllib.error.URLError as exc:
            raise ILinkError("cannot reach %s: %s" % (url, exc.reason)) from None
        except (TimeoutError, OSError) as exc:
            # A read that times out is **not** a URLError (Python 3.10 made
            # socket.timeout an alias of TimeoutError, an OSError) - urllib
            # lets it through, and it used to kill the bot in the middle of
            # the login instead of being one more failed poll.
            raise ILinkError("timeout talking to %s: %s" % (url, exc)) from None
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
        # The status endpoint is a **long poll**: the server holds the
        # connection until the code changes (about 35 s) - so the client has
        # to wait longer than that, or every poll would be a timeout.
        return self._request("GET", "/ilink/bot/get_qrcode_status", None, None, 45, query)

    def updates(self, cursor, token, timeout=45):
        body = {"get_updates_buf": cursor or "", "base_info": self._base_info()}
        return self._post("/ilink/bot/getupdates", body, token, timeout)

    def send_text(self, user_id, context_token, text, token):
        return self.send_items(user_id, context_token,
                               [{"type": ITEM_TEXT, "text_item": {"text": text}}], token)

    def send_items(self, user_id, context_token, items, token):
        """One message with any number of items (text, a file, ...)."""
        msg = {
            "from_user_id": "",
            "to_user_id": user_id,
            "client_id": "tea-a2-" + base64.b32encode(os.urandom(8)).decode().strip("="),
            "message_type": MSG_TYPE_BOT,
            "message_state": MSG_STATE_FINISH,
            "context_token": context_token,
            "item_list": list(items),
        }
        return self._post("/ilink/bot/sendmessage", {"msg": msg, "base_info": self._base_info()}, token)

    # -- the media (the CDN) ----------------------------------------------
    def get_upload_url(self, filekey, to_user_id, rawsize, rawfilemd5, filesize, aeskey, token, media_type=MEDIA_FILE):
        """Ask the iLink server for a ticket to upload one file."""
        body = {
            "filekey": filekey,
            "media_type": media_type,
            "to_user_id": to_user_id,
            "rawsize": rawsize,
            "rawfilemd5": rawfilemd5,
            "filesize": filesize,
            "no_need_thumb": True,
            "aeskey": aeskey,
            "base_info": self._base_info(),
        }
        return self._post("/ilink/bot/getuploadurl", body, token)

    def upload_to_cdn(self, upload_param, filekey, ciphertext):
        """Put the encrypted bytes on the CDN. Returns the download reference."""
        url = ("%s/upload?encrypted_query_param=%s&filekey=%s"
               % (self.cdn_base,
                  urllib.parse.quote(upload_param, safe=""),
                  urllib.parse.quote(filekey, safe="")))
        req = urllib.request.Request(url, data=ciphertext, method="POST")
        req.add_header("Content-Type", "application/octet-stream")
        try:
            with http_open(req, timeout=120) as resp:
                resp.read()
                param = resp.headers.get("x-encrypted-param")
        except urllib.error.HTTPError as exc:
            detail = exc.headers.get("x-error-message") if exc.headers else ""
            raise ILinkError("CDN upload failed: HTTP %s %s" % (exc.code, detail or ""),
                             status=exc.code) from None
        except urllib.error.URLError as exc:
            raise ILinkError("cannot reach the CDN %s: %s" % (url, exc.reason)) from None
        if not param:
            raise ILinkError("the CDN upload did not return x-encrypted-param")
        return param

    def download_url(self, url):
        """The bytes behind a full CDN url."""
        req = urllib.request.Request(url, method="GET")
        try:
            with http_open(req, timeout=120) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            raise ILinkError("CDN download failed: HTTP %s" % exc.code, status=exc.code) from None
        except urllib.error.URLError as exc:
            raise ILinkError("cannot reach the CDN %s: %s" % (url, exc.reason)) from None

    def download_from_cdn(self, encrypt_query_param):
        """The encrypted bytes of a file somebody sent us."""
        url = ("%s/download?encrypted_query_param=%s"
               % (self.cdn_base, urllib.parse.quote(encrypt_query_param, safe="")))
        req = urllib.request.Request(url, method="GET")
        try:
            with http_open(req, timeout=120) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            raise ILinkError("CDN download failed: HTTP %s" % exc.code, status=exc.code) from None
        except urllib.error.URLError as exc:
            raise ILinkError("cannot reach the CDN %s: %s" % (url, exc.reason)) from None

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

    @property
    def dir(self):
        """The directory the credentials live in."""
        return os.path.dirname(self.path)

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


def utf8_stdout():
    """Let stdout carry the Chinese - the locale is not always UTF-8.

    A machine with the POSIX locale and no LANG in the environment gives a
    stdout that can only write ASCII: printing the Chinese there raises
    UnicodeEncodeError and kills the command that was about to work (the
    devices run with LANG=zh_CN.UTF-8, but an SSH shell often has neither).
    """
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:            # Python 3.7+
        try:
            reconfigure(encoding="utf-8", errors="replace")
            return
        except (ValueError, OSError):
            pass
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                      errors="replace", line_buffering=True)
    except (AttributeError, ValueError):
        pass


def safe_name(name):
    """A name that is safe as a directory name."""
    out = re.sub(r"[^0-9A-Za-z._\-\u4e00-\u9fff]", "-", (name or "").strip())
    return out.strip(".-") or "account"


def is_default_account(name):
    """Is this the account without a name of its own?"""
    return (name or "").strip() in ("", "default")


def account_state_dir(name=""):
    """Where the state of one account lives."""
    if is_default_account(name):
        return DEFAULT_STATE_DIR
    return os.path.join(DEFAULT_STATE_DIR, safe_name(name))


def account_workspace(name=""):
    """The workspace of one account: its own, so two accounts do not mix."""
    if is_default_account(name):
        return DEFAULT_WORKSPACE_PATH
    return os.path.expanduser("~/.tea_agent/wechat-%s-workspace" % safe_name(name))


def read_credentials(path):
    """The credentials of one account (empty when there are none)."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def credential_files(root=DEFAULT_STATE_DIR):
    """Every credentials file on this machine (the default one first)."""
    out = []
    try:
        names = sorted(os.listdir(root))
    except OSError:
        names = []
    for name in names:
        path = os.path.join(root, name, "credentials.json")
        if os.path.isfile(path):
            out.append(path)
    default = os.path.join(root, "credentials.json")
    if os.path.isfile(default):
        out.append(default)
    return out


def known_tokens(root=DEFAULT_STATE_DIR, limit=10):
    """The bot tokens this machine knows (the newest first).

    The protocol asks for them in `local_token_list`: the server then
    answers `binded_redirect` when the person scans a bot that is already
    bound here, instead of issuing a duplicate session.
    """
    found = []
    for path in credential_files(root):
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            mtime = 0.0
        token = read_credentials(path).get("token") or ""
        if token:
            found.append((mtime, token))
    found.sort(reverse=True)
    out = []
    for _, token in found:
        if token not in out:
            out.append(token)
    return out[:limit]


def save_qr(directory, content):
    """Put the QR code where it can be shown, and say what it is.

    The protocol gives the code as a URL: usually a small page that shows
    the code and asks the phone to confirm it. That page does not allow
    itself to be embedded in another page (X-Frame-Options), so it has to
    be opened - on the screen the phone can see. Sometimes the code is the
    picture itself; then it is saved next to the credentials.

      {"url": <the link>, "image": <the file>, "kind": "url"|"image"}
    """
    text = (content or "").strip()
    out = {"url": "", "image": "", "kind": ""}
    if not text:
        return out
    os.makedirs(directory, exist_ok=True)
    if text.startswith("http://") or text.startswith("https://"):
        out["url"] = text
        out["kind"] = "url"
        data = b""
        try:
            req = urllib.request.Request(text, method="GET")
            with http_open(req, timeout=20) as resp:
                data = resp.read()
        except (urllib.error.URLError, OSError, ValueError):
            data = b""
        if channel.looks_like_image(data):
            path = os.path.join(directory, "qr.png")
            try:
                with open(path, "wb") as f:
                    f.write(data)
                out["image"] = path
                out["kind"] = "image"
            except OSError:
                pass
        return out
    if text.startswith("data:"):
        text = text.partition(",")[2]
    if not re.fullmatch(r"[A-Za-z0-9+/=\s]+", text):
        return out
    try:
        data = base64.b64decode(text + "=" * (-len(text) % 4))
    except (ValueError, binascii.Error):
        return out
    if not channel.looks_like_image(data):
        return out
    path = os.path.join(directory, "qr.png")
    try:
        with open(path, "wb") as f:
            f.write(data)
    except OSError:
        return out
    out["image"] = path
    out["kind"] = "image"
    return out


def save_qr_image(directory, content):
    """The picture of the QR code (empty when the code is only a link)."""
    return save_qr(directory, content)["image"]


def text_of(raw):
    """The text of an incoming message."""
    parts = []
    for item in raw.get("item_list") or []:
        if item.get("type") == ITEM_TEXT:
            parts.append((item.get("text_item") or {}).get("text", ""))
    return "\n".join(p for p in parts if p)


def media_of(raw):
    """The media of an incoming message: (kind, info) for every item.

    The kind comes from the key the item carries, not from its number: a
    file is a "file_item", a picture an "image_item".
    """
    out = []
    for item in raw.get("item_list") or []:
        for kind in MEDIA_ITEM_KEYS:
            info = item.get(kind) or {}
            if info:
                out.append((kind, info))
    return out


def files_of(raw):
    """The files in an incoming message (the user sent one)."""
    return [info for kind, info in media_of(raw) if kind == "file_item"]


class WeChatBot(channel.TaskChannel):
    """The bot: poll WeChat, drive the queue, answer the user."""

    META_USER_KEY = "wechat_user_id"

    def own_user_id(self):
        """The id of this WeChat account (the one that gets the results).

        The account's own id is in the credentials (the login status reports
        it). It is what a **scheduled** task's result goes to: the schedule
        belongs to the account whose workspace it runs in, and only that
        account's channel sees its events - so the result must not be looked
        up in a recipe that somebody wrote by hand (it may carry a wrong id,
        and then the result goes nowhere).
        """
        try:
            return str(self.state.data.get("ilink_user_id") or "").strip()
        except Exception:  # noqa: BLE001 - the state may not be loaded yet
            return ""

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
        resolve_paths(args)
        channel.TaskChannel.__init__(self, args)
        self.state = State(os.path.join(args.state_dir, "credentials.json"))
        # The directory of the account exists from the start: the code, the
        # pair code and the credentials all live in it.
        try:
            os.makedirs(self.state.dir, exist_ok=True)
        except OSError:
            pass
        # Where the accounts of this machine live: the directory of the
        # default account, or the one its parent (a named account lives in
        # a directory of its own).
        name = (getattr(args, "account", "") or "").strip()
        self.accounts_root = (self.state.dir if is_default_account(name)
                               else os.path.dirname(self.state.dir))
        # Somebody can ask the bot to stop (the multi service does that when
        # an account is gone).
        self.stop_event = None
        self.ilink = ILink(args.base_url, cdn_base=getattr(args, "cdn_url", CDN_BASE_URL))

    def channel_name(self):
        """The name of this channel - it is the trigger source too.

        A named account is a source of its own ("wechat:alice"), so the
        rules and the threads of two accounts never mix.
        """
        name = (getattr(self.args, "account", "") or "").strip()
        if is_default_account(name):
            return "wechat"
        return "wechat:%s" % name

    def pairing_busy(self):
        """Is somebody else already scanning for this account?

        The pairing page writes a marker while it holds a QR code: the bot
        must not ask for a second one, or the person would have two codes
        to choose from.
        """
        return os.path.exists(os.path.join(self.state.dir, "pairing"))

    # -- login ------------------------------------------------------------
    def login(self, verify_code=""):
        """Show a QR code and wait for the scan.

        The code is also written into the account's directory (`qr.png`)
        and the URL is in the log: the person who scans does not have to
        sit at this terminal - the pairing page (sched/wechat_pair.py)
        shows the same code and starts the service when the scan is done.
        """
        deadline = time.time() + self.args.login_timeout
        while time.time() < deadline:
            if self.stopped():
                return False
            try:
                qr = self.ilink.get_qrcode(known_tokens(self.accounts_root))
            except ILinkError as exc:
                log("cannot ask for a QR code: %s" % exc)
                time.sleep(5)
                continue
            code = qr.get("qrcode") or qr.get("qrcode_id") or ""
            image = qr.get("qrcode_img_content") or qr.get("qrcode_url") or ""
            info = save_qr(self.state.dir, image)
            # The link is written down as well: the pairing page shows it,
            # so the person who scans does not have to read the log.
            if info["url"]:
                try:
                    with open(os.path.join(self.state.dir, "qr.url"), "w",
                              encoding="utf-8") as f:
                        f.write(info["url"])
                except OSError:
                    pass
            log("用微信扫这个码登录：%s" % (info["url"] or info["image"] or code))
            log("  （那个链接要在「手机能看见的屏幕」上打开，页面上就是二维码）")
            log("  配对页面：%s" % PAIRING_HINT)
            if self.await_scan(code, deadline, verify_code):
                return True
            verify_code = ""          # a new code deserves a new chance
            time.sleep(1)
        log("登录超时")
        return False

    def await_scan(self, code, deadline, verify_code=""):
        """Poll the QR code until the person scans it (or it dies)."""
        while time.time() < deadline:
            if self.stopped():
                return False
            time.sleep(2)
            try:
                status = self.ilink.qr_status(code, verify_code)
            except ILinkError as exc:
                log("qr: %s" % exc)
                continue
            state = str(status.get("status") or "")
            if state in ("", "wait"):
                continue
            if state == "scaned":
                log("已扫码，等你确认…")
                continue
            if state == "expired":
                log("二维码过期，重新来一个")
                return False
            if state == "need_verifycode":
                # Risk control: the phone shows a few digits. They can be
                # typed here, given as an argument, or written into the
                # account's directory (the pairing page does that).
                given = self.ask_verify_code()
                if given:
                    verify_code = given
                    log("收到验证码，继续等")
                continue
            if state == "verify_code_blocked":
                log("验证码错得太多，这个码废了，重新来一个")
                return False
            if state == "scaned_but_redirect":
                host = str(status.get("redirect_host") or "")
                if host:
                    self.ilink.base_url = host if "://" in host else "https://" + host
                    log("跟着跳转继续问：%s" % self.ilink.base_url)
                continue
            if state == "binded_redirect":
                # The person scanned a bot that is already bound to this
                # machine: take its credentials over instead of making a
                # second bot for the same account.
                return self.take_over_bound()
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
        return False

    def ask_verify_code(self):
        """The digits the phone shows (risk control).

        Three ways to give them: the file in the account's directory (the
        pairing page writes it), the argument, or the terminal.
        """
        path = os.path.join(self.state.dir, "verify_code")
        try:
            with open(path, encoding="utf-8") as f:
                code = f.read().strip()
            if code:
                try:
                    os.unlink(path)
                except OSError:
                    pass
                return code
        except OSError:
            pass
        code = str(getattr(self.args, "verify_code", "") or "").strip()
        if code:
            return code
        try:
            if sys.stdin.isatty():
                return input("手机微信上显示的验证码：").strip()
        except (EOFError, KeyboardInterrupt):
            return ""
        return ""

    def take_over_bound(self):
        """Use the credentials of the bot that is already bound here."""
        for path in credential_files(self.accounts_root):
            data = read_credentials(path)
            if not data.get("token"):
                continue
            self.state.data.update({
                "token": data.get("token") or "",
                "base_url": data.get("base_url") or self.args.base_url,
                "ilink_bot_id": data.get("ilink_bot_id") or "",
                "ilink_user_id": data.get("ilink_user_id") or "",
            })
            self.state.save()
            log("这个微信号已经绑在这台机器上了，直接用它的凭证")
            return True
        log("服务器说这个码已经绑定了，可本机没有找到它的凭证")
        return False

    # -- the files ---------------------------------------------------------
    def fetch_media(self, kind, info):
        """The bytes of one media item, decrypted. None if it did not work."""
        media = info.get("media") or {}
        param = (media.get("encrypt_query_param")
                 or info.get("encrypt_query_param") or "")
        key_text = (media.get("aes_key") or media.get("aeskey")
                    or info.get("aes_key") or info.get("aeskey") or "")
        try:
            if param:
                raw = self.ilink.download_from_cdn(param)
            elif info.get("cdn_url"):
                raw = self.ilink.download_url(info["cdn_url"])
            else:
                log("a %s arrived without a download reference - skipped" % kind)
                log("  its keys: %s" % ", ".join(sorted(info.keys())))
                return None
            if looks_like_image(raw):
                return raw                       # already the picture itself
            if key_text:
                return aes_decrypt(raw, decode_aes_key(key_text))
            if len(raw) % 16 == 0:
                log("a %s arrived encrypted but without a key - skipped" % kind)
                log("  its keys: %s" % ", ".join(sorted(info.keys())))
                return None
            return raw
        except ILinkError as exc:
            log("cannot fetch the %s: %s" % (kind, exc))
            log("  its keys: %s" % ", ".join(sorted(info.keys())))
            return None

    def media_name(self, kind, info, data):
        """The name the media gets on disk."""
        name = info.get("file_name") or info.get("name") or ""
        if not name and kind != "file_item":
            name = "image" + image_ext(data)
        return safe_file_name(name or "file.bin")

    def receive_files(self, raw):
        """Download the media the user sent. Returns [(kind, name, bytes)].

        Only the fetching happens here; putting the bytes on disk is the
        shared half's job (sched/channel.py), so every chat front end does it
        the same way.
        """
        got = []
        for kind, info in media_of(raw):
            data = self.fetch_media(kind, info)
            if data is None:
                continue
            if len(data) > MAX_FILE_BYTES:
                log("the %s is too big (%d bytes) - skipped" % (kind, len(data)))
                continue
            want_md5 = (info.get("md5") or "").strip().lower()
            if want_md5 and hashlib.md5(data).hexdigest() != want_md5:
                log("the %s's md5 does not match - it was changed on the way" % kind)
            name = self.media_name(kind, info, data)
            got.append((channel.KIND_FILE if kind == "file_item" else channel.KIND_MEDIA,
                        name, data))
        return got

    def handle_message(self, raw, text, media, user_id):
        """A message that may carry files (or pictures) as well as text."""
        if not media:
            # 「开隧道」这类命令是死的：自己处理，不进模型
            # （模型去跑命令不可靠，还要烧一次调用）。
            reply = tunnel_cmd.maybe_handle(text, user_id)
            if reply is not None:
                return reply
            reply = self.handle(text, user_id)
            if (text or "").strip() in ("/help", "help", "帮助"):
                # 隧道这条命令是 bot 自己加的，channel 的帮助里没有 → 补一行
                reply = "%s\n%s" % (reply, tunnel_cmd.HELP_LINE)
            return reply
        attachments = self.receive_files(raw)
        if not attachments:
            return "文件没能取下来，再发一次？"
        return self.handle_attachments(text, attachments, user_id)

    def send_file(self, user_id, path, name=None):
        """One file to the user: encrypt, upload to the CDN, send."""
        with open(path, "rb") as f:
            data = f.read()
        if len(data) > MAX_FILE_BYTES:
            raise ILinkError("the file is too big to send (%d bytes)" % len(data))
        key = os.urandom(16)
        ciphertext = aes_encrypt(data, key)
        picture = channel.looks_like_image(data)
        filekey = os.urandom(16).hex()
        info = self.ilink.get_upload_url(filekey, user_id, len(data),
                                         hashlib.md5(data).hexdigest(), len(ciphertext),
                                         key.hex(), self.state.token,
                                         media_type=(MEDIA_IMAGE if picture else MEDIA_FILE))
        upload_param = (info or {}).get("upload_param")
        if not upload_param:
            raise ILinkError("getuploadurl did not return upload_param")
        param = self.ilink.upload_to_cdn(upload_param, filekey, ciphertext)
        media = {"encrypt_query_param": param, "aes_key": encode_aes_key(key),
                 "encrypt_type": 1}
        if picture:
            item = {"type": ITEM_IMAGE,
                    "image_item": {"media": media,
                                  "file_name": name or os.path.basename(path),
                                  "mid_size": len(ciphertext)}}
        else:
            item = {"type": ITEM_FILE,
                    "file_item": {"media": media,
                                  "file_name": name or os.path.basename(path),
                                  "len": str(len(data))}}

        self.ilink.send_items(user_id, self.state.context_for(user_id), [item], self.state.token)
        return len(data)

    # -- the command mapping ----------------------------------------------
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

    def send_text(self, user_id, text):
        """One text message to the user (WeChat needs the context token).

        A long answer goes out in pieces: the whole answer has to arrive, and
        one message that is too long is refused by the far side (or arrives
        cut). Every piece carries the same context token.
        """
        if not text:
            return True
        for part in channel.chunks(text, MESSAGE_CHUNK):
            if not self.reply(user_id, part):
                return False
        return True

    def poll_once(self):
        """One long poll. Returns the messages that came in."""
        payload = self.ilink.updates(self.state.cursor, self.state.token)
        cursor = payload.get("get_updates_buf")
        if cursor and cursor != self.state.cursor:
            self.state.data["cursor"] = cursor
            self.state.save()
        return payload.get("msgs") or []

    def wait_for_credentials(self):
        """Wait until somebody has scanned the QR code.

        As a service this is the normal way in: the unit starts, the first
        thing that happens is a QR code - in the log, in the account's
        directory (`qr.png`) and on the pairing page. Whoever scans it (a
        phone, from anywhere) connects the bot; the service waits until
        then. Nobody has to sit at the machine.
        """
        log("还没有凭证，出个码等扫码（配对页面：%s）" % PAIRING_HINT)
        while not self.state.token:
            if self.stopped():
                return
            self.state.load()
            if self.state.token:
                break
            if self.pairing_busy():
                # The pairing page holds the code; wait for its result.
                time.sleep(5)
                continue
            if not self.login():
                time.sleep(5)
        log("拿到凭证了")

    def process_message(self, raw):
        """One message from a user: remember the context, answer it.

        A message can be text, a file, or both. The file is fetched from the
        CDN first, so the task can be about the file.
        """
        user_id = raw.get("from_user_id", "")
        context = raw.get("context_token", "")
        if user_id and context:
            self.state.remember_context(user_id, context)
        text = text_of(raw)
        media = media_of(raw)
        if not text and not media:
            return
        log("wechat[%s]: %s" % (user_id, (text or "")[:80] or ("%d 个媒体" % len(media))))
        try:
            answer = self.handle_message(raw, text, media, user_id)
        except Exception as exc:  # noqa: BLE001 - a bad message must not kill the bot
            answer = "出错了：%s: %s" % (type(exc).__name__, exc)
        self.reply(user_id, answer)

    def stopped(self):
        """Has somebody asked this bot to stop?"""
        event = self.stop_event
        return bool(event is not None and event.is_set())

    def run(self):
        started = False
        backoff = 1.0
        while True:
            if self.stopped():
                log("stopped")
                return 0
            if not self.state.token:
                # No credentials (yet): publish a QR code and wait for the
                # scan. The service stays up, so the scan is all it takes.
                self.wait_for_credentials()
                continue
            if not started:
                self.ensure_workspace()
                threading.Thread(target=self.watch_tasks, daemon=True).start()
                started = True
            try:
                messages = self.poll_once()
                backoff = 1.0
            except ILinkError as exc:
                if exc.errcode == -14:
                    # The session is gone (they expire after a day or so):
                    # put a new code out and wait for the next scan.
                    log("会话过期了，重新出一个码，扫一下就接上（配对页面：%s）"
                        % PAIRING_HINT)
                    self.state.data["token"] = ""
                    self.state.save()
                    continue
                log("poll: %s" % exc)
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)
                continue
            for raw in messages:
                if raw.get("message_type") != MSG_TYPE_USER:
                    continue
                self.process_message(raw)


def resolve_paths(args):
    """The paths of this account: its own state, its own workspace.

    Without a name the bot keeps the old place (one account on the
    machine). With a name every account gets a directory of its own, so two
    WeChat accounts never share a cursor or a context token - and the queue
    keeps their tasks apart too (each workspace is its own project).
    """
    name = (getattr(args, "account", "") or "").strip()
    if getattr(args, "state_dir", None) is None:
        args.state_dir = account_state_dir(name)
    if getattr(args, "workspace_path", None) is None:
        args.workspace_path = account_workspace(name)
    if getattr(args, "workspace_name", None) is None:
        # The name the agent shows for this account's workspace: one
        # workspace per account, and the name says whose it is.
        args.workspace_name = ("微信_%s" % name) if name else "微信"
    return args


def build_parser():
    parser = argparse.ArgumentParser(description="The WeChat side of the task queue.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="the iLink server")
    parser.add_argument("--agent", default=os.environ.get("TEA_AGENT_URL", DEFAULT_AGENT),
                        help="the agent's HTTP API (for the workspace list)")
    parser.add_argument("--tasks", default=os.environ.get("TEA_TASK_URL", DEFAULT_TASKS),
                        help="the task server's HTTP API")
    parser.add_argument("--account", default="",
                        help="the name of this WeChat account (default: the only one)")
    parser.add_argument("--state-dir", default=None,
                        help="where the credentials and the context tokens live")
    parser.add_argument("--workspace-path", default=None,
                        help="the workspace the WeChat tasks work in")
    parser.add_argument("--workspace-name", default=None, help="its name in the agent")
    parser.add_argument("--verify-code", default="",
                        help="the digits the phone shows, if the server asks for them")
    parser.add_argument("--provider", default=os.environ.get("TEA_TASK_PROVIDER", ""),
                        help="the provider whose quota gates the work (default: the queue's)")
    parser.add_argument("--allow-path", action="append", default=[],
                        help="a directory the WeChat tasks may touch (repeatable)")
    parser.add_argument("--speech", action="store_true",
                        help="let the router speak: the front end delivers the router's sentences instead of translating the queue's events itself")
    parser.add_argument("--triggers", action="store_true",
                        help="go through the queue's trigger entry: the queue decides\n                             (rules first, then the model) whether a message is work")
    parser.add_argument("--cdn-url", default=os.environ.get("TEA_WECHAT_CDN", CDN_BASE_URL),
                        help="the WeChat CDN that stores the media (files)")
    parser.add_argument("--login", action="store_true", help="scan the QR code and exit")
    parser.add_argument("--login-timeout", type=float, default=300.0,
                        help="how long to wait for the scan (default: 300 s)")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    utf8_stdout()

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
