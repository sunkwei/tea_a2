#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""百度网盘：让多台机器共用一份 tea_a2 配置。

配置在**机器本地**（`<tea_home>/provider.yaml`、`tea_agent.yaml`、`workspaces.json`…），
人一换机器就重新抄一遍 —— 这是 tea_a2 用起来最先撞上的墙。这个脚本拿**百度网盘**
当那根线：本地打包 → 加密 → 传到网盘的一个固定目录；另一台机器再拉下来。

    python3 sched/pan_sync.py auth            # 授权一次（设备码：给个网址 + 用户码）
    python3 sched/pan_sync.py status          # 本地和远端现在各是什么，有没有冲突
    python3 sched/pan_sync.py push            # 把本地配置推上去
    python3 sched/pan_sync.py pull            # 把远端配置拉下来
    python3 sched/pan_sync.py sync            # 先拉后推（日常就用这个）
    python3 sched/pan_sync.py watch           # 常驻，每隔几分钟同步一次
    python3 sched/pan_sync.py check           # 配置、令牌、网盘通不通

配置：`~/.tea_agent/pan-sync/config`（`TEA_PAN_CONFIG` 可改），格式是 `键: 值`
的纯文本，模板见 `sched/pan-sync.template`。令牌在
`~/.tea_agent/pan-sync/token.json`（0600），状态在 `~/.tea_agent/pan-sync/state.json`。

几条刻意的选择：

  * **只传配置，不传历史**。同步的是「跟谁说话、怎么干活」——provider.yaml 里带着
    API key，tea_agent.yaml 里带着 peers 的令牌。会话记录、数据库、日志一律不同步：
    那是每台机器自己的历史，搬过去只会把两边的记录搅在一起。
  * **推上去之前先加密**。provider.yaml 里有 API key，明文躺在网盘里等于把钥匙挂在
    门上。口令用 PBKDF2 派生密钥，整包加密 + HMAC 校验（encrypt-then-MAC）。
    没口令就不推 —— 配置「allow_plain: yes」才能明文上传，不推荐。
  * **两边都改了就停手**。远端比上次同步新、本地也改了 —— 这时候谁覆盖谁都是猜，
    脚本就停下（除非 `--force`），并把两边都说清楚，让人来定。**同步工具最忌讳
    的是「默默覆盖」**：它会把一台机器的 provider.yaml 换成另一台机器的，而没人知道。
  * **拉下来的东西先备份**。每次写本地文件之前，旧版本先存进
    `~/.tea_agent/pan-sync/backup/<时间戳>/`，覆盖了还能找回来。
  * **删除不传播**。本地删掉一个文件，不会把远端的也删掉 —— 少同步一个文件比删掉
    一份配置安全得多。
  * **只用标准库**：urllib / hashlib / hmac / zlib。设备上没有 pip 也能跑。

底层是百度网盘**开放平台**的 REST 接口（`pan.baidu.com/rest/2.0/xpan/…`），
授权走设备码模式（`openapi.baidu.com/oauth/2.0/device/code`）—— 不用在终端里
粘 code，一个网址 + 一个用户码就行。令牌 30 天过期，用 refresh_token 续（刷新令牌
只能用一次，所以每次刷新都要马上落盘）。

