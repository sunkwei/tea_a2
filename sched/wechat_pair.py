#!/usr/bin/env python3
"""The pairing page: a person scans a code, the WeChat side starts.

The bot needs one thing from a human: the QR code that binds a WeChat
account to this machine. Until now that meant sitting at the machine and
running `python3 sched/wechat_bot.py --login` in a terminal. This page is
the same thing with a browser in front of it:

    1. open http://127.0.0.1:8391/ on the machine (or through an SSH
       tunnel from a phone),
    2. type a name for the account ("alice"),
    3. the page shows the QR code - the phone scans it and confirms,
    4. the credentials are written and the service for that account is
       started (`systemctl --user start a2_wechat@alice`).

Several accounts can live on one machine: every account has a directory of
its own (~/.tea_agent/wechat/<name>/) with its own credentials, its own
cursor and its own context tokens, and its own service instance
(a2_wechat@<name>.service). Nothing is shared, so two accounts cannot mix
their conversations.

The page is a plain HTTP server from the standard library: no pip, no
dependencies - the same rule as the rest of sched/.

Usage:

  python3 sched/wechat_pair.py                    # 127.0.0.1:8391
  python3 sched/wechat_pair.py --host 0.0.0.0     # reachable from a phone
  python3 sched/wechat_pair.py --token secret     # a token in the URL
"""

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import urllib.parse

try:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
except ImportError:  # Python 3.6
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from socketserver import ThreadingMixIn

    class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
        """The 3.6 spelling of http.server.ThreadingHTTPServer."""
        daemon_threads = True

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import wechat_bot  # noqa: E402

log = wechat_bot.log
DEFAULT_PORT = 8391
DEFAULT_HOST = "127.0.0.1"
# The name of the account that is not a named one: the plain unit.
DEFAULT_ACCOUNT = "default"
# The service that serves every account in one process.
MUL_UNIT = "a2_wechat_mul.service"
# How long a QR code is waited for before a new one is asked for.
SESSION_TIMEOUT = 600.0


def unit_of(name):
    """The service unit of one account."""
    name = (name or "").strip()
    if not name or name == DEFAULT_ACCOUNT:
        return "a2_wechat.service"
    return "a2_wechat@%s.service" % wechat_bot.safe_name(name)


