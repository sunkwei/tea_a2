#!/usr/bin/env python3
"""A workspace the agent cannot write to must say so, not fail later.

The sandbox around a process can limit its writes to one directory. A
workspace outside that limit is perfectly readable but unusable: the session
files cannot be created there. The agent finds out at startup (it tries a real
write) and reports it, so the user sees "read-only" in the workspace list
instead of a failed session request with a raw Windows error.

What this pins:

  * /api/workspaces says whether a workspace is writable, and why not;
  * a workspace the process may not write to is reported as read-only;
  * a normal workspace is reported as writable and really is (a session can be
    created in it).

Usage: python3 tests/smoke_workspace_writable.py [path-to-tea_agent-binary]
"""

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
    api_url: http://127.0.0.1:1/v1
    api_key: sk-test
    models:
      m: {max_context_tokens: 32768, max_output_tokens: 256}
settings: {}
"""


def free_port():
    while True:
        port = random.randint(20000, 60000)
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue


def call(port, method, path, body=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), data=data, method=method,
                                 headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"null")
        except ValueError:
            return e.code, {}


def main():
    binary = (sys.argv[1] if len(sys.argv) > 1
              else os.path.join(REPO_ROOT, "build-mingw", "bin", "tea_agent.exe"))
    if not os.path.exists(binary):
        print("binary not found:", binary)
        return 1

    tmp = tempfile.mkdtemp(prefix="tea_wswrite_")
    cfg = os.path.join(tmp, "provider.yaml")
    with open(cfg, "w", encoding="utf-8") as f:
        f.write(CONFIG)
    home = os.path.join(tmp, "home")
    os.makedirs(home, exist_ok=True)
    good_ws = os.path.join(tmp, "project")
    os.makedirs(good_ws, exist_ok=True)
    # A workspace the process may not write to: the account gets a deny ACE on
    # the directory, exactly what a sandbox around the process does.
    locked_ws = os.path.join(tmp, "locked")
    os.makedirs(locked_ws, exist_ok=True)
    account = "%s\\%s" % (os.environ.get("USERDOMAIN", ""), os.environ.get("USERNAME", ""))
    subprocess.run(["icacls", locked_ws, "/deny", "%s:(OI)(CI)(W)" % account],
                   capture_output=True, text=True)
    port = free_port()

    proc = subprocess.Popen([binary, "--config", cfg, "--tea-home", home, "--host", "127.0.0.1",
                             "--port", str(port), "--no-console", "--quiet",
                             "--log-file", os.path.join(tmp, "agent.log"),
                             "--workspace", good_ws, "--workspace", locked_ws],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    failures = []
    checks = []

    def check(name, ok, detail=""):
        checks.append(name)
        print(("PASS " if ok else "FAIL ") + name + ((("  [" + str(detail) + "]")) if not ok else ""))
        if not ok:
            failures.append(name)

    try:
        up = False
        for _ in range(100):
            try:
                if call(port, "GET", "/api/health")[0] == 200:
                    up = True
                    break
            except OSError:
                time.sleep(0.2)
        check("the server is up", up)

        status, data = call(port, "GET", "/api/workspaces")
        workspaces = (data or {}).get("workspaces") or []
        by_path = {w.get("path"): w for w in workspaces}
        good = by_path.get(good_ws)
        locked = by_path.get(locked_ws)
        check("both workspaces are listed", bool(good) and bool(locked),
              [w.get("path") for w in workspaces])
        check("a writable workspace says so", good and good.get("writable") is True, good)
        check("a workspace the process cannot write to is read-only",
              locked and locked.get("writable") is False, locked)
        check("the reason is given", locked and locked.get("write_error"),
              locked and locked.get("write_error"))

        # and the writable one really is usable
        status, data = call(port, "POST", "/api/workspaces/%s/sessions" % good["id"], {"title": "probe"})
        check("a session can be created in the writable workspace",
              status == 200 and (data or {}).get("id"), (status, data))
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
