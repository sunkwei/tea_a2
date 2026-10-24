#!/usr/bin/env python3
"""The web terminal: a real shell on this machine, driven from the browser.

The feature has nothing to do with the model - no provider, no agent, no
tools. It is a shell (bash on Linux, PowerShell on Windows) on a pseudo
terminal, plus the handful of HTTP routes the browser needs to drive it.

What this pins:

  * GET /api/shell answers with what this build can do (and says why not when
    it cannot);
  * a session can be created, written to, read from and closed;
  * the output arrives as bytes with a cursor, so a reader that comes back
    late gets exactly what it missed - and only that;
  * two sessions do not see each other's output;
  * an unknown session is a 404, not a crash;
  * the shell really runs: the text it prints comes back.

Usage: python tests/smoke_shell.py [path-to-tea_agent-binary]
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
from urllib.parse import quote

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CONFIG = """version: 1
providers:
  mock:
    api_url: http://127.0.0.1:1/v1
    api_key: sk-test
    models:
      m: {max_context_tokens: 32768, max_output_tokens: 256}
settings: {}
"""

IS_WINDOWS = os.name == "nt"

# A command that prints a marker and nothing else, plus the key that ends it.
if IS_WINDOWS:
    PRINT_CMD = "Write-Output tea-shell-marker\r"
    EXIT_CMD = "exit\r"
else:
    PRINT_CMD = "echo tea-shell-marker\r"
    EXIT_CMD = "exit\r"


def free_port():
    while True:
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

    def post_raw(self, path, payload):
        """POST the bytes as they are (a file upload)."""
        req = urllib.request.Request(self.base + path, data=payload,
                                     headers={"Content-Type": "application/octet-stream"},
                                     method="POST")
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                body = r.read().decode("utf-8")
                return r.status, (json.loads(body) if body else None)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            try:
                return e.code, json.loads(body)
            except ValueError:
                return e.code, {"raw": body}

    def get_bytes(self, path):
        """GET a route that answers with bytes (a download)."""
        req = urllib.request.Request(self.base + path)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, r.read(), r.headers.get("Content-Disposition", "")
        except urllib.error.HTTPError as e:
            return e.code, e.read(), ""

    def delete(self, path):
        return self._request("DELETE", path)


def main():
    binary = (sys.argv[1] if len(sys.argv) > 1
              else os.path.join(REPO_ROOT, "build-mingw", "bin", "tea_agent.exe"))
    if not os.path.exists(binary):
        print("binary not found:", binary)
        return 1

    tmp = tempfile.mkdtemp(prefix="tea_shell_")
    cfg = os.path.join(tmp, "provider.yaml")
    with open(cfg, "w", encoding="utf-8") as f:
        f.write(CONFIG)
    home = os.path.join(tmp, "home")
    os.makedirs(home, exist_ok=True)
    port = free_port()

    proc = subprocess.Popen(
        [binary, "--config", cfg, "--tea-home", home, "--host", "127.0.0.1",
         "--port", str(port), "--no-console", "--quiet",
         "--log-file", os.path.join(tmp, "agent.log")],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=tmp)

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

        # ---- what this build can do -------------------------------------
        status, cap = client.get("/api/shell")
        check("GET /api/shell answers", status == 200, status)
        check("it reports the platform", bool(cap.get("platform")), cap)
        check("it says whether a shell is available",
              isinstance(cap.get("available"), bool), cap)
        available = bool(cap.get("available"))
        if available:
            check("an available shell names the program", bool(cap.get("shell")), cap)
        else:
            check("an unavailable shell says why", bool(cap.get("reason")), cap)

        # ---- the session list -------------------------------------------
        status, listing = client.get("/api/shell/sessions")
        check("GET /api/shell/sessions answers", status == 200, status)
        check("it starts out empty", listing.get("sessions") == [], listing)

        if not available:
            # Nothing else can be checked on a platform without a terminal,
            # but the routes must still answer instead of hanging.
            status, body = client.post("/api/shell/session", {"cols": 80, "rows": 24})
            check("creating a session is refused with a reason",
                  status == 400 and bool((body.get("error") or {}).get("message")), body)
            status, body = client.get("/api/shell/session/does-not-exist/output")
            check("an unknown session is a 404", status == 404, status)
            print()
            print("%d/%d checks passed" % (len(checks) - len(failures), len(checks)))
            if failures:
                print("FAILED:", ", ".join(failures))
                return 1
            return 0

        # ---- a session that really runs ----------------------------------
        status, created = client.post("/api/shell/session", {"cols": 100, "rows": 30})
        check("a session can be created", status == 200 and bool(created.get("id")),
              (status, created))
        sid = created.get("id")
        if not sid:
            print()
            print("%d/%d checks passed" % (len(checks) - len(failures), len(checks)))
            return 1
        check("the session reports a shell", bool(created["session"]["shell"]), created)
        check("the session is running", created["session"]["running"] is True, created)

        def read(cursor, wait=0):
            status, body = client.get(
                "/api/shell/session/%s/output?cursor=%d&wait=%d" % (sid, cursor, wait))
            if status != 200:
                return status, body, cursor, b""
            data = base64.b64decode(body.get("data_b64", ""))
            return status, body, body.get("cursor", cursor), data

        # The first prompt (and whatever the shell printed while starting).
        status, body, cursor, data = read(0, 2000)
        check("the output route answers", status == 200, status)
        check("the cursor starts at zero", cursor >= 0, cursor)

        # Write something and read it back.
        status, body = client.post("/api/shell/session/%s/input" % sid,
                                   {"data": PRINT_CMD})
        check("input is accepted", status == 200, (status, body))

        text = ""
        deadline = time.time() + 20
        while time.time() < deadline and "tea-shell-marker" not in text:
            status, body, cursor, data = read(cursor, 2000)
            text += data.decode("utf-8", "replace")
        check("the shell printed what it was asked to", "tea-shell-marker" in text,
              text[-300:])

        # The cursor only moves forward: a second read with the same cursor
        # returns nothing new.
        status, body, cursor2, data2 = read(cursor, 0)
        check("the cursor does not go back", cursor2 >= cursor, (cursor, cursor2))
        check("there is nothing new to read", data2 == b"", data2[:120])

        # Another session must not see this one's output.
        status, other = client.post("/api/shell/session", {"cols": 80, "rows": 24})
        check("a second session can be created", status == 200 and bool(other.get("id")),
              (status, other))
        other_id = other.get("id")
        if other_id:
            status, body = client.get(
                "/api/shell/session/%s/output?cursor=0&wait=300" % other_id)
            other_text = base64.b64decode(body.get("data_b64", "")).decode("utf-8", "replace")
            check("the sessions do not mix", "tea-shell-marker" not in other_text,
                  other_text[-200:])

        # Resize.
        status, body = client.post("/api/shell/session/%s/resize" % sid,
                                   {"cols": 120, "rows": 40})
        check("the terminal can be resized", status == 200, (status, body))
        status, body = client.get("/api/shell/session/%s" % sid)
        check("the new size is remembered",
              body.get("session", {}).get("cols") == 120, body)

        # The status route and an unknown id.
        status, body = client.get("/api/shell/session/%s" % sid)
        check("the session status answers", status == 200 and body.get("id") == sid, body)
        status, body = client.get("/api/shell/session/nope/output")
        check("an unknown session is a 404", status == 404, status)

        # The shell can also end on its own ("exit"): the session has to
        # say so, otherwise the browser would poll a session that will never
        # answer again.
        status, body = client.post("/api/shell/session/%s/input" % sid, {"data": EXIT_CMD})
        check("the exit command is accepted", status == 200, (status, body))
        ended = False
        deadline = time.time() + 20
        while time.time() < deadline:
            status, body = client.get("/api/shell/session/%s/output?cursor=%d&wait=500" % (sid, cursor))
            if isinstance(body, dict) and body.get("ended"):
                ended = True
                break
            cursor = body.get("cursor", cursor) if isinstance(body, dict) else cursor
        check("the session reports that the shell is gone", ended, body)
        if isinstance(body, dict):
            check("it reports the exit code", body.get("exit_code") is not None, body)
            check("it is not running any more", body.get("running") is False, body)

        # ---- files in and out ------------------------------------------
        payload = b"tea-shell-upload\x00\x01binary\xff\xfe"
        status, body = client.post_raw(
            "/api/shell/session/%s/upload?name=tea-upload.bin" % sid, payload)
        check("a file can be uploaded", status == 200 and body.get("ok"), (status, body))
        uploaded = body.get("path", "") if isinstance(body, dict) else ""
        check("the upload lands in the session's directory",
              os.path.basename(uploaded) == "tea-upload.bin" and
              os.path.isfile(uploaded), uploaded)
        check("the file has the bytes it was given",
              os.path.isfile(uploaded) and open(uploaded, "rb").read() == payload, uploaded)

        # A name with directories in it must not walk out of the directory.
        status, body = client.post_raw(
            "/api/shell/session/%s/upload?name=../tea-escape.bin" % sid, b"x")
        escaped = body.get("path", "") if isinstance(body, dict) else ""
        check("the name cannot walk out of the directory",
              status == 200 and os.path.dirname(escaped) == os.path.dirname(uploaded), escaped)

        # A named directory.
        status, body = client.post_raw(
            "/api/shell/session/%s/upload?name=in-sub.txt&dir=sub" % sid, b"sub")
        sub_path = body.get("path", "") if isinstance(body, dict) else ""
        check("the upload can go into another directory",
              status == 200 and os.path.isfile(sub_path) and
              os.path.basename(os.path.dirname(sub_path)) == "sub", sub_path)

        # Download it again: the bytes have to come back exactly.
        status, data, disposition = client.get_bytes(
            "/api/shell/session/%s/file?path=%s" % (sid, quote(uploaded)))
        check("the file can be downloaded", status == 200 and data == payload,
              (status, data[:60]))
        check("the download is an attachment",
              "attachment" in (disposition or ""), disposition)
        # A relative path is resolved against the session's directory.
        status, data, _d = client.get_bytes(
            "/api/shell/session/%s/file?path=tea-upload.bin" % sid)
        check("a relative path works too", status == 200 and data == payload, (status, data[:60]))
        status, _data, _d = client.get_bytes(
            "/api/shell/session/%s/file?path=tea-nope.bin" % sid)
        check("a missing file is a 404", status == 404, status)
        status, body = client.get("/api/shell/session/%s/file" % sid)
        check("a download without a path is refused", status == 400, (status, body))

        # Closing: the shell is gone, and the route says so.
        status, body = client.delete("/api/shell/session/%s" % sid)
        check("a session can be closed", status == 200, (status, body))
        status, body = client.get("/api/shell/session/%s" % sid)
        check("a closed session is gone", status == 404, status)
        if other_id:
            client.delete("/api/shell/session/%s" % other_id)
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            proc.kill()

    print()
    print("%d/%d checks passed" % (len(checks) - len(failures), len(checks)))
    if failures:
        print("FAILED:", ", ".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