class Pairing(object):
    """The accounts of this machine and the codes that are waiting for a scan."""

    def __init__(self, root=None, base_url=wechat_bot.DEFAULT_BASE_URL,
                 start_cmd="systemctl --user start", stop_cmd="systemctl --user stop",
                 mul_unit=MUL_UNIT):
        self.root = os.path.abspath(os.path.expanduser(
            root or wechat_bot.DEFAULT_STATE_DIR))
        self.mul_unit = mul_unit
        self.base_url = base_url
        self.start_cmd = start_cmd
        self.stop_cmd = stop_cmd
        self.lock = threading.Lock()
        self.sessions = {}
        # The state of the services is asked from systemd - the page
        # refreshes every two seconds, so the answer is kept for a moment.
        self.service_cache = {}
        # How often the code is asked about (the tests make it quick).
        self.poll_interval = 2.0
        os.makedirs(self.root, exist_ok=True)

    # -- the accounts -----------------------------------------------------
    def state_dir(self, name):
        """The directory of one account."""
        name = (name or "").strip()
        if not name or name == DEFAULT_ACCOUNT:
            return self.root
        return os.path.join(self.root, wechat_bot.safe_name(name))

    def credential_path(self, name):
        return os.path.join(self.state_dir(name), "credentials.json")

    def account_names(self):
        """Every account this machine knows (the default one first)."""
        names = []
        if os.path.isfile(self.credential_path(DEFAULT_ACCOUNT)):
            names.append(DEFAULT_ACCOUNT)
        try:
            for name in sorted(os.listdir(self.root)):
                if name.startswith("."):
                    continue
                if os.path.isfile(os.path.join(self.root, name, "credentials.json")):
                    names.append(name)
        except OSError:
            pass
        if DEFAULT_ACCOUNT not in names:
            names.insert(0, DEFAULT_ACCOUNT)
        return names

    def qr_url_of(self, name, session):
        """The link of the code that is waiting.

        The bot publishes its own code (in the log); it writes the link
        down, so the page can show it too.
        """
        url = (session or {}).get("image") or ""
        if url:
            return url
        try:
            with open(os.path.join(self.state_dir(name), "qr.url"),
                      encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            return ""

    def accounts(self):
        """What the page shows: one line per account."""
        out = []
        with self.lock:
            sessions = dict(self.sessions)
        for name in self.account_names():
            creds = wechat_bot.read_credentials(self.credential_path(name))
            session = sessions.get(name) or {}
            out.append({
                "name": name,
                "unit": unit_of(name),
                "logged_in": bool(creds.get("token")),
                "bot_id": creds.get("ilink_bot_id") or "",
                "user_id": creds.get("ilink_user_id") or "",
                "service": self.service_state(name),
                "owner": self.owner_of(name),
                "session": session.get("status") or "",
                "qr": bool(session.get("image")) or os.path.isfile(
                    os.path.join(self.state_dir(name), "qr.png")),
                "qr_url": self.qr_url_of(name, session),
                "message": session.get("message") or "",
            })
        return out

    # -- the services -----------------------------------------------------
    def service_state(self, name):
        """Is the service of this account running?"""
        unit = unit_of(name)
        now = time.time()
        cached = self.service_cache.get(unit)
        if cached and now - cached[0] < 5.0:
            return cached[1]
        try:
            proc = subprocess.run(["systemctl", "--user", "is-active", unit],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  timeout=10)
        except (OSError, subprocess.SubprocessError):
            return "unknown"
        out = (proc.stdout or b"").decode("utf-8", "replace").strip() or "unknown"
        self.service_cache[unit] = (now, out)
        return out

    def mul_active(self):
        """Is the one-process-many-accounts service running?

        If it is, that service takes the new accounts - starting a unit for
        them as well would mean two bots polling with the same cursor.
        """
        now = time.time()
        cached = self.service_cache.get(self.mul_unit)
        if cached and now - cached[0] < 5.0:
            return cached[1] == "active"
        try:
            proc = subprocess.run(["systemctl", "--user", "is-active", self.mul_unit],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  timeout=10)
        except (OSError, subprocess.SubprocessError):
            return False
        out = (proc.stdout or b"").decode("utf-8", "replace").strip()
        self.service_cache[self.mul_unit] = (now, out or "unknown")
        return out == "active"

    def owner_of(self, name):
        """Who serves this account: its own unit, or the one-process service."""
        if self.service_state(name) == "active":
            return unit_of(name)
        if self.mul_active():
            return self.mul_unit
        return unit_of(name)

    def service_start(self, name):
        """Start the service of this account."""
        unit = unit_of(name)
        if self.service_state(name) == "active":
            return True, "%s 已经在跑了" % unit
        if self.mul_active():
            # One service serves every account: it picks the new one up by
            # itself, so no unit is started here.
            return True, "%s 会接上这个账号" % self.mul_unit
        cmd = self.start_cmd.split() + [unit]
        try:
            proc = subprocess.run(cmd, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, timeout=30)
        except (OSError, subprocess.SubprocessError) as exc:
            log("cannot start %s: %s" % (unit, exc))
            return False, "cannot run %s: %s" % (" ".join(cmd), exc)
        if proc.returncode != 0:
            detail = (proc.stderr or b"").decode("utf-8", "replace").strip()[:200]
            log("cannot start %s: %s" % (unit, detail))
            return False, detail or ("exit %d" % proc.returncode)
        log("started %s" % unit)
        return True, unit

    def service_stop(self, name):
        """Stop the service of this account."""
        unit = unit_of(name)
        cmd = self.stop_cmd.split() + [unit]
        try:
            proc = subprocess.run(cmd, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, timeout=30)
        except (OSError, subprocess.SubprocessError) as exc:
            return False, str(exc)
        if proc.returncode != 0:
            detail = (proc.stderr or b"").decode("utf-8", "replace").strip()[:200]
            return False, detail or ("exit %d" % proc.returncode)
        return True, unit

    # -- the codes --------------------------------------------------------
    def add(self, name):
        """Ask for a QR code for this account and wait for the scan.

        The page is answered right away (with the code); a thread watches
        the status and starts the service when the person confirms.
        """
        name = (name or "").strip()
        if not name:
            return {"ok": False, "error": "the account needs a name"}
        directory = self.state_dir(name)
        os.makedirs(directory, exist_ok=True)
        with self.lock:
            old = self.sessions.get(name)
            if old and old.get("thread") and old["thread"].is_alive():
                return {"ok": True, "name": name, "status": old.get("status"),
                        "qr_url": old.get("image") or ""}
        client = wechat_bot.ILink(self.base_url)
        try:
            qr = client.get_qrcode(wechat_bot.known_tokens(self.root))
        except wechat_bot.ILinkError as exc:
            log("cannot ask for a QR code: %s" % exc)
            return {"ok": False, "error": str(exc)}
        code = qr.get("qrcode") or qr.get("qrcode_id") or ""
        image = qr.get("qrcode_img_content") or qr.get("qrcode_url") or ""
        wechat_bot.save_qr_image(directory, image)
        # The marker tells the bot of this account that somebody else holds
        # the code: it must not ask for a second one.
        marker = os.path.join(directory, "pairing")
        try:
            with open(marker, "w", encoding="utf-8") as f:
                f.write(str(os.getpid()))
        except OSError:
            pass
        session = {"status": "waiting", "image": image, "qrcode": code,
                   "message": "等人扫码", "started": time.time(),
                   "client": client, "directory": directory, "name": name}
        with self.lock:
            self.sessions[name] = session
        thread = threading.Thread(target=self._watch, args=(name,), daemon=True)
        session["thread"] = thread
        thread.start()
        log("a QR code is waiting for %s" % name)
        return {"ok": True, "name": name, "status": "waiting", "qr_url": image}

    def set_code(self, name, code):
        """The digits the phone shows (risk control)."""
        code = str(code or "").strip()
        if not code:
            return {"ok": False, "error": "no code"}
        directory = self.state_dir(name)
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, "verify_code")
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(code)
        except OSError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True}

    def _watch(self, name):
        """Watch one code until the person scans it (or it dies)."""
        with self.lock:
            session = self.sessions.get(name) or {}
        client = session.get("client")
        code = session.get("qrcode") or ""
        if client is None or not code:
            return
        directory = session.get("directory") or self.state_dir(name)
        deadline = time.time() + SESSION_TIMEOUT
        verify_code = ""
        while time.time() < deadline:
            time.sleep(self.poll_interval)
            try:
                status = client.qr_status(code, verify_code)
            except wechat_bot.ILinkError as exc:
                self._update(name, "error", "问二维码出了错：%s" % exc)
                continue
            state = str(status.get("status") or "")
            if state in ("", "wait"):
                continue
            if state == "scaned":
                self._update(name, "scaned", "已扫码，等确认…")
                continue
            if state == "need_verifycode":
                given = self._read_code(directory)
                if given:
                    verify_code = given
                    self._update(name, "need_verifycode", "收到验证码，继续等")
                else:
                    self._update(name, "need_verifycode", "手机上的验证码：填在下面")
                continue
            if state == "verify_code_blocked":
                self._update(name, "blocked", "验证码错太多，重新生成一个码")
                self._clear_marker(directory)
                return
            if state == "expired":
                self._update(name, "expired", "二维码过期了，重新生成一个")
                self._clear_marker(directory)
                return
            if state == "scaned_but_redirect":
                host = str(status.get("redirect_host") or "")
                if host:
                    client.base_url = host if "://" in host else "https://" + host
                continue
            if state == "binded_redirect":
                if self._take_over(name, directory):
                    self._clear_marker(directory)
                    return
                self._update(name, "error", "这个号已经绑过了，但本机没有凭证")
                self._clear_marker(directory)
                return
            if state == "confirmed":
                creds = {
                    "token": status.get("bot_token", ""),
                    "base_url": status.get("baseurl") or self.base_url,
                    "ilink_bot_id": status.get("ilink_bot_id", ""),
                    "ilink_user_id": status.get("ilink_user_id", ""),
                    "cursor": "",
                    "contexts": {},
                }
                path = os.path.join(directory, "credentials.json")
                try:
                    with open(path, "w", encoding="utf-8") as f:
                        json.dump(creds, f, ensure_ascii=False, indent=2)
                except OSError as exc:
                    self._update(name, "error", "凭证写不下去：%s" % exc)
                    self._clear_marker(directory)
                    return
                self._update(name, "confirmed", "扫好了，正在启动服务…")
                self._clear_marker(directory)
                ok, detail = self.service_start(name)
                self._update(name, "connected" if ok else "error",
                             "服务已启动（%s）" % detail if ok else detail)
                return
        self._update(name, "expired", "二维码过期了，重新生成一个")
        self._clear_marker(directory)

    def _read_code(self, directory):
        """The digits the phone shows, if the page was given them."""
        path = os.path.join(directory, "verify_code")
        try:
            with open(path, encoding="utf-8") as f:
                code = f.read().strip()
        except OSError:
            return ""
        if code:
            try:
                os.unlink(path)
            except OSError:
                pass
        return code

    def _take_over(self, name, directory):
        """The account is already bound here: take its credentials over."""
        for path in wechat_bot.credential_files(self.root):
            data = wechat_bot.read_credentials(path)
            if not data.get("token"):
                continue
            target = os.path.join(directory, "credentials.json")
            try:
                with open(target, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
            except OSError:
                return False
            self._update(name, "confirmed", "这个号已经绑过了，直接用它的凭证")
            ok, detail = self.service_start(name)
            self._update(name, "connected" if ok else "error",
                         "服务已启动（%s）" % detail if ok else detail)
            return True
        return False

    @staticmethod
    def _clear_marker(directory):
        try:
            os.unlink(os.path.join(directory, "pairing"))
        except OSError:
            pass

    def _update(self, name, status, message=""):
        with self.lock:
            session = self.sessions.get(name)
            if session is None:
                return
            session["status"] = status
            session["message"] = message
        log("%s: %s%s" % (name, status, (" - " + message) if message else ""))

    # -- the page ---------------------------------------------------------
    def page(self, token=""):
        """The page itself: the accounts and the codes."""
        rows = []
        for account in self.accounts():
            rows.append(
                "<tr><td><b>%s</b></td>"
                "<td>%s</td>"
                "<td><code>%s</code></td>"
                "<td>%s</td>"
                "<td>%s</td>"
                "<td class='act'>"
                "<button onclick='act(\"start\", \"%s\")'>启动</button> "
                "<button onclick='act(\"stop\", \"%s\")'>停止</button> "
                "<button onclick='add(\"%s\")'>重新扫码</button>"
                "</td></tr>"
                % (_esc(account["name"]),
                   "已登录" if account["logged_in"] else "没登录",
                   _esc(account["user_id"] or "-"),
                   _esc(account.get("owner") or account["unit"]),
                   _esc(account["service"]),
                   _esc(account["name"]), _esc(account["name"]),
                   _esc(account["name"])))
        html = PAGE.replace("{{rows}}", "".join(rows))
        html = html.replace("{{token}}", _esc(token))
        return html


def _esc(text):
    out = str(text if text is not None else "")
    return (out.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


PAGE = """<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>微信配对</title>
<style>
body { font-family: system-ui, "PingFang SC", "Microsoft YaHei", sans-serif;
       margin: 0 auto; max-width: 900px; padding: 24px; color: #222; }
h1 { font-size: 20px; }
table { border-collapse: collapse; width: 100%; margin: 12px 0 24px; }
th, td { border-bottom: 1px solid #e5e5e5; padding: 8px 6px; text-align: left;
         font-size: 14px; vertical-align: top; }
th { color: #666; font-weight: 500; }
code { font-size: 12px; color: #666; word-break: break-all; }
button { padding: 4px 10px; margin-right: 4px; cursor: pointer; }
input { padding: 6px 8px; font-size: 14px; }
.card { border: 1px solid #e5e5e5; border-radius: 8px; padding: 16px;
        margin-bottom: 16px; }
#qr img { max-width: 260px; display: block; margin: 12px 0; }
#msg { font-size: 14px; color: #444; }
.small { font-size: 12px; color: #888; }
</style>
</head>
<body>
<h1>微信配对</h1>
<p class="small">一个人扫一次码，这台机器上的微信这一端就接上了；扫完服务自动起来。
每个微信号一个目录、一个服务实例，互不干扰。</p>

<div class="card">
  <b>加一个微信号</b>
  <p><input id="name" placeholder="账号名，比如 alice" size="24">
     <button onclick="add(document.getElementById('name').value.trim())">生成二维码</button></p>
  <div id="qr"></div>
  <p id="msg"></p>
  <p class="small">手机微信显示验证码的时候，填在这里再点一下：<br>
     <input id="code" placeholder="验证码" size="10">
     <input id="codename" placeholder="账号名" size="14">
     <button onclick="sendCode()">提交验证码</button></p>
</div>

<table>
<tr><th>账号</th><th>状态</th><th>微信用户</th><th>服务单元</th><th>服务</th><th>操作</th></tr>
{{rows}}
</table>
<p class="small">接口：GET /api/accounts（JSON）、POST /add（name=）、
POST /code（name=、code=）、POST /start（name=）、POST /stop（name=）。</p>

<script>
var token = "{{token}}";
function post(path, data, done) {
  var body = new URLSearchParams(data || {});
  body.set("token", token);
  fetch(path, {method: "POST", body: body}).then(function(r) { return r.json(); })
    .then(function(j) { if (done) done(j); });
}
function add(name) {
  if (!name) { alert("先写个账号名"); return; }
  post("/add", {name: name}, function(j) {
    if (!j.ok) { msg(j.error || "出了点问题"); return; }
    msg("码已经出来了，等着扫码…（这个页面每 2 秒自己刷新）");
    showQr(name);
  });
}
function showQr(name) {
  document.getElementById("qr").innerHTML =
    "<b>" + name + "</b><br><img src='/qr/" + encodeURIComponent(name) +
    ".png?t=" + Date.now() + "'>";
}
function msg(text) { document.getElementById("msg").textContent = text; }
function act(what, name) {
  post("/" + what, {name: name}, function(j) {
    msg(j.ok ? (what + " " + name + "：好了") : (j.error || "失败"));
    refresh();
  });
}
function sendCode() {
  post("/code", {name: document.getElementById("codename").value.trim(),
                  code: document.getElementById("code").value.trim()},
       function(j) { msg(j.ok ? "验证码收到了" : (j.error || "没收到")); });
}
function refresh() {
  fetch("/api/accounts?t=" + Date.now()).then(function(r) { return r.json(); })
    .then(function(list) {
      list.forEach(function(a) {
        if (a.session && a.session !== "waiting" && a.session !== "scaned") {
          msg(a.name + "：" + (a.message || a.session));
        }
      });
    });
}
setInterval(refresh, 2000);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    """The few routes the page needs."""

    server_version = "a2-wechat-pair"

    def log_message(self, *args):
        pass

    @property
    def pairing(self):
        return self.server.pairing

    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        data = body if isinstance(body, bytes) else str(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, payload, code=200):
        self._send(code, json.dumps(payload, ensure_ascii=False),
                   "application/json; charset=utf-8")

    def _allowed(self, query):
        """The token, when there is one."""
        token = getattr(self.server, "token", "")
        if not token:
            return True
        given = (query.get("token") or [""])[0]
        return given == token

    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        if not self._allowed(query):
            return self._json({"ok": False, "error": "bad token"}, 403)
        path = parsed.path
        if path == "/":
            return self._send(200, self.pairing.page(getattr(self.server, "token", "")))
        if path == "/api/accounts":
            return self._json(self.pairing.accounts())
        if path.startswith("/qr/"):
            name = path[4:]
            if name.endswith(".png"):
                name = name[:-4]
            path_png = os.path.join(self.pairing.state_dir(name), "qr.png")
            try:
                with open(path_png, "rb") as f:
                    data = f.read()
            except OSError:
                return self._send(404, "no qr code yet")
            return self._send(200, data, "image/png")
        return self._send(404, "not found")

    def do_POST(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length).decode("utf-8", "replace") if length else ""
        ctype = self.headers.get("Content-Type", "")
        data = {}
        if "json" in (ctype or ""):
            try:
                data = json.loads(raw or "{}")
            except ValueError:
                data = {}
        else:
            data = {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}
        query = urllib.parse.parse_qs(parsed.query)
        query.update({k: [v] for k, v in data.items()})
        if not self._allowed(query):
            return self._json({"ok": False, "error": "bad token"}, 403)
        path = parsed.path
        name = str(data.get("name") or "").strip()
        if path == "/add":
            return self._json(self.pairing.add(name))
        if path == "/code":
            return self._json(self.pairing.set_code(name, data.get("code")))
        if path == "/start":
            ok, detail = self.pairing.service_start(name)
            return self._json({"ok": ok, "detail": detail})
        if path == "/stop":
            ok, detail = self.pairing.service_stop(name)
            return self._json({"ok": ok, "detail": detail})
        return self._send(404, "not found")


def build_parser():
    parser = argparse.ArgumentParser(description="The WeChat pairing page.")
    parser.add_argument("--host", default=DEFAULT_HOST,
                        help="the address to listen on (default: %s)" % DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help="the port (default: %d)" % DEFAULT_PORT)
    parser.add_argument("--root", default=None,
                        help="where the accounts live (default: %s)"
                             % wechat_bot.DEFAULT_STATE_DIR)
    parser.add_argument("--base-url", default=wechat_bot.DEFAULT_BASE_URL,
                        help="the iLink server")
    parser.add_argument("--token", default="",
                        help="a token every request must carry (?token=...)")
    parser.add_argument("--start-cmd", default="systemctl --user start",
                        help="how a service is started (the unit name is added)")
    parser.add_argument("--stop-cmd", default="systemctl --user stop",
                        help="how a service is stopped (the unit name is added)")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    pairing = Pairing(root=args.root, base_url=args.base_url,
                      start_cmd=args.start_cmd, stop_cmd=args.stop_cmd)
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.daemon_threads = True
    httpd.pairing = pairing
    httpd.token = args.token
    url = "http://%s:%d/" % (args.host, args.port)
    if args.token:
        url += "?token=%s" % urllib.parse.quote(args.token)
    log("配对页面：%s" % url)
    log("账号目录：%s" % pairing.root)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
