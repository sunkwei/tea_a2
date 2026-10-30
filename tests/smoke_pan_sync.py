#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""百度网盘配置同步：协议、加密、冲突、备份、两台机器。

真的百度网盘不碰（那要一个应用、一份授权，而且谁也不想测试往自己网盘里写东西）。
这里起一个**假的开放平台**：设备码、令牌、列目录、建目录、预上传、分片上传、
创建文件、下载地址，一个不落地实现 —— 脚本怎么调它，这里就怎么答它。

钉住的是：

  * 配置格式：注释、`|` 块、不认识的键要带行号报错（拼错不能悄悄不生效）；
  * 加密：口令错就解不开、篡改一个字节就验不过、密文里没有明文；
  * 打包：文件名要干净（不许 ../）、校验对不上要报错；
  * 授权：设备码换令牌、令牌落盘是 0600、过期会用 refresh_token 续；
  * 推送：先传包再传清单、清单里写的是谁在什么时候推的；
  * 拉取：写之前先备份、写出来的文件就是远端那份；
  * 冲突：两边都改了就停手（谁也不覆盖），--force 才覆盖；
  * 删除不传播：本地删了文件，不会把远端的也删掉；
  * 两台机器：一台推，一台拉，拉完两边的 provider.yaml 一模一样。

