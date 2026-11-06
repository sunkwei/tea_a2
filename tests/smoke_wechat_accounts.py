#!/usr/bin/env python3
"""Several WeChat accounts on one machine, and the scan that starts them.

What this pins:

  * one account = one directory: the credentials, the cursor and the
    context tokens of two accounts never mix;
  * one account = one workspace: the queue keeps their tasks apart too;
  * one account = one channel name ("wechat:alice"), so the rules and the
    threads of two accounts do not mix either;
  * the login flow: the QR code, the pair code the phone asks for, the
    "already bound" answer (the credentials of the account that is already
    here are taken over), and a new code when the old one dies;
  * the protocol gets the tokens this machine knows, so the server can say
    "this bot is already bound here";
  * the pairing page: a person scans the code, the credentials are written
    and the service of that account is started;
  * the rules of the channel apply to every account on it.

Usage: python3 tests/smoke_wechat_accounts.py
"""

import base64
import json
import os
import shutil
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
import wechat_multi  # noqa: E402
import wechat_pair  # noqa: E402
import triggers  # noqa: E402

# A 1x1 PNG: enough for the page to have something to show.
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


class MockILink(BaseHTTPRequestHandler):
    """The iLink server: a QR code, a status, and the requests it got."""

    protocol_version = "HTTP/1.1"
    statuses = ["confirmed"]      # a queue: the last one repeats
    token = "bot-token-123"
    bot_id = "bot-1@im.bot"
    user_id = "me-1@im.wechat"
    qr_requests = []              # the bodies of get_bot_qrcode
    status_requests = []          # the query strings of get_qrcode_status
    polls = []                    # the tokens the bots poll with

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
            MockILink.status_requests.append(self.path)
            state = MockILink.statuses[0]
            if len(MockILink.statuses) > 1:
                state = MockILink.statuses.pop(0)
            payload = {"status": state}
            if state == "confirmed":
                payload.update({
                    "bot_token": MockILink.token,
                    "ilink_bot_id": MockILink.bot_id,
                    "ilink_user_id": MockILink.user_id,
                    "baseurl": "http://127.0.0.1:%d" % self.server.server_address[1],
                })
            return self._json(payload)
        if self.path.startswith("/qr"):
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(PNG)))
            self.end_headers()
            self.wfile.write(PNG)
            return
        self._json({"error": "no route"}, 404)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            body = {}
        if "get_bot_qrcode" in self.path:
            MockILink.qr_requests.append(body)
            return self._json({"qrcode": "qrc_test",
                               "qrcode_img_content":
                                   "http://127.0.0.1:%d/qr.png"
                                   % self.server.server_address[1]})
        if "getupdates" in self.path:
            MockILink.polls.append(self.headers.get("Authorization", ""))
            return self._json({"ret": 0, "msgs": [],
                               "get_updates_buf": body.get("get_updates_buf", "")})
        self._json({"error": "no route"}, 404)


class MockAgent(BaseHTTPRequestHandler):
    """The agent's workspace list - the only thing a bot needs of it."""

    protocol_version = "HTTP/1.1"
    workspaces = []

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
        if self.path.startswith("/api/workspaces"):
            return self._json({"workspaces": MockAgent.workspaces})
        self._json({"error": "not found"}, 404)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            body = {}
        entry = {"id": "ws_mock", "path": body.get("path", ""), "name": body.get("name", "")}
        MockAgent.workspaces.append(entry)
        self._json(entry)


