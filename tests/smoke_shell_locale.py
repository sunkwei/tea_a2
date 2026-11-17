#!/usr/bin/env python3
"""The web terminal has to speak UTF-8, even when the machine has no locale.

The bug this pins down: readline in the C locale turns every byte with the
high bit set into an escape sequence (ESC + the rest).  So a Chinese word
typed into the terminal does not arrive as a word - it arrives as a handful
of meta keys, and bash answers with "Display all 2159 possibilities?".

Machines that ask for a locale they do not have (a minimal install asks for
zh_CN.UTF-8 and only has C) fall into that trap without saying anything:
setlocale() fails quietly and the shell runs in the C locale.

So the test starts the agent with a locale that cannot be set and checks
that Chinese still goes in and comes back.

Usage: python tests/smoke_shell_locale.py [path-to-tea_agent-binary]
"""

import base64
import json
import os
import random
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CONFIG = """version: 1
providers:
  mock:
    api_url: http://127.0.0.1:9/v1
    api_key: sk-test
    default_model: m
    models: {m: {supports_tools: true}}
"""

# A locale that is not installed anywhere: setlocale() fails for it.
BROKEN_LOCALE = "zz_ZZ.NOPE"


def free_port():
    for _ in range(50):
        port = random.randint(20000, 60000)
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue


class Client:
    def __init__(self, port):
        self.base = "http://127.0.0.1:%d" % port

    def _request(self, method, path, payload=None):
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data, headers=headers,
                                     method=method)
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                body = r.read().decode("utf-8")
                return r.status, (json.loads(body) if body else None)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            try:
                return e.code, json.loads(body)
            except ValueError:
                return e.code, {"raw": body}

    def get(self, path):
        return self._request("GET", path)

    def post(self, path, payload=None):
        return self._request("POST", path, payload if payload is not None else {})


def drain(client, sid, cursor, wait=1200):
    """Read what the shell has printed since `cursor`."""
    status, body = client.get("/api/shell/session/%s/output?cursor=%d&wait=%d" % (sid, cursor, wait))
    if status != 200 or not isinstance(body, dict):
        return cursor, b""
    return body.get("cursor", cursor), base64.b64decode(body.get("data_b64", ""))


def main():
    binary = (sys.argv[1] if len(sys.argv) > 1
              else os.path.join(REPO_ROOT, "build", "bin", "tea_agent"))
    if not os.path.exists(binary):
        print("binary not found:", binary)
        return 1

    tmp = tempfile.mkdtemp(prefix="tea_shell_locale_")
    cfg = os.path.join(tmp, "provider.yaml")
    with open(cfg, "w", encoding="utf-8") as f:
        f.write(CONFIG)
    home = os.path.join(tmp, "home")
    os.makedirs(home, exist_ok=True)
    port = free_port()

    env = dict(os.environ)
    # The environment asks for a locale that is not there: the shell must
    # still end up in a UTF-8 one.
    env["LANG"] = BROKEN_LOCALE
    env["LC_ALL"] = BROKEN_LOCALE
    env.pop("LC_CTYPE", None)
    # A home directory without a .bashrc: the user's own startup file often
    # exports a working locale, and that would hide the bug.
    env["HOME"] = home

    proc = subprocess.Popen(
        [binary, "--config", cfg, "--tea-home", home, "--host", "127.0.0.1",
         "--port", str(port), "--no-console", "--quiet",
         "--log-file", os.path.join(tmp, "agent.log")],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=tmp, env=env)

    failures = []
    checks = []

    def check(name, ok, detail=""):
        checks.append(name)
        print(("PASS " if ok else "FAIL ") + name +
              ((("  [" + str(detail) + "]")) if not ok else ""))
        if not ok:
            failures.append(name)

    client = Client(port)
    try:
        up = False
        for _ in range(100):
            if proc.poll() is not None:
                break
            try:
                status, _body = client.get("/api/health")
                if status == 200:
                    up = True
                    break
            except OSError:
                time.sleep(0.2)
        check("the server serves", up)
        if not up:
            return 1

        status, body = client.post("/api/shell/session", {"workspace_id": "", "cols": 80, "rows": 24})
        check("a shell session is created", status == 200 and isinstance(body, dict) and body.get("id"),
              (status, body))
        if not up or not body or not body.get("id"):
            return 1
        sid = body["id"]

        cursor, out = drain(client, sid, 0)
        check("the shell starts", len(out) > 0, out[-200:])

        # ---- the shell runs in a UTF-8 locale ------------------------------
        # The environment asks for a locale that is not installed.  The shell
        # must not end up in the C locale: that is where readline turns the
        # Chinese into escape sequences.
        client.post("/api/shell/session/%s/input" % sid, {"data": "locale charmap\r"})
        time.sleep(1.5)
        cursor, out = drain(client, sid, cursor)
        text = out.decode("utf-8", "replace")
        check("the shell does not complain about the locale",
              "cannot change locale" not in text, text[-300:])
        check("the shell runs in a UTF-8 locale", "UTF-8" in text.upper(), text[-300:])

        # ---- the word goes in and comes back -----------------------------
        word = "你好"          # 你好
        raw = word.encode("utf-8")
        status, _body = client.post("/api/shell/session/%s/input" % sid, {"data": word})
        check("the word is accepted", status == 200, status)
        time.sleep(1.5)
        cursor, out = drain(client, sid, cursor)
        check("the shell echoes the word", raw in out, out[-300:])
        check("the word did not turn into escape sequences",
              b"\x1b" not in out.split(raw)[0][-40:] if raw in out else True, out[-300:])

        # ---- and the shell really understands it -------------------------
        client.post("/api/shell/session/%s/input" % sid, {"data": "\r"})
        time.sleep(1.5)
        cursor, out = drain(client, sid, cursor)
        # bash cannot run a Chinese word: it says so, in the C locale that
        # would be "command not found", in a UTF-8 one the word is in the
        # message.  Either way the word must be there and not be mangled.
        check("the shell answers about the word", raw in out, out[-300:])
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    print("%d/%d checks passed" % (len(checks) - len(failures), len(checks)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
