#!/usr/bin/env python3
"""One instance per port: a second agent must not share a port with the first.

httplib sets SO_REUSEADDR on every platform. On Windows that option means
"another process may bind the same address and port", so a second agent used
to start happily on a port that was already serving - and the traffic was
split between the two, so the browser could talk to either. Nothing said so.

What this pins:

  * the second instance refuses to start and says who holds the port;
  * the refusal names the process, not just "in use";
  * the first instance keeps serving (the check must not disturb it);
  * the port file is written while the instance runs and removed when it stops.

Usage: python3 tests/smoke_port_guard.py [path-to-tea_agent-binary]
"""

import json
import os
import random
import socket
import subprocess
import sys
import tempfile
import time
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


def main():
    binary = (sys.argv[1] if len(sys.argv) > 1
              else os.path.join(REPO_ROOT, "build-mingw", "bin", "tea_agent.exe"))
    if not os.path.exists(binary):
        print("binary not found:", binary)
        return 1

    tmp = tempfile.mkdtemp(prefix="tea_port_")
    cfg = os.path.join(tmp, "provider.yaml")
    with open(cfg, "w", encoding="utf-8") as f:
        f.write(CONFIG)
    home = os.path.join(tmp, "home")
    os.makedirs(home, exist_ok=True)
    port = free_port()

    def start(log_name):
        return subprocess.Popen(
            [binary, "--config", cfg, "--tea-home", home, "--host", "127.0.0.1",
             "--port", str(port), "--no-console", "--quiet",
             "--log-file", os.path.join(tmp, log_name)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    failures = []
    checks = []

    def check(name, ok, detail=""):
        checks.append(name)
        print(("PASS " if ok else "FAIL ") + name + ((("  [" + str(detail) + "]")) if not ok else ""))
        if not ok:
            failures.append(name)

    first = start("first.log")
    try:
        # wait for the first instance to serve
        up = False
        for _ in range(100):
            if first.poll() is not None:
                break
            try:
                with urllib.request.urlopen("http://127.0.0.1:%d/api/health" % port, timeout=2) as r:
                    if r.status == 200:
                        up = True
                        break
            except OSError:
                time.sleep(0.2)
        check("the first instance serves", up)

        port_file = os.path.join(home, "tea_agent.port")
        info = {}
        # The file is written right after the listener comes up, so give it a
        # moment: the health check can succeed a hair earlier.
        for _ in range(30):
            if os.path.exists(port_file):
                try:
                    with open(port_file, "r", encoding="utf-8") as f:
                        info = json.load(f)
                except ValueError:
                    info = {}
                if info:
                    break
            time.sleep(0.1)
        check("the port file names the listener",
              info.get("port") == port and info.get("pid"), info)

        # the second instance must refuse to start
        second = start("second.log")
        try:
            out, _ = second.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            second.kill()
            out, _ = second.communicate()
        check("the second instance exits", second.returncode != 0, second.returncode)
        log = os.path.join(tmp, "second.log")
        text = ""
        if os.path.exists(log):
            with open(log, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        check("it says the port is in use", "already in use" in text, (out or "")[:200] or text[-200:])
        check("it names the instance that holds the port",
              ("pid %d" % info.get("pid", -1)) in text, text[-300:])

        # the first instance is untouched
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/api/health" % port, timeout=5) as r:
                alive = r.status == 200
        except OSError:
            alive = False
        check("the first instance keeps serving", alive)
    finally:
        for proc in (first,):
            try:
                proc.terminate()
                proc.wait(timeout=10)
            except Exception:
                proc.kill()
        time.sleep(0.5)
        check("the port file is cleaned up",
              not os.path.exists(os.path.join(home, "tea_agent.port")))

    print()
    print("%d/%d checks passed" % (len(checks) - len(failures), len(checks)))
    if failures:
        print("FAILED:", ", ".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