def free_account_args(ilink_url, tmp, name):
    """The arguments of one account."""
    return wechat_bot.build_parser().parse_args([
        "--base-url", ilink_url,
        "--account", name,
        "--state-dir", os.path.join(tmp, "state", name),
        "--workspace-path", os.path.join(tmp, "ws", name),
        "--login-timeout", "15",
    ])


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
    ilink_url = "http://127.0.0.1:%d" % ilink.server_address[1]
    agent = ThreadingHTTPServer(("127.0.0.1", 0), MockAgent)
    agent.daemon_threads = True
    threading.Thread(target=agent.serve_forever, daemon=True).start()
    agent_url = "http://127.0.0.1:%d" % agent.server_address[1]

    tmp = tempfile.mkdtemp(prefix="tea_wechat_accounts_")
    try:
        # ---- two accounts, two of everything --------------------------
        alice = wechat_bot.WeChatBot(free_account_args(ilink_url, tmp, "alice"))
        bob = wechat_bot.WeChatBot(free_account_args(ilink_url, tmp, "bob"))
        check("the accounts have their own state",
              alice.state.path != bob.state.path, (alice.state.path, bob.state.path))
        check("the accounts have their own workspace",
              alice.args.workspace_path != bob.args.workspace_path,
              (alice.args.workspace_path, bob.args.workspace_path))
        check("the channel name carries the account",
              alice.channel_name() == "wechat:alice", alice.channel_name())
        check("the channel name of the other account is its own",
              bob.channel_name() == "wechat:bob", bob.channel_name())
        check("the account without a name keeps the old channel name",
              wechat_bot.WeChatBot(wechat_bot.build_parser().parse_args([
                  "--state-dir", os.path.join(tmp, "state", "one"),
                  "--workspace-path", os.path.join(tmp, "ws", "one"),
              ])).channel_name() == "wechat")

        # ---- the login: the QR code and the token --------------------
        MockILink.statuses = ["confirmed"]
        MockILink.qr_requests = []
        ok = alice.login()
        check("the login works", ok)
        creds = wechat_bot.read_credentials(alice.state.path)
        check("the credentials are written", creds.get("token") == MockILink.token, creds)
        check("the QR code is on disk", os.path.isfile(
            os.path.join(alice.state.dir, "qr.png")), os.listdir(alice.state.dir))
        check("the first account knows no token yet",
              not MockILink.qr_requests[-1].get("local_token_list"),
              MockILink.qr_requests[-1])

        # The tokens this machine knows go with the request: the server
        # then says "this bot is already bound here" instead of making a
        # second bot for the same account.
        bob.ilink = wechat_bot.ILink(ilink_url)
        bob.login()
        check("the known tokens go with the request",
              MockILink.token in (MockILink.qr_requests[-1].get("local_token_list") or []),
              MockILink.qr_requests[-1])

        # ---- the pair code the phone asks for ------------------------
        carol = wechat_bot.WeChatBot(free_account_args(ilink_url, tmp, "carol"))
        MockILink.statuses = ["need_verifycode", "confirmed"]
        with open(os.path.join(carol.state.dir, "verify_code"), "w",
                  encoding="utf-8") as f:
            f.write("123456")
        ok = carol.login()
        check("the pair code is accepted", ok)
        check("the pair code was sent",
              any("verify_code=123456" in p for p in MockILink.status_requests),
              MockILink.status_requests[-3:])
        check("the code file is used once",
              not os.path.exists(os.path.join(carol.state.dir, "verify_code")))

        # ---- "already bound": the credentials are taken over ---------
        dave = wechat_bot.WeChatBot(free_account_args(ilink_url, tmp, "dave"))
        MockILink.statuses = ["binded_redirect"]
        ok = dave.login()
        check("an account that is already bound takes its credentials over", ok)
        check("the credentials are the ones of the machine",
              wechat_bot.read_credentials(dave.state.path).get("token") == MockILink.token,
              wechat_bot.read_credentials(dave.state.path))

        # ---- a dead code: a new one is asked for ----------------------
        eve = wechat_bot.WeChatBot(free_account_args(ilink_url, tmp, "eve"))
        MockILink.statuses = ["expired", "confirmed"]
        before = len(MockILink.qr_requests)
        ok = eve.login()
        check("a dead code is replaced by a new one", ok)
        check("a new code was asked for",
              len(MockILink.qr_requests) > before, (before, len(MockILink.qr_requests)))

        # ---- the rules of the channel are the rules of the account ---
        rules_path = os.path.join(tmp, "triggers.conf")
        with open(rules_path, "w", encoding="utf-8") as f:
            f.write("[wechat]\nrequire = /.*/\nignore = 早上好\n\n"
                    "[wechat:alice]\nignore = 私人\n")
        rules = triggers.Rules(rules_path)
        check("the rules of the channel apply to an account",
              rules.decide("wechat:alice", "干活")[0] == triggers.ACCEPT,
              rules.decide("wechat:alice", "干活"))
        check("the rules of the account are its own",
              rules.decide("wechat:alice", "私人的事")[0] == triggers.IGNORE,
              rules.decide("wechat:alice", "私人的事"))
        check("the rules of the channel reach the other accounts too",
              rules.decide("wechat:bob", "早上好")[0] == triggers.IGNORE,
              rules.decide("wechat:bob", "早上好"))

        # ---- the pairing page: scan, and the service starts ----------
        root = os.path.join(tmp, "accounts")
        started = os.path.join(tmp, "started.txt")
        starter = os.path.join(tmp, "start.sh")
        with open(starter, "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\necho \"$@\" >> %s\n" % started)
        os.chmod(starter, 0o755)
        # A unit that does not exist here: the one-process service of this
        # machine must not decide what this test does.
        pairing = wechat_pair.Pairing(root=root, base_url=ilink_url,
                                      start_cmd=starter,
                                      mul_unit="a2_wechat_mul_test.service")
        pairing.poll_interval = 0.05
        MockILink.statuses = ["confirmed"]
        result = pairing.add("frank")
        check("the pairing page hands out a code", result.get("ok"), result)
        check("the code is on disk",
              os.path.isfile(os.path.join(pairing.state_dir("frank"), "qr.png")))
        check("the bot of that account knows a code is waiting",
              os.path.exists(os.path.join(pairing.state_dir("frank"), "pairing")))
        deadline = time.time() + 20
        while time.time() < deadline:
            if os.path.exists(os.path.join(pairing.state_dir("frank"),
                                           "credentials.json")):
                break
            time.sleep(0.1)
        check("the scan writes the credentials",
              os.path.isfile(os.path.join(pairing.state_dir("frank"),
                                          "credentials.json")),
              os.listdir(pairing.state_dir("frank")))
        check("the service of that account is started",
              os.path.isfile(started) and "a2_wechat@frank.service"
              in open(started, encoding="utf-8").read(),
              open(started, encoding="utf-8").read() if os.path.isfile(started) else "")
        check("the marker is gone when the scan is done",
              not os.path.exists(os.path.join(pairing.state_dir("frank"), "pairing")))
        accounts = pairing.accounts()
        names = [a["name"] for a in accounts]
        check("the page lists the accounts", "frank" in names, names)
        frank = [a for a in accounts if a["name"] == "frank"][0]
        check("the page says the account is logged in", frank["logged_in"], frank)
        check("the page names the unit of the account",
              frank["unit"] == "a2_wechat@frank.service", frank)
        page = pairing.page("")
        check("the page shows the accounts", "frank" in page, page[:200])

        # ---- one process, many accounts ------------------------------
        multi_root = os.path.join(tmp, "multi")
        for name in ("alice", "bob"):
            os.makedirs(os.path.join(multi_root, name), exist_ok=True)
        # both have been scanned already
        for name, token in (("alice", "token-alice"), ("bob", "token-bob")):
            with open(os.path.join(multi_root, name, "credentials.json"), "w",
                      encoding="utf-8") as f:
                json.dump({"token": token, "base_url": ilink_url,
                           "ilink_bot_id": "bot-" + name,
                           "ilink_user_id": "u-" + name,
                           "cursor": "", "contexts": {}}, f)
        multi_args = wechat_multi.build_parser().parse_args([
            "--accounts-dir", multi_root,
            "--workspaces-dir", os.path.join(tmp, "workspaces"),
            "--base-url", ilink_url,
            "--agent", agent_url,
            "--tasks", "http://127.0.0.1:1",     # nothing listens there
            "--scan-interval", "0.2",
            "--retry-delay", "0.2",
        ])
        multi = wechat_multi.Multi(multi_args)
        check("the accounts of the directory are found",
              multi.names() == ["alice", "bob"], multi.names())
        check("the unit of an account is named after it",
              multi.unit_of("alice") == "a2_wechat@alice.service", multi.unit_of("alice"))
        check("the unit of the account without a name is the plain one",
              multi.unit_of("default") == "a2_wechat.service", multi.unit_of("default"))
        check("an account with a service of its own is left alone by default",
              multi_args.skip_units is True)
        MockILink.polls = []
        thread = threading.Thread(target=multi.run, daemon=True)
        thread.start()
        deadline = time.time() + 20
        while time.time() < deadline and not MockILink.polls:
            time.sleep(0.2)
        check("the account that has been scanned polls with its own token",
              any("token-alice" in p for p in MockILink.polls), MockILink.polls[:3])
        check("every account polls with its own token",
              any("token-bob" in p for p in MockILink.polls), MockILink.polls[:6])
        check("every account gets a bot of its own",
              set(multi.bots) == {"alice", "bob"}, sorted(multi.bots))

        # an account whose directory is gone must not keep polling
        shutil.rmtree(os.path.join(multi_root, "bob"))
        deadline = time.time() + 20
        while time.time() < deadline:
            if "bob" not in multi.bots:
                break
            time.sleep(0.2)
        check("the bot of an account that is gone stops",
              "bob" not in multi.bots, sorted(multi.bots))
        multi.stop.set()

        # ---- the page over HTTP ---------------------------------------
        import urllib.request
        pair_http = ThreadingHTTPServer(("127.0.0.1", 0), wechat_pair.Handler)
        pair_http.daemon_threads = True
        pair_http.pairing = pairing
        pair_http.token = ""
        threading.Thread(target=pair_http.serve_forever, daemon=True).start()
        base = "http://127.0.0.1:%d" % pair_http.server_address[1]
        with urllib.request.urlopen(base + "/api/accounts", timeout=10) as resp:
            listed = json.loads(resp.read().decode("utf-8"))
        check("the page has an api", any(a["name"] == "frank" for a in listed), listed)
        with urllib.request.urlopen(base + "/", timeout=10) as resp:
            body = resp.read().decode("utf-8")
        check("the page is a page", "微信配对" in body, body[:120])
        request = urllib.request.Request(
            base + "/add", data=urllib.parse.urlencode({"name": "grace"}).encode())
        with urllib.request.urlopen(request, timeout=10) as resp:
            added = json.loads(resp.read().decode("utf-8"))
        check("the page can add an account", added.get("ok"), added)
        with urllib.request.urlopen(base + "/qr/grace.png", timeout=10) as resp:
            image = resp.read()
        check("the page serves the code", image == PNG, len(image))
        pair_http.shutdown()
    finally:
        ilink.shutdown()
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