用法：python3 tests/smoke_pan_sync.py
"""

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
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

TMP_HOME = tempfile.mkdtemp(prefix="tea_pan_sync_")
os.environ["TEA_PAN_HOME"] = os.path.join(TMP_HOME, "pan-home")
os.environ.pop("TEA_PAN_CONFIG", None)

import pan_sync  # noqa: E402


# ---------------------------------------------------------------------------
# 假的百度网盘
# ---------------------------------------------------------------------------
class FakePan(object):
    """一个假的网盘：目录、文件、上传中的分片。"""

    def __init__(self):
        self.dirs = set(["/"])
        self.files = {}          # path -> bytes
        self.ids = {}            # path -> fs_id
        self.uploads = {}        # uploadid -> {partseq: bytes}
        self.counter = 0
        self.calls = []

    def fs_id_for(self, path):
        if path not in self.ids:
            self.ids[path] = self.next_id()
        return self.ids[path]

    def next_id(self):
        self.counter += 1
        return 1000 + self.counter

    def ensure_dir(self, path):
        parts = [p for p in path.split("/") if p]
        current = ""
        for part in parts:
            current += "/" + part
            self.dirs.add(current)

    def list_dir(self, path):
        prefix = path if path.endswith("/") else path + "/"
        out = []
        for directory in sorted(self.dirs):
            if directory == path:
                continue
            if not directory.startswith(prefix):
                continue
            rest = directory[len(prefix):]
            if "/" in rest:
                continue
            out.append({"path": directory, "server_filename": rest,
                        "fs_id": self.fs_id_for(directory), "isdir": 1, "size": 0})
        for name, data in sorted(self.files.items()):
            if not name.startswith(prefix):
                continue
            rest = name[len(prefix):]
            if "/" in rest:
                continue
            out.append({"path": name, "server_filename": rest,
                        "fs_id": self.fs_id_for(name), "isdir": 0, "size": len(data)})
        return out


PAN = FakePan()


def _parse_multipart(body, boundary):
    """把 multipart/form-data 拆成 {字段: 值} 和 文件内容。"""
    fields = {}
    blob = None
    marker = ("--" + boundary).encode("utf-8")
    for chunk in body.split(marker):
        chunk = chunk.strip(b"\r\n")
        if not chunk or chunk == b"--":
            continue
        head, _, data = chunk.partition(b"\r\n\r\n")
        headers = head.decode("utf-8", "replace")
        name = None
        for line in headers.split("\r\n"):
            if line.lower().startswith("content-disposition"):
                for piece in line.split(";"):
                    piece = piece.strip()
                    if piece.startswith("name="):
                        name = piece[5:].strip('"')
        if name is None:
            continue
        if 'filename="' in headers:
            blob = data
        else:
            fields[name] = data.decode("utf-8", "replace")
    return fields, blob


class FakePanHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *args):
        pass

    # -- helpers ------------------------------------------------------------
    def _send(self, payload, code=200, content_type="application/json"):
        if isinstance(payload, (dict, list)):
            payload = json.dumps(payload)
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _query(self):
        parsed = urllib.parse.urlparse(self.path)
        return parsed, urllib.parse.parse_qs(parsed.query)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _form(self):
        raw = self._body().decode("utf-8", "replace")
        return {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}

    # -- routing ------------------------------------------------------------
    def do_GET(self):
        parsed, query = self._query()
        path = parsed.path
        PAN.calls.append(("GET", path))
        if path == "/oauth/2.0/device/code":
            return self._send({"device_code": "dev-1", "user_code": "WXYZ",
                               "verification_url": "https://openapi.baidu.com/device",
                               "qrcode_url": "https://openapi.baidu.com/qr?code=WXYZ",
                               "expires_in": 600, "interval": 5})
        if path == "/oauth/2.0/token":
            grant = (query.get("grant_type") or [""])[0]
            if grant == "device_token":
                PAN.device_polls = getattr(PAN, "device_polls", 0) + 1
                if PAN.device_polls == 1:
                    # 用户还没在手机上点「同意」
                    return self._send({"error": "authorization_pending",
                                       "error_description": "need authorization"})
                return self._send({"access_token": "acc-1", "refresh_token": "ref-1",
                                   "expires_in": 2592000, "scope": "basic netdisk"})
            if grant == "refresh_token":
                return self._send({"access_token": "acc-2", "refresh_token": "ref-2",
                                   "expires_in": 2592000, "scope": "basic netdisk"})
            return self._send({"error": "unsupported_grant_type"}, code=400)
        if path == "/rest/2.0/xpan/nas":
            return self._send({"errno": 0, "uk": 12345, "username": "fake-user",
                               "avatar_url": "", "baidu_name": "假用户"})
        if path == "/rest/2.0/xpan/file" and (query.get("method") or [""])[0] == "list":
            target = (query.get("dir") or ["/"])[0]
            return self._send({"errno": 0, "list": PAN.list_dir(target)})
        if path == "/rest/2.0/xpan/multimedia":
            fsids = json.loads((query.get("fsids") or ["[]"])[0])
            out = []
            for name, data in PAN.files.items():
                fs_id = PAN.fs_id_for(name)
                if fs_id not in fsids:
                    continue
                out.append({"fs_id": fs_id, "md5": "0" * 32,
                            "dlink": "http://127.0.0.1:%d/download%s"
                                     % (self.server.server_address[1],
                                        urllib.parse.quote(name))})
            return self._send({"errno": 0, "list": out})
        if path.startswith("/download"):
            name = urllib.parse.unquote(path[len("/download"):])
            if name not in PAN.files:
                return self._send({"errno": 7}, code=404)
            return self._send(PAN.files[name], content_type="application/octet-stream")
        return self._send({"errno": -6}, code=404)

    def do_POST(self):
        parsed, query = self._query()
        path = parsed.path
        PAN.calls.append(("POST", path))
        if path == "/rest/2.0/xpan/file":
            method = (query.get("method") or [""])[0]
            form = self._form()
            target = form.get("path", "")
            if method == "create" and form.get("isdir") == "1":
                if target in PAN.dirs:
                    return self._send({"errno": -8, "errmsg": "目录已存在"})
                PAN.ensure_dir(target)
                return self._send({"errno": 0, "fs_id": PAN.next_id(), "path": target,
                                   "isdir": 1, "category": 6})
            if method == "create":
                uploadid = form.get("uploadid", "")
                blocks = PAN.uploads.get(uploadid, {})
                data = b"".join(blocks[key] for key in sorted(blocks, key=int))
                PAN.files[target] = data
                return self._send({"errno": 0, "fs_id": PAN.next_id(), "path": target,
                                   "isdir": 0, "size": len(data)})
            if method == "filemanager":
                opera = (query.get("opera") or [""])[0]
                if opera == "delete":
                    raw = form.get("filelist", "[]")
                    for target in json.loads(raw):
                        PAN.files.pop(target, None)
                        PAN.dirs.discard(target)
                        PAN.ids.pop(target, None)
                        for name in list(PAN.files):
                            if name.startswith(target.rstrip("/") + "/"):
                                PAN.files.pop(name, None)
                        for name in list(PAN.dirs):
                            if name.startswith(target.rstrip("/") + "/"):
                                PAN.dirs.discard(name)
                    return self._send({"errno": 0})
                return self._send({"errno": -6})
            if method == "precreate":
                uploadid = "up-%d" % PAN.next_id()
                PAN.uploads[uploadid] = {}
                return self._send({"errno": 0, "uploadid": uploadid, "path": target})
            return self._send({"errno": -6})
        if path == "/rest/2.0/pcs/file" and (query.get("method") or [""])[0] == "locateupload":
            host = "127.0.0.1:%d" % self.server.server_address[1]
            return self._send({"error_code": 0, "host": host,
                               "servers": [{"server": host}], "expire": 3600})
        if path == "/rest/2.0/pcs/superfile2":
            content_type = self.headers.get("Content-Type") or ""
            boundary = ""
            for piece in content_type.split(";"):
                piece = piece.strip()
                if piece.startswith("boundary="):
                    boundary = piece[len("boundary="):].strip('"')
            fields, blob = _parse_multipart(self._body(), boundary)
            # 官方文档：path / uploadid / partseq / type 都是**查询参数**，body 里只有 file
            uploadid = (query.get("uploadid") or [""])[0]
            partseq = (query.get("partseq") or ["0"])[0]
            if (query.get("method") or [""])[0] != "upload":
                return self._send({"errno": -6})
            PAN.uploads.setdefault(uploadid, {})[partseq] = blob or b""
            return self._send({"md5": hashlib.md5(blob or b"").hexdigest()})
        return self._send({"errno": -6}, code=404)


def start_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakePanHandler)
    thread = threading.Thread(target=server.serve_forever)
    thread.daemon = True
    thread.start()
    return server


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def make_config(base_url, home, passphrase="正确的口令", extra=None):
    lines = [
        "app_key: k",
        "app_secret: s",
        "passphrase: %s" % passphrase,
        "profile: test",
        "tea_home: %s" % home,
        "api_base: %s" % base_url,
        "oauth_base: %s" % base_url,
        "pcs_base: %s" % base_url,
        "timeout: 10",
    ]
    if extra:
        lines.extend(extra)
    path = os.path.join(TMP_HOME, "config-%s" % os.path.basename(home))
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    return path


def write_provider(home, key="sk-secret-value", model="deepseek-flash"):
    ensure = os.path.join(home, "provider.yaml")
    if not os.path.isdir(home):
        os.makedirs(home)
    with open(ensure, "w", encoding="utf-8") as handle:
        handle.write("providers:\n  DeepSeek:\n    api_url: https://api.deepseek.com\n"
                     "    api_key: %s\n    models:\n      %s: {}\n" % (key, model))
    return ensure


def main():
    failures = []
    checks = []

    def check(name, ok, detail=""):
        checks.append(name)
        print(("PASS " if ok else "FAIL ") + name + (("  " + str(detail)) if not ok else ""))
        if not ok:
            failures.append(name)

    server = start_server()
    base_url = "http://127.0.0.1:%d" % server.server_address[1]

    # ---- the config format ------------------------------------------------
    parsed = pan_sync.parse_config(
        "# comment\n"
        "app_key: abc\n"
        "note: |\n"
        "  first line\n"
        "  second line\n"
        "allow_plain: yes  # trailing comment\n")
    check("a comment line is skipped", "app_key" in parsed)
    check("a | block keeps its lines", parsed["note"] == "first line\nsecond line",
          parsed.get("note"))
    check("a trailing comment is cut", parsed["allow_plain"] == "yes", parsed.get("allow_plain"))
    check("the config turns strings into booleans",
          pan_sync.Config(parsed).boolean("allow_plain") is True)
    check("the file list is split on commas",
          pan_sync.Config({"files": "a.yaml, b.yaml"}).file_list() == ["a.yaml", "b.yaml"])
    try:
        pan_sync.parse_config("nope: 1\n")
        check("an unknown key is an error", False, "no exception")
    except pan_sync.ConfigError as exc:
        check("an unknown key is an error with the line number",
              "第 1 行" in str(exc) and "nope" in str(exc), str(exc))
    try:
        pan_sync.Config({"files": "../evil"}).file_list()
        check("a file name with a path is refused", False, "no exception")
    except pan_sync.ConfigError as exc:
        check("a file name with a path is refused", "不像文件名" in str(exc), str(exc))

    # ---- the crypto --------------------------------------------------------
    secret = b"provider.yaml with sk-secret-value inside"
    blob = pan_sync.encrypt_blob(secret, "口令")
    check("the ciphertext does not contain the plaintext",
          b"sk-secret-value" not in blob)
    check("the ciphertext round trips",
          pan_sync.decrypt_blob(blob, "口令") == secret)
    try:
        pan_sync.decrypt_blob(blob, "错口令")
        check("a wrong passphrase is refused", False, "no exception")
    except pan_sync.PanError as exc:
        check("a wrong passphrase is refused", "口令不对" in str(exc), str(exc))
    tampered = bytearray(blob)
    tampered[-40] = (tampered[-40] + 1) % 256
    try:
        pan_sync.decrypt_blob(bytes(tampered), "口令")
        check("a tampered package is refused", False, "no exception")
    except pan_sync.PanError as exc:
        check("a tampered package is refused", "坏了" in str(exc), str(exc))

    # ---- the bundle --------------------------------------------------------
    entries = [("provider.yaml", b"a: 1\n"), ("tea_agent.yaml", b"settings: {}\n")]
    packed, header = pan_sync.pack_bundle(entries, "machine-a")
    back, files = pan_sync.unpack_bundle(packed)
    check("the bundle round trips", files == entries, files)
    check("the bundle header says who packed it", back["machine"] == "machine-a")
    check("the bundle header lists the files",
          sorted(f["name"] for f in back["files"]) == ["provider.yaml", "tea_agent.yaml"])
    bad = pan_sync.pack_bundle([("../evil", b"x")], "m")[0]
    try:
        pan_sync.unpack_bundle(bad)
        check("a file name with a path is refused in a bundle", False, "no exception")
    except pan_sync.PanError as exc:
        check("a file name with a path is refused in a bundle", "奇怪的文件名" in str(exc), str(exc))
    broken = bytearray(packed)
    broken[-1] = (broken[-1] + 1) % 256
    try:
        pan_sync.unpack_bundle(bytes(broken))
        check("a bundle whose checksum fails is refused", False, "no exception")
    except Exception as exc:  # noqa: BLE001 - 压缩/校验都可能在这里炸
        check("a bundle whose checksum fails is refused", True, str(exc))

    # ---- machine A: authorize and push -------------------------------------
    home_a = os.path.join(TMP_HOME, "home-a")
    write_provider(home_a)
    with open(os.path.join(home_a, "tea_agent.yaml"), "w", encoding="utf-8") as handle:
        handle.write("settings:\n  peers: {}\n")
    config_a = make_config(base_url, home_a)

    config = pan_sync.load_config(config_a)
    syncer_a = pan_sync.Syncer(config)
    try:
        syncer_a.client.ensure_token()
        check("without a token the sync says so", False, "no exception")
    except pan_sync.PanError as exc:
        check("without a token the sync says so", "还没授权" in str(exc), str(exc))

    # 还没授权的时候，推和拉都要说清楚是「没授权」，而不是「远端是空的」。
    syncer_z = pan_sync.Syncer(pan_sync.load_config(
        make_config(base_url, os.path.join(TMP_HOME, "home-z"))),
        home=os.path.join(TMP_HOME, "pan-z"))
    for name, action in (("push", syncer_z.push), ("pull", syncer_z.pull)):
        try:
            action()
            check("a %s without a token says so" % name, False, "no exception")
        except pan_sync.PanError as exc:
            check("a %s without a token says so" % name, "还没授权" in str(exc), str(exc))
    status = syncer_z.status()
    check("the status without a token says so",
          "没授权" in status["remote_state"], status.get("remote_state"))

    info = syncer_a.client.device_code()
    check("the device code comes with a user code", info.get("user_code") == "WXYZ", info)
    try:
        syncer_a.client.device_token(info["device_code"])
        check("the first poll says the user has not authorized yet", False, "no exception")
    except pan_sync.PanError as exc:
        check("the first poll says the user has not authorized yet",
              "authorization_pending" in str(exc), str(exc))
    token = syncer_a.client.device_token(info["device_code"])
    check("the device code buys a token", token["access_token"] == "acc-1", token)
    token_path = syncer_a.client.token_path
    check("the token is written with 0600",
          oct(os.stat(token_path).st_mode & 0o777) == "0o600",
          oct(os.stat(token_path).st_mode & 0o777))
    check("the token file has the refresh token",
          json.load(open(token_path, encoding="utf-8"))["refresh_token"] == "ref-1")

    report = syncer_a.push()
    check("the push reports the files it sent",
          report["pushed"] == ["provider.yaml", "tea_agent.yaml"], report)
    remote_dir = config.remote_dir()
    check("the bundle is on the fake netdisk",
          remote_dir + "/config.tea" in PAN.files, sorted(PAN.files))
    check("the manifest is on the fake netdisk",
          remote_dir + "/manifest.json" in PAN.files, sorted(PAN.files))
    manifest = json.loads(PAN.files[remote_dir + "/manifest.json"].decode("utf-8"))
    check("the manifest says who pushed it", manifest["machine"] == config.machine(), manifest)
    check("the manifest says when", bool(manifest.get("updated_at")), manifest)
    check("the manifest lists the files",
          sorted(manifest["files"]) == ["provider.yaml", "tea_agent.yaml"], manifest)
    stored = PAN.files[remote_dir + "/config.tea"]
    check("the uploaded package is encrypted", b"sk-secret-value" not in stored)
    plain = pan_sync.decrypt_blob(stored, "正确的口令")
    check("the uploaded package can be unpacked",
          [name for name, _ in pan_sync.unpack_bundle(plain)[1]]
          == ["provider.yaml", "tea_agent.yaml"])

    # ---- status ------------------------------------------------------------
    status = syncer_a.status()
    check("the status shows the remote", "远端" in pan_sync.render_report(status),
          pan_sync.render_report(status))
    check("the status says nothing changed", not status["changed"], status.get("changed"))
    check("the status knows the remote is not ahead", not status["remote_changed"])

    # ---- machine B: pull ---------------------------------------------------
    home_b = os.path.join(TMP_HOME, "home-b")
    if not os.path.isdir(home_b):
        os.makedirs(home_b)
    config_b = make_config(base_url, home_b)
    # 每台机器一份状态：同一个网盘账号可以有好几台机器。
    syncer_b = pan_sync.Syncer(pan_sync.load_config(config_b),
                               home=os.path.join(TMP_HOME, "pan-b"))
    # B 自己也得有令牌（同一个假服务器，同一个账号）
    syncer_b.client.token = syncer_a.client.token
    report = syncer_b.pull()
    check("the pull writes the remote files",
          sorted(report["applied"]) == ["provider.yaml", "tea_agent.yaml"], report)
    with open(os.path.join(home_b, "provider.yaml"), encoding="utf-8") as handle:
        pulled = handle.read()
    with open(os.path.join(home_a, "provider.yaml"), encoding="utf-8") as handle:
        original = handle.read()
    check("the two machines end up with the same provider.yaml", pulled == original)
    check("the pulled file keeps its mode 0600",
          oct(os.stat(os.path.join(home_b, "provider.yaml")).st_mode & 0o777) == "0o600")

    # ---- a second push is a no-op -----------------------------------------
    report = syncer_a.push()
    check("a second push with no change does nothing",
          not report["pushed"] and "一模一样" in " ".join(report["notes"]), report)

    # ---- a change on A, then B pulls it ------------------------------------
    write_provider(home_a, key="sk-new-value")
    report = syncer_a.push()
    check("a changed file is pushed", report["changed"] == ["provider.yaml"], report)
    check("the upload host is asked for (it is dynamic)",
          any(path == "/rest/2.0/pcs/file" for method, path in PAN.calls), PAN.calls[:6])
    check("the pushed set is the whole config",
          sorted(report["pushed"]) == ["provider.yaml", "tea_agent.yaml"], report)
    report = syncer_b.pull()
    check("the other machine pulls the change",
          report["applied"] == ["provider.yaml"], report)
    with open(os.path.join(home_b, "provider.yaml"), encoding="utf-8") as handle:
        check("the pulled change is the new one", "sk-new-value" in handle.read())

    # ---- conflict: both sides change --------------------------------------
    # B 先推（远端动了），A 也改了本地 —— 这时候谁覆盖谁都是猜。
    write_provider(home_b, key="sk-from-b")
    syncer_b.push()
    write_provider(home_a, key="sk-from-a")
    report = syncer_a.push()
    check("a push on a conflicted machine refuses", report["conflict"], report)
    check("the refusal says what to do",
          "冲突" in pan_sync.render_report(report), pan_sync.render_report(report))
    check("the refused push did not touch the remote",
          b"sk-from-a" not in PAN.files[remote_dir + "/config.tea"])
    report = syncer_a.pull()
    check("a pull on a conflicted machine refuses", report["conflict"], report)
    with open(os.path.join(home_a, "provider.yaml"), encoding="utf-8") as handle:
        check("the refused pull did not touch the local file", "sk-from-a" in handle.read())
    report = syncer_b.pull()
    check("the machine that pushed has nothing to pull", not report["conflict"], report)
    status = syncer_a.status()
    check("the status says there is a conflict", status["conflict"], status)

    # ---- force -------------------------------------------------------------
    report = syncer_a.push(force=True)
    check("a forced push goes through", "provider.yaml" in (report.get("pushed") or []), report)
    report = syncer_b.pull(force=True)
    check("a forced pull goes through", "provider.yaml" in (report.get("applied") or []), report)
    with open(os.path.join(home_b, "provider.yaml"), encoding="utf-8") as handle:
        check("the forced pull took the remote version", "sk-from-a" in handle.read())

    # ---- the backup --------------------------------------------------------
    found = []
    for root, _dirs, files in os.walk(syncer_b.backup_root):
        found.extend(files)
    check("the old file was backed up before it was overwritten",
          "provider.yaml" in found, found)

    # ---- dry run -----------------------------------------------------------
    write_provider(home_a, key="sk-dry-run")
    before = PAN.files[remote_dir + "/config.tea"]
    report = syncer_a.push(dry_run=True)
    check("a dry run reports what it would do",
          report["changed"] == ["provider.yaml"], report)
    check("a dry run uploads nothing",
          PAN.files[remote_dir + "/config.tea"] == before)

    # ---- deleting a file does not delete the other machine's copy ----------
    os.remove(os.path.join(home_a, "tea_agent.yaml"))
    report = syncer_a.push()
    check("a deleted file is not in the pushed set",
          "tea_agent.yaml" not in (report.get("pushed") or []), report)
    manifest = json.loads(PAN.files[remote_dir + "/manifest.json"].decode("utf-8"))
    check("the deleted file is gone from the manifest",
          "tea_agent.yaml" not in manifest["files"], manifest)
    report = syncer_b.pull()
    check("a pull does not delete the other machine's file",
          os.path.isfile(os.path.join(home_b, "tea_agent.yaml")), report)

    # ---- a brand new machine ------------------------------------------------
    # 没同步过的机器（state.json 都没有）：拉的时候不该被「冲突」挡住，
    # 但本地那份要先备份；推的时候如果远端已经有配置了，得先问一句。
    home_c = os.path.join(TMP_HOME, "home-c")
    write_provider(home_c, key="sk-mine")
    config_c = make_config(base_url, home_c)
    syncer_c = pan_sync.Syncer(pan_sync.load_config(config_c),
                               home=os.path.join(TMP_HOME, "pan-c"))
    syncer_c.client.token = syncer_a.client.token
    report = syncer_c.pull()
    check("a new machine takes the remote config",
          "provider.yaml" in (report.get("applied") or []), report)
    check("a new machine is told what it lost",
          "第一次同步" in " ".join(report.get("notes", [])), report.get("notes"))
    with open(os.path.join(home_c, "provider.yaml"), encoding="utf-8") as handle:
        check("the new machine ended up with the remote one",
              "sk-mine" not in handle.read())
    found = []
    for root, _dirs, files in os.walk(syncer_c.backup_root):
        found.extend(files)
    check("the new machine's own file was backed up",
          "provider.yaml" in found, found)
    report = syncer_c.push()
    check("a new machine has nothing to push",
          not report.get("pushed"), report)

    # 另一台新机器，本地有、远端没有的文件：下一次 push 要把它带上去。
    home_d = os.path.join(TMP_HOME, "home-d")
    if not os.path.isdir(home_d):
        os.makedirs(home_d)
    with open(os.path.join(home_d, "mcp.yaml"), "w", encoding="utf-8") as handle:
        handle.write("mcp_servers: {}\n")
    config_d = make_config(base_url, home_d)
    syncer_d = pan_sync.Syncer(pan_sync.load_config(config_d),
                               home=os.path.join(TMP_HOME, "pan-d"))
    syncer_d.client.token = syncer_a.client.token
    syncer_d.pull()
    report = syncer_d.push()
    check("a file the remote does not have yet is pushed",
          "mcp.yaml" in (report.get("pushed") or []), report)
    manifest = json.loads(PAN.files[remote_dir + "/manifest.json"].decode("utf-8"))
    check("the pushed file is in the manifest", "mcp.yaml" in manifest["files"], manifest)

    # ---- 同步范围：只同步该同步的 ----------------------------------------
    # workspaces.json 是**每台机器自己的**（本机绝对路径 + 会话计数），
    # 默认不该同步；拉的时候也只写白名单里的文件。
    check("workspaces.json is not in the default list",
          "workspaces.json" not in pan_sync.DEFAULTS["files"],
          pan_sync.DEFAULTS["files"])

    # G：先推一份两个文件的
    home_g = os.path.join(TMP_HOME, "home-g")
    write_provider(home_g, key="sk-g")
    with open(os.path.join(home_g, "tea_agent.yaml"), "w", encoding="utf-8") as handle:
        handle.write("settings: {}\n")
    config_g = make_config(base_url, home_g, extra=["files: provider.yaml, tea_agent.yaml"])
    syncer_g = pan_sync.Syncer(pan_sync.load_config(config_g),
                               home=os.path.join(TMP_HOME, "pan-g"))
    syncer_g.client.token = syncer_a.client.token
    # 远端已经有一份了（新机器第一次推）→ 要 --force 才覆盖
    report = syncer_g.push(force=True)
    check("the two files are pushed",
          sorted(report.get("pushed") or []) == ["provider.yaml", "tea_agent.yaml"], report)

    # F：同步范围里只有 provider.yaml —— 远端包里的 tea_agent.yaml 不能写下来
    home_f = os.path.join(TMP_HOME, "home-f")
    if not os.path.isdir(home_f):
        os.makedirs(home_f)
    config_f = make_config(base_url, home_f, extra=["files: provider.yaml"])
    syncer_f = pan_sync.Syncer(pan_sync.load_config(config_f),
                               home=os.path.join(TMP_HOME, "pan-f"))
    syncer_f.client.token = syncer_a.client.token
    report = syncer_f.pull()
    check("a pull only writes the files in the list",
          report["applied"] == ["provider.yaml"], report)
    check("a file outside the list is not written",
          not os.path.isfile(os.path.join(home_f, "tea_agent.yaml")), report)
    check("a file outside the list is reported",
          "tea_agent.yaml" in (report.get("skipped") or []), report)

    # 把范围改小 → 要重推一份，远端包里不该再有那个文件
    config_g2 = make_config(base_url, home_g, extra=["files: provider.yaml"])
    syncer_g2 = pan_sync.Syncer(pan_sync.load_config(config_g2),
                                home=os.path.join(TMP_HOME, "pan-g"))
    syncer_g2.client.token = syncer_a.client.token
    report = syncer_g2.push()
    check("the scope change makes it push again",
          "provider.yaml" in (report.get("pushed") or []), report)
    manifest = json.loads(PAN.files[remote_dir + "/manifest.json"].decode("utf-8"))
    check("the remote package no longer has the file left out",
          "tea_agent.yaml" not in manifest["files"], manifest)

    # ---- 发布产物：上传 + 回读校验 + 删除 --------------------------------
    import pan_publish

    home_pub = os.path.join(TMP_HOME, "home-pub")
    if not os.path.isdir(home_pub):
        os.makedirs(home_pub)
    package = os.path.join(home_pub, "tea_a2-9.9.9.zip")
    with open(package, "wb") as handle:
        handle.write(b"PK\x03\x04" + b"x" * (5 * 1024 * 1024))   # 5MB：两片
    config_pub = make_config(base_url, home_pub)
    os.environ["TEA_PAN_HOME"] = pan_sync.PAN_HOME
    code = pan_publish.main([package, "--config", config_pub,
                              "--remote-dir", "/tea_a2/releases/9.9.9"])
    check("publishing returns 0", code == 0, code)
    check("the package is on the fake netdisk",
          "/tea_a2/releases/9.9.9/tea_a2-9.9.9.zip" in PAN.files, sorted(PAN.files))
    check("the release manifest is written",
          "/tea_a2/releases/9.9.9/release-manifest.json" in PAN.files, sorted(PAN.files))
    manifest = json.loads(
        PAN.files["/tea_a2/releases/9.9.9/release-manifest.json"].decode("utf-8"))
    check("the manifest knows the version", manifest["version"] == "9.9.9", manifest)
    entry = manifest["files"][0]
    check("the manifest has the sha256", len(entry["sha256"]) == 64, entry)
    check("the manifest has the md5", len(entry["md5"]) == 32, entry)
    check("the manifest counts the 4MB blocks", entry["blocks"] == 2, entry)
    check("the uploaded bytes are the local bytes",
          PAN.files["/tea_a2/releases/9.9.9/tea_a2-9.9.9.zip"]
          == open(package, "rb").read())
    # 删除
    syncer_a.client.delete_path("/tea_a2/releases/9.9.9/tea_a2-9.9.9.zip")
    check("a published file can be deleted",
          "/tea_a2/releases/9.9.9/tea_a2-9.9.9.zip" not in PAN.files, sorted(PAN.files))

    # 只留最新的 N 个：造几个版本，传完再清理
    for version in ("1.0.1", "1.0.2", "1.0.3", "1.0.4"):
        dummy = os.path.join(home_pub, "tea_a2-%s.zip" % version)
        with open(dummy, "wb") as handle:
            handle.write(b"dummy " + version.encode("ascii"))
        code = pan_publish.main([dummy, "--config", config_pub,
                                  "--remote-dir", "/tea_a2/releases/" + version,
                                  "--no-verify"])
        check("publishing %s works" % version, code == 0, code)
    # 版本号比大小：9.9.9 > 1.0.4 > 1.0.3 …
    removed = pan_publish.prune(syncer_a.client, "/tea_a2/releases", 2)
    check("the pruning keeps the newest ones",
          sorted(removed) == ["1.0.1", "1.0.2", "1.0.3"], removed)
    left = [e.get("server_filename") for e in syncer_a.client.list_dir("/tea_a2/releases")
            if e.get("isdir")]
    check("only the newest releases are left",
          sorted(left) == ["1.0.4", "9.9.9"], left)

    # ---- the command line --------------------------------------------------
    env = dict(os.environ)
    env["TEA_PAN_HOME"] = pan_sync.PAN_HOME
    proc = subprocess.Popen([sys.executable, os.path.join(REPO_ROOT, "sched", "pan_sync.py"),
                             "--config", config_a, "status"],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    out, err = proc.communicate()
    check("the status command runs", proc.returncode == 0, err.decode("utf-8", "replace"))
    check("the status command prints the remote dir",
          remote_dir in out.decode("utf-8", "replace"), out.decode("utf-8", "replace"))
    proc = subprocess.Popen([sys.executable, os.path.join(REPO_ROOT, "sched", "pan_sync.py"),
                             "--config", config_a, "check"],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    out, err = proc.communicate()
    check("the check command runs", proc.returncode == 0, err.decode("utf-8", "replace"))
    check("the check command says the netdisk is reachable",
          "网盘：通" in out.decode("utf-8", "replace"), out.decode("utf-8", "replace"))

    # ---- no passphrase means no push --------------------------------------
    home_e = os.path.join(TMP_HOME, "home-e")
    write_provider(home_e, key="sk-e")
    config_e = make_config(base_url, home_e,
                            passphrase="", extra=["allow_plain: no"])
    config_e = config_e.replace("passphrase: \n", "passphrase:\n")
    syncer_e = pan_sync.Syncer(pan_sync.load_config(config_e))
    try:
        syncer_e.push()
        check("a push without a passphrase is refused", False, "no exception")
    except pan_sync.ConfigError as exc:
        check("a push without a passphrase is refused", "没有加密口令" in str(exc), str(exc))

    # ---- the expired token is refreshed ------------------------------------
    syncer_a.client.token = {"access_token": "old", "refresh_token": "ref-1",
                             "expires_at": 0, "scope": "basic netdisk"}
    fresh = syncer_a.client.ensure_token()
    check("an expired token is refreshed", fresh == "acc-2", fresh)
    check("the new refresh token is written down",
          json.load(open(token_path, encoding="utf-8"))["refresh_token"] == "ref-2")

    server.shutdown()
    shutil.rmtree(TMP_HOME, ignore_errors=True)

    print("\n%d/%d checks passed" % (len(checks) - len(failures), len(checks)))
    if failures:
        print("FAILED: " + ", ".join(failures))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
