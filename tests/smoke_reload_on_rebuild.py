# -*- coding: utf-8 -*-
"""A rebuilt binary takes the port over from the running one.

On Windows the build replaces the exe while the old instance is still running
(the build moves the old file aside so the linker can write a new one). The
running agent watches its own file: when it changes, the new build is started,
waits for the port, and the old instance drains and exits. That is what makes
"compile and it takes effect" work on a machine that runs the agent straight
from the build tree.

    python3 tests/smoke_reload_on_rebuild.py [path-to-tea_agent]

Local only: a fake provider is never contacted. Windows only - the feature is
Windows only.
"""
import http.client
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXE = sys.argv[1] if len(sys.argv) > 1 else os.path.join(REPO_ROOT, "build", "bin", "tea_agent.exe")

results = []


def check(name, ok, detail=""):
    results.append((name, ok))
    print(("PASS " if ok else "FAIL ") + name + (("  " + str(detail)) if (detail and not ok) else ""))


def free_port():
    for _ in range(50):
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        finally:
            s.close()
        if port > 1024:
            return port
    raise RuntimeError("no free port")


def health(port, timeout=2.0):
    try:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        c.request("GET", "/api/health")
        r = c.getresponse()
        body = r.read().decode("utf-8", "replace")
        c.close()
        return r.status == 200 and json.loads(body).get("ok")
    except Exception:
        return False


def wait_health(port, timeout=30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if health(port):
            return True
        time.sleep(0.15)
    return False


def port_holder_pid(home):
    """The pid that owns the port, as the agent itself records it."""
    path = os.path.join(home, "tea_agent.port")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return int(json.load(handle).get("pid") or 0)
    except Exception:
        return 0


def alive(pid):
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def main():
    if os.name != "nt":
        print("SKIP: the reload-on-rebuild handover is Windows only")
        return 0
    if not os.path.isfile(EXE):
        print("no agent binary: %s" % EXE)
        return 2

    tmp = tempfile.mkdtemp(prefix="tea_reload_")
    work = os.path.join(tmp, "bin")
    os.makedirs(work)
    exe = os.path.join(work, "tea_agent.exe")
    shutil.copy2(EXE, exe)
    home = os.path.join(tmp, "home")
    port = free_port()

    proc = subprocess.Popen(
        [exe, "--host", "127.0.0.1", "--port", str(port), "--tea-home", home,
         "--no-console", "--quiet"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    new_pid = 0
    try:
        check("the copy starts and serves", wait_health(port))
        first_pid = port_holder_pid(home) or proc.pid
        check("the port file names the running instance", first_pid > 0, first_pid)

        # What a build does: the old file is moved aside (Windows will not let
        # the linker overwrite a running image), and a new one takes the name.
        # copyfile, not copy2: the mtime has to be the new one (copy2 keeps the
        # old timestamp, and a file that did not change is not a new build).
        os.replace(exe, exe + ".prev")
        shutil.copyfile(EXE, exe)
        os.utime(exe, None)

        deadline = time.time() + 90
        took_over = False
        while time.time() < deadline:
            time.sleep(0.5)
            pid = port_holder_pid(home)
            if pid and pid != first_pid and health(port):
                new_pid = pid
                took_over = True
                break
        check("the new build took the port over", took_over,
              "still pid %s" % first_pid)
        check("the old instance is gone", proc.poll() is not None or not alive(first_pid),
              "old pid %s still alive" % first_pid)
        check("the new instance answers", health(port))
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        if new_pid and alive(new_pid):
            try:
                os.kill(new_pid, signal.SIGTERM)
            except OSError:
                pass
        shutil.rmtree(tmp, ignore_errors=True)

    failed = [name for name, ok in results if not ok]
    print("")
    print("%d/%d checks passed" % (len(results) - len(failed), len(results)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
