#!/usr/bin/env python3
"""Adding a workspace has to say out loud when the result is not usable.

Two failures hide behind a "200 OK" when a workspace is added:

  * the workspace cannot be written to (a sandbox, a read-only mount, a deny
    ACE): the registration is fine, but every session in it would fail;
  * the index (workspaces.json) cannot be written: the registration is gone
    after the next restart, and nothing else says so.

Both are reported in the response ("warning"), where the user is looking,
instead of surfacing later as a failed request or as a vanished entry.

What this pins:

  * a workspace the process cannot write to is added, but the response says
    it is read-only and why;
  * the workspace list carries the same verdict (writable / write_error);
  * an index that cannot be written produces a warning that names the file.

Usage: python3 tests/smoke_workspace_add_warning.py [path-to-tea_agent-binary]
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


def deny_writes(directory):
    """The account may not write into `directory` - what a sandbox does."""
    account = "%s\\%s" % (os.environ.get("USERDOMAIN", ""), os.environ.get("USERNAME", ""))
    subprocess.run(["icacls", directory, "/deny", "%s:(OI)(CI)(W)" % account],
                   capture_output=True, text=True)


def safe_list(directory):
    """The directory may refuse to be listed too - that is the point of it."""
    try:
        return os.listdir(directory)
    except OSError as e:
        return "cannot list: %s" % e


def start(binary, cfg, home, port, workspaces):
    args = [binary, "--config", cfg, "--tea-home", home, "--host", "127.0.0.1",
            "--port", str(port), "--no-console", "--quiet",
            "--log-file", os.path.join(tempfile.gettempdir(), "tea_wsaddwarn.log")]
    for ws in workspaces:
        args += ["--workspace", ws]
    return subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def wait_up(port):
    for _ in range(100):
        try:
            if call(port, "GET", "/api/health")[0] == 200:
                return True
        except OSError:
            time.sleep(0.2)
    return False


def main():
    binary = (sys.argv[1] if len(sys.argv) > 1
              else os.path.join(REPO_ROOT, "build-mingw", "bin", "tea_agent.exe"))
    if not os.path.exists(binary):
        print("binary not found:", binary)
        return 1

    tmp = tempfile.mkdtemp(prefix="tea_wsaddwarn_")
    cfg = os.path.join(tmp, "provider.yaml")
    with open(cfg, "w", encoding="utf-8") as f:
        f.write(CONFIG)

    failures = []
    checks = []

    def check(name, ok, detail=""):
        checks.append(name)
        print(("PASS " if ok else "FAIL ") + name + ((("  [" + str(detail) + "]")) if not ok else ""))
        if not ok:
            failures.append(name)

    # ---- 1) a workspace the process cannot write to -------------------------
    home = os.path.join(tmp, "home")
    os.makedirs(home, exist_ok=True)
    good = os.path.join(tmp, "project")
    os.makedirs(good, exist_ok=True)
    locked = os.path.join(tmp, "locked")
    os.makedirs(locked, exist_ok=True)
    deny_writes(locked)

    port = free_port()
    proc = start(binary, cfg, home, port, [good])
    try:
        check("the server is up", wait_up(port))
        status, data = call(port, "POST", "/api/workspaces", {"path": locked, "name": "locked"})
        check("the workspace is added", status == 200 and (data or {}).get("id"), (status, data))
        check("the response says it is not writable",
              data and data.get("writable") is False, data)
        check("the response explains why",
              data and data.get("write_error"), data and data.get("write_error"))
        check("the response warns about it",
              data and data.get("warning") and "cannot be written to" in data["warning"],
              data and data.get("warning"))
        status, listing = call(port, "GET", "/api/workspaces")
        by_path = {w.get("path"): w for w in (listing or {}).get("workspaces") or []}
        check("the list agrees", by_path.get(locked, {}).get("writable") is False,
              by_path.get(locked))
        check("the list gives the reason", by_path.get(locked, {}).get("write_error"),
              by_path.get(locked, {}).get("write_error"))
        check("a writable workspace is still writable",
              by_path.get(good, {}).get("writable") is True, by_path.get(good))
        # a tea home that can be written to says so
        status, health = call(port, "GET", "/api/health")
        check("the health endpoint reports a writable tea home",
              health and health.get("tea_home_writable") is True, health)
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            proc.kill()

    # ---- 2) an index that cannot be written ---------------------------------
    home2 = os.path.join(tmp, "home2")
    os.makedirs(home2, exist_ok=True)
    deny_writes(home2)
    ws2 = os.path.join(tmp, "project2")
    os.makedirs(ws2, exist_ok=True)
    port2 = free_port()
    proc2 = start(binary, cfg, home2, port2, [ws2])
    try:
        check("the second server is up", wait_up(port2))
        status, data = call(port2, "POST", "/api/workspaces", {"path": ws2, "name": "project2"})
        check("the workspace is added", status == 200 and (data or {}).get("id"), (status, data))
        warning = (data or {}).get("warning") or ""
        check("the response warns that the index was not saved",
              "index" in warning and "could not be saved" in warning, warning)
        check("the warning names the index file",
              "workspaces.json" in warning, warning)
        check("the index really is not on disk",
              not os.path.exists(os.path.join(home2, "workspaces.json")),
              safe_list(home2))
        # and the server says so up front, not only when a request fails
        status, health = call(port2, "GET", "/api/health")
        check("the health endpoint reports the tea home as not writable",
              health and health.get("tea_home_writable") is False, health)
        check("the health endpoint gives the reason",
              health and health.get("tea_home_error"), health and health.get("tea_home_error"))
    finally:
        try:
            proc2.terminate()
            proc2.wait(timeout=10)
        except Exception:
            proc2.kill()

    print()
    print("%d/%d checks passed" % (len(checks) - len(failures), len(checks)))
    if failures:
        print("FAILED:", ", ".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