用法上和「机群」(`sched/fleet.py`) 是两件事：机群管的是**已经能直连**的设备，
这里管的是**连不上彼此**的机器 —— 一台在宿舍，一台在公司，都只有一个网盘账号。
"""

import argparse
import hashlib
import hmac
import json
import os
import re
import shutil
import socket
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zlib

HOME = os.path.expanduser("~")
PAN_HOME = os.environ.get("TEA_PAN_HOME") or os.path.join(HOME, ".tea_agent", "pan-sync")
CONFIG_PATH = os.environ.get("TEA_PAN_CONFIG") or os.path.join(PAN_HOME, "config")
TOKEN_PATH = os.path.join(PAN_HOME, "token.json")
STATE_PATH = os.path.join(PAN_HOME, "state.json")
BACKUP_DIR = os.path.join(PAN_HOME, "backup")
LOG_PATH = os.path.join(PAN_HOME, "logs", "pan_sync.log")

# 远端固定的两个文件：一个是加密包，一个是明文的清单（谁在什么时候推的、里面有什么）。
BUNDLE_NAME = "config.tea"
MANIFEST_NAME = "manifest.json"

# 分片大小：百度网盘单片 4MB。
CHUNK_SIZE = 4 * 1024 * 1024

# 上传域名那几个接口（locateupload / superfile2）在 d.pcs.baidu.com 上，
# 而且上传域名本身是动态的（要问 locateupload）。
PCS_BASE = "https://d.pcs.baidu.com"

# 加密包的格式：magic + salt + nonce + ciphertext + tag。
MAGIC = b"TEAPAN1"
PBKDF2_ROUNDS = 200000

# 同步哪些文件：跟谁说话、怎么干活。
#
# **不要**把 workspaces.json 放进来：那是**每台机器自己的**（里面是本机的绝对路径
# 和会话计数），搬到另一台上只会把它自己的工作区清单覆盖成不存在的路径。
# 不含会话历史、不含数据库。
DEFAULT_FILES = ("provider.yaml", "tea_agent.yaml", "mcp.yaml")

TRUE_WORDS = ("1", "yes", "on", "true", "y", "开", "是")
FALSE_WORDS = ("0", "no", "off", "false", "n", "关", "否")


class PanError(Exception):
    """网盘/配置出错了。消息是给人看的，要说清楚是哪一步。"""

    def __init__(self, message, errno=None):
        Exception.__init__(self, message)
        self.errno = errno


class ConfigError(Exception):
    """配置读不了。消息里带行号。"""


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
DEFAULTS = {
    # 开放平台的凭证（https://pan.baidu.com/union/console/createapp）
    "app_key": "",
    "app_secret": "",
    # 同步组：不同的人/机器群用不同的组名，互不打扰。
    "profile": "default",
    "remote_dir": "",            # 空 = /tea_a2/<profile>
    # 加密口令。没口令不推（provider.yaml 里有 API key）。
    "passphrase": "",
    "allow_plain": "no",         # yes = 明文上传，不推荐
    # 同步哪些文件（文件名，逗号分隔）。workspaces.json 是**每台机器自己的**，
    # 里面有本机绝对路径，不要同步（要同步就自己写上，那是你的选择）。
    "files": "provider.yaml, tea_agent.yaml, mcp.yaml",
    "tea_home": "",              # 空 = $TEA_AGENT_HOME 或 ~/.tea_agent
    "machine": "",               # 空 = 主机名
    # 节奏与超时
    "interval": "300",
    "timeout": "60",
    # 换网关（测试用；生产不用动）
    "api_base": "https://pan.baidu.com",
    "oauth_base": "https://openapi.baidu.com",
    "pcs_base": "https://d.pcs.baidu.com",
    "note": "",                  # 给人看的备注
}

BLOCK_KEYS = ("note",)

KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*)$")


class Config(object):
    """一张扁平的表：键 -> 字符串。判空、转数字、转布尔在这里做一次。"""

    def __init__(self, values=None, path=""):
        self.values = dict(DEFAULTS)
        self.values.update(values or {})
        self.path = path

    def get(self, key, default=""):
        value = self.values.get(key)
        if value is None:
            return default
        return value

    def text(self, key):
        return (self.get(key) or "").strip()

    def integer(self, key):
        raw = self.text(key)
        try:
            return int(raw)
        except ValueError:
            raise ConfigError("%s 要一个数字，写的是 %r" % (key, raw))

    def boolean(self, key):
        raw = self.text(key).lower()
        if not raw:
            return False
        if raw in TRUE_WORDS:
            return True
        if raw in FALSE_WORDS:
            return False
        raise ConfigError("%s 要 yes/no，写的是 %r" % (key, raw))

    def file_list(self):
        raw = self.text("files")
        names = [part.strip() for part in raw.split(",") if part.strip()]
        if not names:
            raise ConfigError("files 是空的：至少要写一个要同步的文件名")
        for name in names:
            if not safe_name(name):
                raise ConfigError("files 里的 %r 不像文件名（不许带目录）" % name)
        return names

    def tea_home(self):
        return (self.text("tea_home")
                or os.environ.get("TEA_AGENT_HOME")
                or os.path.join(HOME, ".tea_agent"))

    def remote_dir(self):
        raw = self.text("remote_dir")
        if raw:
            return raw if raw.startswith("/") else "/" + raw
        profile = self.text("profile") or "default"
        return "/tea_a2/" + profile

    def machine(self):
        return self.text("machine") or socket.gethostname() or "unknown"


def parse_config(text):
    """读配置：`键: 值`、缩进续行、`键: |` 的多行块、`#` 注释。

    不是 yaml —— 标准库里没有 yaml 解析器，而这个脚本要能裸 Python 跑。
    """
    values = {}
    errors = []
    block = None
    lines = (text or "").splitlines()

    for number, raw in enumerate(lines, 1):
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if line[:1] in (" ", "\t"):
            if block is None:
                errors.append("第 %d 行：这一行缩进了，但它属于上面哪个键？" % number)
                continue
            values[block].append(stripped)
            continue
        match = KEY_RE.match(stripped)
        if not match:
            errors.append("第 %d 行：看不懂这一行：%r" % (number, stripped[:40]))
            block = None
            continue
        key, value = match.group(1), match.group(2).strip()
        if key not in DEFAULTS:
            errors.append("第 %d 行：不认识的键 %r（认识的：%s）"
                          % (number, key, "、".join(sorted(DEFAULTS))))
        # 值后面的注释：只有「空格 + #」才算，网址里的 # 不受影响
        mark = value.find(" #")
        if mark >= 0:
            value = value[:mark].strip()
        if value in ("|", ">"):
            block = key
            values[key] = []
            continue
        block = None
        values[key] = value

    if errors:
        raise ConfigError("\n".join(errors))

    out = {}
    for key, value in values.items():
        if isinstance(value, list):
            out[key] = "\n".join(value).strip()
        else:
            out[key] = value
    return out


def load_config(path=None):
    path = path or CONFIG_PATH
    if not os.path.exists(path):
        raise ConfigError("没有配置文件：%s\n照 sched/pan-sync.template 抄一份，"
                          "填上 app_key / app_secret 和一个加密口令。" % path)
    with open(path, "r", encoding="utf-8") as handle:
        return Config(parse_config(handle.read()), path)


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(1 << 16)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def safe_name(name):
    """同步包里的文件名：只允许一层、不许 .. 、不许路径分隔符。"""
    if not name or len(name) > 128:
        return False
    if name in (".", ".."):
        return False
    if "/" in name or "\\" in name:
        return False
    return True


def iso_now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def ensure_dir(path):
    if not os.path.isdir(path):
        os.makedirs(path)


def write_secret(path, data):
    """带 0600 写文件：令牌这种东西不该别人能读。"""
    ensure_dir(os.path.dirname(path))
    handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(handle, data)
    finally:
        os.close(handle)


# ---------------------------------------------------------------------------
# 加密：口令 -> 密钥 -> 整包加密 + 校验
# ---------------------------------------------------------------------------
def _derive_keys(passphrase, salt):
    material = hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"), salt,
                                   PBKDF2_ROUNDS, 64)
    return material[:32], material[32:]


def _keystream(key, nonce, length):
    out = bytearray()
    counter = 0
    while len(out) < length:
        block = hmac.new(key, nonce + struct.pack(">Q", counter), hashlib.sha256).digest()
        out.extend(block)
        counter += 1
    return bytes(out[:length])


def encrypt_blob(data, passphrase):
    """口令 -> 盐/随机数 -> 加密流 + HMAC 标签。

    做法是常见的「口令派生密钥 + 计数器模式流加密 + encrypt-then-MAC」：
    密钥从口令 PBKDF2 派生（200000 轮），密文用 HMAC-SHA256 当伪随机流，
    标签覆盖 magic/盐/随机数/密文，解密前先验标签 —— 标签不过就不解密。
    """
    salt = os.urandom(16)
    nonce = os.urandom(16)
    enc_key, mac_key = _derive_keys(passphrase, salt)
    stream = _keystream(enc_key, nonce, len(data))
    cipher = bytes(bytearray(a ^ b for a, b in zip(bytearray(data), bytearray(stream))))
    tag = hmac.new(mac_key, MAGIC + salt + nonce + cipher, hashlib.sha256).digest()
    return MAGIC + salt + nonce + cipher + tag


def decrypt_blob(blob, passphrase):
    if not blob.startswith(MAGIC):
        raise PanError("这不像是 tea_a2 的同步包（头不对）")
    if len(blob) < len(MAGIC) + 16 + 16 + 32:
        raise PanError("同步包不完整")
    salt = blob[len(MAGIC):len(MAGIC) + 16]
    nonce = blob[len(MAGIC) + 16:len(MAGIC) + 32]
    body = blob[len(MAGIC) + 32:-32]
    tag = blob[-32:]
    enc_key, mac_key = _derive_keys(passphrase, salt)
    expect = hmac.new(mac_key, MAGIC + salt + nonce + body, hashlib.sha256).digest()
    if not hmac.compare_digest(tag, expect):
        raise PanError("口令不对，或者同步包坏了（校验没过）")
    stream = _keystream(enc_key, nonce, len(body))
    return bytes(bytearray(a ^ b for a, b in zip(bytearray(body), bytearray(stream))))


# ---------------------------------------------------------------------------
# 打包：一组文件 -> 一个可校验的包
# ---------------------------------------------------------------------------
BUNDLE_MAGIC = b"TEACFG1"


def pack_bundle(entries, machine):
    """entries: [(名字, 内容)] -> (压缩后的包, 清单头)。"""
    header = {
        "version": 1,
        "created_at": iso_now(),
        "machine": machine,
        "files": [{"name": name, "size": len(data), "sha256": sha256_bytes(data)}
                  for name, data in entries],
    }
    raw_header = json.dumps(header, sort_keys=True, ensure_ascii=False).encode("utf-8")
    body = b"".join(data for _, data in entries)
    blob = BUNDLE_MAGIC + struct.pack(">I", len(raw_header)) + raw_header + body
    return zlib.compress(blob, 6), header


def unpack_bundle(blob):
    """压缩包 -> (清单头, [(名字, 内容)])。名字和校验都要过一遍。"""
    try:
        raw = zlib.decompress(blob)
    except zlib.error:
        raise PanError("同步包解不开（不是压缩数据）")
    if not raw.startswith(BUNDLE_MAGIC):
        raise PanError("同步包的头不对")
    start = len(BUNDLE_MAGIC)
    header_len = struct.unpack(">I", raw[start:start + 4])[0]
    header = json.loads(raw[start + 4:start + 4 + header_len].decode("utf-8"))
    pos = start + 4 + header_len
    files = []
    for entry in header.get("files", []):
        name = entry.get("name", "")
        if not safe_name(name):
            raise PanError("同步包里有奇怪的文件名：%r" % name)
        size = int(entry.get("size", 0))
        data = raw[pos:pos + size]
        pos += size
        if len(data) != size:
            raise PanError("同步包不完整（%s 少了一块）" % name)
        if sha256_bytes(data) != entry.get("sha256"):
            raise PanError("同步包里 %s 的校验对不上" % name)
        files.append((name, data))
    return header, files


# ---------------------------------------------------------------------------
# 百度网盘开放平台
# ---------------------------------------------------------------------------
# 网盘那几个「看不懂」的错误码，翻译成人话（官方 FAQ 里的说法）。
ERRNO_HINTS = {
    20016: "access_token 已过期，重新跑 auth 授权一次",
    20017: "access_token 无效（用户解除了授权？），重新跑 auth 授权一次",
    -6: "鉴权失败：令牌无效，或授权时没勾选网盘权限（scope 要 basic,netdisk）",
    31034: "被风控了，需要人工验证，等一会儿再试",
    -7: "文件名或路径有问题（也不许访问这个目录）",
    -10: "网盘满了",
    31066: "文件被标记为违规",
}


class PanClient(object):
    """开放平台的 REST 客户端（标准库 urllib）。

    只用得到五件事：授权、列目录、建目录、上传、下载。
    """

    def __init__(self, app_key, app_secret, token_path=TOKEN_PATH,
                 api_base="https://pan.baidu.com",
                 oauth_base="https://openapi.baidu.com",
                 pcs_base=PCS_BASE, timeout=60):
        self.app_key = app_key
        self.app_secret = app_secret
        self.token_path = token_path
        self.api_base = api_base.rstrip("/")
        self.oauth_base = oauth_base.rstrip("/")
        # locateupload / superfile2 在 d.pcs.baidu.com 上（上传域名本身是动态的）
        self.pcs_base = pcs_base.rstrip("/")
        self.timeout = timeout
        self.token = self._load_token()

    # -- HTTP ---------------------------------------------------------------
    def _http(self, url, method="GET", body=None, headers=None):
        request = urllib.request.Request(url, data=body, method=method)
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read()[:200].decode("utf-8", "replace")
            except Exception:  # noqa: BLE001 - 读不出正文就算了
                detail = ""
            raise PanError("HTTP %s %s%s" % (exc.code, url, ("：" + detail) if detail else ""))
        except urllib.error.URLError as exc:
            raise PanError("连不上 %s：%s" % (url, exc.reason))

    def _api(self, path, params=None, method="GET", form=None, base=None):
        """业务接口。返回 dict；errno != 0 抛 PanError（带上 errno）。

        base：默认用 api_base；上传域名那几个接口在 d.pcs.baidu.com 上。
        """
        query = dict(params or {})
        url = (base or self.api_base) + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        body = None
        headers = {"User-Agent": "pan.baidu.com"}
        if form is not None:
            body = urllib.parse.urlencode(form).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        raw = self._http(url, method=method, body=body, headers=headers)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise PanError("接口返回的不是 JSON：%s" % raw[:200])
        errno = data.get("errno")
        if errno is None:
            # locateupload 这类接口用 error_code
            errno = data.get("error_code")
        if errno not in (0, None):
            message = data.get("errmsg") or data.get("show_msg") or data.get("error_msg") or ""
            hint = ERRNO_HINTS.get(errno, "")
            raise PanError("网盘接口报错 errno=%s %s%s"
                           % (errno, message, ("（%s）" % hint) if hint else ""),
                           errno=errno)
        return data

    # -- 令牌 ---------------------------------------------------------------
    def _load_token(self):
        if not os.path.exists(self.token_path):
            return None
        try:
            with open(self.token_path, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except (ValueError, OSError):
            return None

    def _save_token(self, token):
        write_secret(self.token_path,
                     json.dumps(token, sort_keys=True, ensure_ascii=False).encode("utf-8"))

    def _oauth(self, params):
        url = self.oauth_base + "/oauth/2.0/token?" + urllib.parse.urlencode(params)
        raw = self._http(url, headers={"User-Agent": "pan.baidu.com"})
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise PanError("授权接口返回的不是 JSON：%s" % raw[:200])
        if "error" in data:
            raise PanError("授权失败：%s %s"
                           % (data.get("error"), data.get("error_description") or ""))
        return data

    def device_code(self):
        """设备码模式第一步：要一个 device_code 和一个用户码。"""
        url = self.oauth_base + "/oauth/2.0/device/code?" + urllib.parse.urlencode({
            "response_type": "device_code",
            "client_id": self.app_key,
            "scope": "basic,netdisk",
        })
        raw = self._http(url, headers={"User-Agent": "pan.baidu.com"})
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise PanError("授权接口返回的不是 JSON：%s" % raw[:200])
        if "error" in data:
            raise PanError("拿不到设备码：%s %s"
                           % (data.get("error"), data.get("error_description") or ""))
        return data

    def device_token(self, device_code):
        """设备码模式第三步：拿设备码换令牌（未授权时会报 authorization_pending）。"""
        data = self._oauth({
            "grant_type": "device_token",
            "code": device_code,
            "client_id": self.app_key,
            "client_secret": self.app_secret,
        })
        return self._remember(data)

    def refresh_token(self, refresh_token):
        """刷新令牌。refresh_token 只能用一次，所以新的必须马上落盘。"""
        data = self._oauth({
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": self.app_key,
            "client_secret": self.app_secret,
        })
        return self._remember(data)

    def _remember(self, data):
        token = {
            "access_token": data.get("access_token", ""),
            "refresh_token": data.get("refresh_token", ""),
            "expires_at": time.time() + float(data.get("expires_in", 2592000)),
            "scope": data.get("scope", ""),
        }
        if not token["access_token"]:
            raise PanError("授权接口没给 access_token")
        self._save_token(token)
        self.token = token
        return token

    def ensure_token(self):
        """拿一个还能用的 access_token：过期了就用 refresh_token 续。"""
        token = self.token or self._load_token()
        if not token or not token.get("access_token"):
            raise PanError("还没授权：先跑 python3 sched/pan_sync.py auth")
        if float(token.get("expires_at", 0)) - 300 > time.time():
            self.token = token
            return token["access_token"]
        refresh = token.get("refresh_token")
        if not refresh:
            raise PanError("令牌过期了，而且没有 refresh_token：重新跑 auth 授权一次")
        try:
            fresh = self.refresh_token(refresh)
        except PanError as exc:
            raise PanError("刷新令牌失败（%s）：重新跑 auth 授权一次" % exc)
        return fresh["access_token"]

    # -- 文件 ---------------------------------------------------------------
    def list_dir(self, path):
        data = self._api("/rest/2.0/xpan/file", {
            "method": "list",
            "access_token": self.ensure_token(),
            "dir": path or "/",
            "order": "name",
            "desc": 0,
            "start": 0,
            "limit": 1000,
            "web": 1,
        })
        return data.get("list", []) or []

    def ensure_dir(self, path):
        """一级一级建目录。已经存在就算了（errno -8）。"""
        current = ""
        for part in [p for p in path.split("/") if p]:
            current += "/" + part
            try:
                self._api("/rest/2.0/xpan/file",
                          {"method": "create", "access_token": self.ensure_token()},
                          method="POST",
                          form={"path": current, "isdir": "1", "rtype": "0"})
            except PanError as exc:
                if exc.errno != -8:
                    raise

    def find(self, path):
        """在父目录里找到这个路径对应的条目。"""
        parent = path.rsplit("/", 1)[0] or "/"
        name = path.rsplit("/", 1)[-1]
        for entry in self.list_dir(parent):
            if entry.get("server_filename") == name or entry.get("path") == path:
                return entry
        return None

    def upload_host(self, remote_path, uploadid):
        """上传域名是**动态的**（官方文档：「不要在生产代码中固定使用示例域名」）。

        先问一次 locateupload，拿推荐的 host；拿不到就退回 d.pcs.baidu.com。
        """
        token = self.ensure_token()
        try:
            data = self._api("/rest/2.0/pcs/file", {
                "method": "locateupload",
                "appid": "250528",
                "access_token": token,
                "path": remote_path,
                "uploadid": uploadid,
                "upload_version": "2.0",
            }, base=self.pcs_base)
        except PanError:
            return self.pcs_base
        servers = data.get("servers") or []
        for entry in servers:
            server = (entry or {}).get("server")
            if server:
                return self._as_url(server)
        if data.get("host"):
            return self._as_url(data["host"])
        return self.pcs_base

    @staticmethod
    def _as_url(server):
        server = (server or "").strip()
        if not server:
            return self.pcs_base
        if server.startswith("http://") or server.startswith("https://"):
            return server.rstrip("/")
        return "https://" + server.rstrip("/")

    def upload_bytes(self, remote_path, data):
        """上传一段内容：预上传 -> 分片 -> 创建。三步，少一步文件就是坏的。"""
        size = len(data)
        blocks = [data[i:i + CHUNK_SIZE] for i in range(0, size, CHUNK_SIZE)] or [b""]
        block_list = json.dumps([hashlib.md5(block).hexdigest() for block in blocks])
        token = self.ensure_token()
        precreate = self._api("/rest/2.0/xpan/file",
                               {"method": "precreate", "access_token": token},
                               method="POST",
                               form={"path": remote_path, "size": str(size), "isdir": "0",
                                     "autoinit": "1", "rtype": "3",
                                     "block_list": block_list})
        uploadid = precreate.get("uploadid")
        if not uploadid:
            raise PanError("预上传没给 uploadid：%s" % precreate)
        host = self.upload_host(remote_path, uploadid)
        for index, block in enumerate(blocks):
            self._upload_block(remote_path, uploadid, index, block, token, host)
        created = self._api("/rest/2.0/xpan/file",
                             {"method": "create", "access_token": token},
                             method="POST",
                             form={"path": remote_path, "size": str(size), "isdir": "0",
                                   "uploadid": uploadid, "block_list": block_list,
                                   "rtype": "3"})
        return created

    def _upload_block(self, remote_path, uploadid, index, block, token, host):
        """传一个分片。

        官方文档里 path / uploadid / partseq / type 都是**查询参数**，请求体里只有 file。
        （照着文档写，别照着别人的 SDK 抄 —— 那是旧的参数位置。）
        """
        boundary = "----teapan" + uuid.uuid4().hex
        parts = []
        parts.append(("--" + boundary + "\r\n").encode("utf-8"))
        parts.append(('Content-Disposition: form-data; name="file"; filename="%s"\r\n'
                      % os.path.basename(remote_path)).encode("utf-8"))
        parts.append(b"Content-Type: application/octet-stream\r\n\r\n")
        parts.append(block + b"\r\n")
        parts.append(("--" + boundary + "--\r\n").encode("utf-8"))
        body = b"".join(parts)
        url = (host + "/rest/2.0/pcs/superfile2?"
               + urllib.parse.urlencode({"method": "upload", "access_token": token,
                                         "type": "tmpfile", "path": remote_path,
                                         "uploadid": uploadid, "partseq": index}))
        raw = self._http(url, method="POST", body=body, headers={
            "Content-Type": "multipart/form-data; boundary=" + boundary,
            "User-Agent": "pan.baidu.com",
        })
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise PanError("分片上传返回的不是 JSON：%s" % raw[:200])
        if "md5" not in data:
            raise PanError("分片 %d 上传失败：%s" % (index, data))

    def download_bytes(self, remote_path):
        """下载：先问下载地址（dlink），再顺着它把文件拿下来。"""
        token = self.ensure_token()
        entry = self.find(remote_path)
        dlink = None
        if entry and entry.get("fs_id"):
            data = self._api("/rest/2.0/xpan/multimedia", {
                "method": "filemetas",
                "access_token": token,
                "fsids": json.dumps([entry["fs_id"]]),
                "dlink": 1,
                "thumb": 0,
                "extra": 0,
                "needmedia": 0,
            })
            items = data.get("list") or []
            if items:
                dlink = items[0].get("dlink")
        if not dlink:
            # 退一步：按路径查元信息（某些账号下 fs_id 拿不到）
            data = self._api("/rest/2.0/xpan/file", {
                "method": "meta",
                "access_token": token,
                "path": remote_path,
                "dlink": 1,
            })
            dlink = data.get("dlink")
        if not dlink:
            raise PanError("拿不到 %s 的下载地址" % remote_path)
        if "access_token=" not in dlink:
            joiner = "&" if "?" in dlink else "?"
            dlink = dlink + joiner + "access_token=" + urllib.parse.quote(token)
        return self._http(dlink, headers={"User-Agent": "pan.baidu.com"})

    def delete_paths(self, paths):
        """删文件或目录（清理网盘上的旧版本用）。

        opera 在**查询串**里，filelist 是路径的 JSON 数组（在表单里）——
        照官方 SDK 的 location_map 写。
        """
        if isinstance(paths, str):
            paths = [paths]
        self._api("/rest/2.0/xpan/file",
                  {"method": "filemanager", "opera": "delete",
                   "access_token": self.ensure_token()},
                  method="POST",
                  form={"async": "0", "filelist": json.dumps(list(paths))})

    def delete_path(self, path):
        self.delete_paths([path])

    def user_info(self):
        return self._api("/rest/2.0/xpan/nas", {
            "method": "uinfo",
            "access_token": self.ensure_token(),
        })


# ---------------------------------------------------------------------------
# 同步
# ---------------------------------------------------------------------------
class Syncer(object):
    """把 tea home 里的配置和网盘上的那份对齐。

    判定用的三样东西：
      * 本地快照：每个配置文件的 sha256；
      * 远端清单：上次谁在什么时候推的、里面有什么；
      * 本地状态：上次同步完两边是什么样（state.json）。
    两边都比上次动过 = 冲突，停手让人来定。
    """

    def __init__(self, config, client=None, home=None):
        self.config = config
        self.tea_home = config.tea_home()
        self.remote_dir = config.remote_dir()
        # 每台机器一份状态（同一个网盘账号可以有好几台机器）。
        self.home = home or PAN_HOME
        self.state_path = os.path.join(self.home, "state.json")
        self.backup_root = os.path.join(self.home, "backup")
        self.client = client or PanClient(
            config.text("app_key"), config.text("app_secret"),
            token_path=os.path.join(self.home, "token.json"),
            api_base=config.text("api_base"), oauth_base=config.text("oauth_base"),
            pcs_base=config.text("pcs_base"), timeout=config.integer("timeout"))

    # -- 本地 ---------------------------------------------------------------
    def local_paths(self):
        out = []
        for name in self.config.file_list():
            out.append((name, os.path.join(self.tea_home, name)))
        return out

    def snapshot(self):
        """本地每个配置文件的 sha256（文件不在就是 None）。"""
        result = {}
        for name, path in self.local_paths():
            if os.path.isfile(path):
                result[name] = {"sha256": sha256_file(path), "size": os.path.getsize(path)}
            else:
                result[name] = None
        return result

    def load_state(self):
        if not os.path.exists(self.state_path):
            return {}
        try:
            with open(self.state_path, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except (ValueError, OSError):
            return {}

    def save_state(self, state):
        ensure_dir(os.path.dirname(self.state_path))
        with open(self.state_path, "w", encoding="utf-8") as handle:
            json.dump(state, handle, sort_keys=True, ensure_ascii=False, indent=2)

    def local_changed(self, snapshot, state):
        """本地相对上次同步动过的文件。

        两种算「动过」：哈希和上次不一样；或者本地有、**远端那份里根本没有**（还没推上去）。
        第二种别漏 —— 漏了它就永远传不上去（拉回来时它不在包里，却被记成「同步过了」）。
        """
        base = state.get("base") or {}
        remote_files = state.get("remote_files")
        changed = []
        for name, info in snapshot.items():
            now = info["sha256"] if info else None
            before = base.get(name)
            if now != before:
                changed.append(name)
            elif remote_files is not None and now is not None and name not in remote_files:
                changed.append(name)
        return changed

    def first_run(self, state):
        """这台机器还没同步过一次（state.json 都不在）。"""
        return not os.path.exists(self.state_path)

    # -- 远端 ---------------------------------------------------------------
    def remote_manifest(self):
        """远端的清单（明文）。没有就是 None。"""
        try:
            raw = self.client.download_bytes(self.remote_dir + "/" + MANIFEST_NAME)
        except PanError:
            return None, None
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None, None
        return raw, data

    def remote_changed(self, raw_manifest, state):
        if raw_manifest is None:
            return False
        return sha256_bytes(raw_manifest) != state.get("last_manifest_sha")

    # -- 打包 / 解包 --------------------------------------------------------
    def build_bundle(self):
        entries = []
        for name, path in self.local_paths():
            if os.path.isfile(path):
                with open(path, "rb") as handle:
                    entries.append((name, handle.read()))
        return entries

    def passphrase(self):
        text = self.config.text("passphrase") or os.environ.get("TEA_PAN_PASSPHRASE", "")
        if text:
            return text
        if self.config.boolean("allow_plain"):
            return None
        raise ConfigError("没有加密口令：provider.yaml 里有 API key，明文传上去等于把"
                          "钥匙挂在门上。\n在配置里写 passphrase: <你自己的口令>，"
                          "或者确认不要加密就写 allow_plain: yes。")

    def encrypt(self, data):
        secret = self.passphrase()
        if secret is None:
            return data
        return encrypt_blob(data, secret)

    def decrypt(self, data):
        secret = self.passphrase()
        if secret is None:
            return data
        return decrypt_blob(data, secret)

    def require_token(self):
        """推/拉之前先确认令牌在。缺令牌要说清楚，而不是报「远端没有清单」。"""
        self.client.ensure_token()

    # -- 动作 ---------------------------------------------------------------
    def push(self, dry_run=False, force=False):
        report = {"action": "push", "pushed": [], "changed": [], "skipped": [],
                  "conflict": False, "notes": [], "remote": self.remote_dir}
        self.passphrase()          # 没有口令就别想推（早说，别推到一半才说）
        self.require_token()       # 没授权就说没授权
        snapshot = self.snapshot()
        state = self.load_state()
        changed = self.local_changed(snapshot, state)
        raw_manifest, manifest = self.remote_manifest()
        # 同步范围变了（比如把 workspaces.json 从 files: 里去掉）也算「动过」：
        # 不重推一份，远端包里就还留着上次范围内的旧文件。
        wanted = sorted(self.config.file_list())
        scope_changed = state.get("files") != wanted
        moved = list(changed) + ([] if not scope_changed else ["(同步范围变了)"])

        if self.remote_changed(raw_manifest, state) and moved and not force:
            report["conflict"] = True
            if self.first_run(state):
                report["notes"].append(
                    "这是第一次同步，而远端已经有一份配置了（%s 在 %s 推的），"
                    "本地也有自己的一份。先 pull 收下远端那份，还是确定要用本地这份覆盖远端"
                    "（--force）？"
                    % (manifest.get("machine", "?"), manifest.get("updated_at", "?")))
            else:
                report["notes"].append("远端在上次同步之后也动过（%s），本地也动过（%s）。"
                                       "两边都改了就不知道谁对，先看看 status，"
                                       "确定要覆盖远端再加 --force。"
                                       % (manifest.get("machine", "?"), "、".join(moved)))
            return report

        report["changed"] = list(changed)
        if scope_changed:
            report["notes"].append("同步范围变了（现在同步：%s），要重推一份。" % "、".join(wanted))
        entries = self.build_bundle()
        if not entries:
            report["notes"].append("本地没有可同步的文件（%s）"
                                   % "、".join(self.config.file_list()))
            return report
        if not changed and not scope_changed and not force:
            report["notes"].append("本地跟上次的同步一模一样，没什么要推的。")
            return report

        packed, header = pack_bundle(entries, self.config.machine())
        blob = self.encrypt(packed)
        if dry_run:
            report["pushed"] = [name for name, _ in entries]
            report["notes"].append("（dry-run：没有真的上传）")
            return report

        self.client.ensure_dir(self.remote_dir)
        # 先传包，再传清单：清单指向的包必须已经在那儿了。
        self.client.upload_bytes(self.remote_dir + "/" + BUNDLE_NAME, blob)
        manifest = {
            "version": 1,
            "updated_at": iso_now(),
            "machine": self.config.machine(),
            "bundle": BUNDLE_NAME,
            "bundle_sha256": sha256_bytes(blob),
            "files": {name: {"sha256": sha256_bytes(data), "size": len(data)}
                      for name, data in entries},
        }
        raw = json.dumps(manifest, sort_keys=True, ensure_ascii=False, indent=2).encode("utf-8")
        self.client.upload_bytes(self.remote_dir + "/" + MANIFEST_NAME, raw)

        new_state = dict(state)
        new_state["version"] = 1
        new_state["machine"] = self.config.machine()
        new_state["last_sync_at"] = iso_now()
        new_state["last_manifest_sha"] = sha256_bytes(raw)
        base = dict(new_state.get("base") or {})
        for name, data in entries:
            base[name] = sha256_bytes(data)
        for name in list(base):
            if not snapshot.get(name):
                # 本地删掉了：别记着旧哈希，不然它永远算「动过」
                del base[name]
        new_state["base"] = base
        new_state["remote_files"] = sorted(manifest["files"])
        new_state["files"] = wanted
        self.save_state(new_state)
        report["pushed"] = [name for name, _ in entries]
        report["notes"].append("推了 %d 个文件到 %s" % (len(entries), self.remote_dir))
        return report

    def pull(self, dry_run=False, force=False):
        report = {"action": "pull", "applied": [], "unchanged": [], "changed": [],
                  "skipped": [], "conflict": False, "notes": [], "remote": self.remote_dir}
        self.passphrase()          # 没有口令就解不开远端那份
        self.require_token()       # 没授权就说没授权
        snapshot = self.snapshot()
        state = self.load_state()
        changed = self.local_changed(snapshot, state)
        raw_manifest, manifest = self.remote_manifest()
        if raw_manifest is None:
            report["notes"].append("远端还没有清单（%s）：先在另一台机器上 push。" % self.remote_dir)
            return report
        if not self.remote_changed(raw_manifest, state) and not force:
            report["notes"].append("远端跟上次的同步一模一样，没什么要拉的。")
            report["unchanged"] = sorted(manifest.get("files", {}).keys())
            return report
        if changed and not force and not self.first_run(state):
            report["conflict"] = True
            report["notes"].append("本地在上次同步之后动过（%s），远端也动过"
                                   "（%s）。两边都改了就不知道谁对："
                                   "先 status 看看，确认要听远端的再加 --force。"
                                   % ("、".join(changed), manifest.get("machine", "?")))
            return report
        if changed and self.first_run(state):
            report["notes"].append("第一次同步：远端那份会盖掉本地的 %s"
                                   "（旧的备份在 %s）。"
                                   % ("、".join(changed), self.backup_root))

        packed = self.decrypt(self.client.download_bytes(self.remote_dir + "/" + BUNDLE_NAME))
        header, files = unpack_bundle(packed)
        report["notes"].append("远端是 %s 在 %s 推的" % (header.get("machine", "?"),
                                                        header.get("created_at", "?")))
        base = dict(state.get("base") or {})
        wanted = set(self.config.file_list())
        for name, data in files:
            if name not in wanted:
                # 远端包里有、但这次的同步范围里没有 → **不写**。
                # （比如远端存着上一版的 workspaces.json：那是每台机器自己的，
                #   写过去只会把另一台的工作区清单覆盖成不存在的路径。）
                report["skipped"].append(name)
                continue
            digest = sha256_bytes(data)
            local_path = os.path.join(self.tea_home, name)
            current = None
            if os.path.isfile(local_path):
                current = sha256_file(local_path)
            if current == digest:
                report["unchanged"].append(name)
                base[name] = digest
                continue
            if dry_run:
                report["applied"].append(name)
                base[name] = digest
                continue
            self._backup(local_path)
            ensure_dir(os.path.dirname(local_path))
            with open(local_path, "wb") as handle:
                handle.write(data)
            try:
                os.chmod(local_path, 0o600)
            except OSError:
                pass
            report["applied"].append(name)
            base[name] = digest
        if not dry_run:
            new_state = dict(state)
            new_state["version"] = 1
            new_state["machine"] = self.config.machine()
            new_state["last_sync_at"] = iso_now()
            new_state["last_manifest_sha"] = sha256_bytes(raw_manifest)
            # 远端没有的文件（比如只有这台机器才有的 mcp.yaml）：记住本地这份，
            # 不然它会永远被当成「本地改过」。
            for name, info in snapshot.items():
                if name not in base and info:
                    base[name] = info["sha256"]
            new_state["base"] = base
            new_state["remote_files"] = sorted(manifest.get("files", {}).keys())
            new_state["files"] = sorted(self.config.file_list())
            self.save_state(new_state)
        if report["skipped"]:
            report["notes"].append("远端包里有 %s，但不在这次的同步范围里，没写。"
                                   % "、".join(report["skipped"]))
        if report["applied"]:
            report["notes"].append("写了 %d 个文件（旧的备份在 %s）"
                                   % (len(report["applied"]), self.backup_root))
        else:
            report["notes"].append("远端和本地已经一样了。")
        return report

    def _backup(self, path):
        if not os.path.isfile(path):
            return None
        stamp = time.strftime("%Y%m%d-%H%M%S")
        target = os.path.join(self.backup_root, stamp, os.path.basename(path))
        ensure_dir(os.path.dirname(target))
        shutil.copy2(path, target)
        return target

    def sync(self, dry_run=False, force=False):
        """先拉后推：远端有更新就收下，本地有更新就推上去。

        顺序不能反 —— 先推会把远端还没收下来的更新盖掉。
        """
        pulled = self.pull(dry_run=dry_run, force=force)
        if pulled["conflict"]:
            pulled["action"] = "sync"
            return pulled
        pushed = self.push(dry_run=dry_run, force=force)
        pushed["action"] = "sync"
        pushed["pulled"] = pulled.get("applied", [])
        pushed["notes"] = list(pulled.get("notes", [])) + list(pushed.get("notes", []))
        if pulled.get("conflict"):
            pushed["conflict"] = True
        return pushed

    def status(self):
        report = {"action": "status", "remote": self.remote_dir, "notes": []}
        state = self.load_state()
        snapshot = self.snapshot()
        report["local"] = {name: (info["sha256"][:12] if info else None)
                           for name, info in snapshot.items()}
        report["changed"] = self.local_changed(snapshot, state)
        report["last_sync_at"] = state.get("last_sync_at")
        try:
            self.require_token()
        except PanError as exc:
            report["remote_state"] = "远端：%s" % exc
            report["remote_manifest"] = None
            report["remote_changed"] = False
            report["conflict"] = False
            return report
        raw_manifest, manifest = self.remote_manifest()
        if raw_manifest is None:
            report["remote_state"] = "远端还没有清单"
            report["remote_manifest"] = None
            report["remote_changed"] = False
            report["conflict"] = False
            return report
        report["remote_state"] = "远端是 %s 在 %s 推的" % (manifest.get("machine", "?"),
                                                          manifest.get("updated_at", "?"))
        report["remote_manifest"] = manifest
        report["remote_changed"] = self.remote_changed(raw_manifest, state)
        report["conflict"] = bool(report["remote_changed"] and report["changed"])
        if report["conflict"]:
            report["notes"].append("冲突：两边都动过了。定下来哪边对，再 push/pull。")
        return report


# ---------------------------------------------------------------------------
# 命令
# ---------------------------------------------------------------------------
def render_report(report):
    lines = []
    action = report.get("action", "")
    if action == "status":
        lines.append("远端目录：%s" % report.get("remote"))
        lines.append("本地：")
        for name, digest in sorted((report.get("local") or {}).items()):
            lines.append("  %-20s %s" % (name, digest or "（本地没有）"))
        lines.append("上次同步：%s" % (report.get("last_sync_at") or "（从来没有）"))
        lines.append("远端：%s" % report.get("remote_state"))
        if report.get("remote_changed"):
            lines.append("远端比上次同步新。")
        if report.get("changed"):
            lines.append("本地动过的文件：%s" % "、".join(report["changed"]))
        if report.get("conflict"):
            lines.append("⚠ 冲突：两边都动过了。")
        for note in report.get("notes", []):
            lines.append("· %s" % note)
        return "\n".join(lines)

    for key, label in (("changed", "改过的"), ("pushed", "推上去"),
                       ("applied", "写下来"), ("pulled", "从远端收到"),
                       ("unchanged", "没变"), ("skipped", "跳过")):
        items = report.get(key) or []
        if items:
            lines.append("%s：%s" % (label, "、".join(items)))
    if report.get("conflict"):
        lines.append("⚠ 冲突：两边都改过了 —— 我没有覆盖任何一边。")
    for note in report.get("notes", []):
        lines.append("· %s" % note)
    return "\n".join(lines) if lines else "没什么可做的。"


def cmd_auth(config, args):
    client = PanClient(config.text("app_key"), config.text("app_secret"),
                       api_base=config.text("api_base"),
                       oauth_base=config.text("oauth_base"),
                       pcs_base=config.text("pcs_base"),
                       timeout=config.integer("timeout"))
    if not config.text("app_key") or not config.text("app_secret"):
        raise ConfigError("配置里没有 app_key / app_secret：\n"
                          "  1) 打开 https://pan.baidu.com/union/console/createapp 建个应用\n"
                          "  2) 把 AppKey / SecretKey 写进 %s" % config.path)
    info = client.device_code()
    url = info.get("verification_url") or "https://openapi.baidu.com/device"
    print("打开这个网址，输用户码：%s" % url)
    print("用户码：%s" % info.get("user_code", ""))
    if info.get("qrcode_url"):
        print("或者用手机扫这个二维码：%s" % info["qrcode_url"])
    sys.stdout.flush()
    interval = max(5, int(info.get("interval", 5)))
    deadline = time.time() + float(info.get("expires_in", 600))
    while time.time() < deadline:
        time.sleep(interval)
        try:
            token = client.device_token(info["device_code"])
        except PanError as exc:
            text = str(exc).lower()
            # 用户还没在手机上点「同意」：继续等（官方说轮询间隔不能低于 5 秒）。
            if "authorization_pending" in text or "pending" in text \
                    or "slow down" in text or "not authorized" in text:
                continue
            if "expired" in text or "invalid" in text:
                raise PanError("设备码过期了：再跑一次 auth")
            raise
        print("授权好了。令牌存在 %s（有效期 %d 天）。"
              % (client.token_path, int(float(token.get("expires_at", 0) - time.time()) / 86400)))
        return 0
    raise PanError("等太久了，设备码过期了：再跑一次 auth")


def cmd_check(config, args):
    lines = []
    ok = True
    try:
        config.file_list()
        lines.append("· 配置：%s" % config.path)
    except ConfigError as exc:
        ok = False
        lines.append("✗ %s" % exc)
    lines.append("· 同步的文件：%s" % "、".join(config.file_list() if ok else []))
    lines.append("· 本地 tea home：%s" % config.tea_home())
    lines.append("· 远端目录：%s" % config.remote_dir())
    try:
        secret = config.text("passphrase") or os.environ.get("TEA_PAN_PASSPHRASE", "")
        if secret:
            lines.append("· 加密：开（口令来自配置）")
        elif config.boolean("allow_plain"):
            ok = False
            lines.append("✗ 加密：关 —— allow_plain: yes 明文上传，provider.yaml 里有 API key")
        else:
            ok = False
            lines.append("✗ 加密：没有口令，推不上去")
    except ConfigError as exc:
        ok = False
        lines.append("✗ %s" % exc)
    client = PanClient(config.text("app_key"), config.text("app_secret"),
                       api_base=config.text("api_base"),
                       oauth_base=config.text("oauth_base"),
                       pcs_base=config.text("pcs_base"),
                       timeout=config.integer("timeout"))
    try:
        info = client.user_info()
        lines.append("· 网盘：通（%s）" % (info.get("username") or info.get("uk") or "?"))
    except PanError as exc:
        ok = False
        lines.append("✗ 网盘：%s" % exc)
    print("\n".join(lines))
    return 0 if ok else 1


def cmd_watch(config, args):
    interval = args.interval or config.integer("interval")
    print("每 %d 秒同步一次（Ctrl-C 停）。" % interval)
    while True:
        try:
            report = Syncer(config).sync(dry_run=args.dry_run, force=args.force)
            sys.stdout.write(render_report(report) + "\n")
            sys.stdout.flush()
            log_line(render_report(report))
        except (PanError, ConfigError) as exc:
            sys.stdout.write("同步失败：%s\n" % exc)
            sys.stdout.flush()
            log_line("同步失败：%s" % exc)
        except KeyboardInterrupt:
            print("\n停了。")
            return 0
        time.sleep(max(30, interval))


def log_line(text):
    try:
        ensure_dir(os.path.dirname(LOG_PATH))
        with open(LOG_PATH, "a", encoding="utf-8") as handle:
            handle.write("%s %s\n" % (iso_now(), text.replace("\n", " | ")))
    except OSError:
        pass


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="pan_sync.py",
        description="百度网盘：多台机器共用一份 tea_a2 配置")
    parser.add_argument("--config", help="配置文件（默认 ~/.tea_agent/pan-sync/config）")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("auth", help="授权一次（设备码）")
    sub.add_parser("status", help="本地和远端现在各是什么")
    sub.add_parser("check", help="配置、令牌、网盘通不通")
    for name, help_text in (("push", "把本地配置推上去"),
                            ("pull", "把远端配置拉下来"),
                            ("sync", "先拉后推")):
        item = sub.add_parser(name, help=help_text)
        item.add_argument("--dry-run", action="store_true", help="只看会做什么")
        item.add_argument("--force", action="store_true", help="两边都改过时也覆盖")
    watch = sub.add_parser("watch", help="常驻，每隔一会儿同步一次")
    watch.add_argument("--interval", type=int, help="间隔秒数")
    watch.add_argument("--dry-run", action="store_true")
    watch.add_argument("--force", action="store_true")

    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 2

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    try:
        if args.command == "auth":
            return cmd_auth(config, args)
        if args.command == "check":
            return cmd_check(config, args)
        if args.command == "status":
            report = Syncer(config).status()
            print(render_report(report))
            return 0
        if args.command == "push":
            report = Syncer(config).push(dry_run=args.dry_run, force=args.force)
            print(render_report(report))
            return 1 if report.get("conflict") else 0
        if args.command == "pull":
            report = Syncer(config).pull(dry_run=args.dry_run, force=args.force)
            print(render_report(report))
            return 1 if report.get("conflict") else 0
        if args.command == "sync":
            report = Syncer(config).sync(dry_run=args.dry_run, force=args.force)
            print(render_report(report))
            return 1 if report.get("conflict") else 0
        if args.command == "watch":
            return cmd_watch(config, args)
    except (PanError, ConfigError) as exc:
        print("出错了：%s" % exc, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
