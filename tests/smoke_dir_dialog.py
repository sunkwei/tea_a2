#!/usr/bin/env python3
"""Tests for the native directory picker behind POST /api/dialog/directory.

A real dialog cannot be driven from a test, so two seams are used:

  * TEA_AGENT_DIR_PICKER replaces the backend with an arbitrary command whose
    stdout is the chosen path (this is also the escape hatch for headless boxes
    and terminal file browsers over SSH);
  * fake `kdialog` / `zenity` executables placed first on PATH exercise the real
    backend-selection and argument-building code without drawing anything.

Usage: python3 tests/smoke_dir_dialog.py [path-to-tea_agent-binary]
"""

import http.client
import json
import os
import random
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def free_port():
    while True:
        port = random.randint(20000, 60000)
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue


def wait_health(port, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            conn.request("GET", "/api/health")
            if conn.getresponse().status == 200:
                return True
        except OSError:
            pass
        time.sleep(0.15)
    return False


def main():
    binary = (
        sys.argv[1] if len(sys.argv) > 1 else os.path.join(REPO_ROOT, "build", "bin", "tea_agent")
    )
    if not os.path.exists(binary):
        print("binary not found:", binary)
        return 1

    tmp = tempfile.mkdtemp(prefix="tea_dlg_")
    os.makedirs(os.path.join(tmp, "home"))
    os.makedirs(os.path.join(tmp, "ws"))
    chosen = os.path.join(tmp, "picked dir")  # a space, to catch quoting bugs
    os.makedirs(chosen)

    config = os.path.join(tmp, "provider.yaml")
    with open(config, "w", encoding="utf-8") as f:
        f.write(
            """version: 1
providers:
  mock:
    api_url: http://127.0.0.1:1
    api_key: sk-test
    default_model: m
    models: {m: {supports_tools: true}}
roles: {main: {provider: mock, model: m}}
"""
        )

    def script(name, body):
        path = os.path.join(tmp, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
        return path

    picker = script("pick.sh", "#!/bin/sh\necho '%s'\n" % chosen)
    cancel = script("cancel.sh", "#!/bin/sh\nexit 1\n")
    broken = script("broken.sh", "#!/bin/sh\necho 'boom' >&2\nexit 2\n")
    slow = script("slow.sh", "#!/bin/sh\nsleep 30\necho '%s'\n" % chosen)

    # Fake GUI backends that record their argv.
    fakebin = os.path.join(tmp, "fakebin")
    os.makedirs(fakebin)
    for tool in ("kdialog", "zenity"):
        path = os.path.join(fakebin, tool)
        with open(path, "w", encoding="utf-8") as f:
            f.write(
                "#!/bin/sh\n"
                "printf '%%s\\n' \"$@\" > %s/%s.args\n"
                "echo '%s'\n" % (fakebin, tool, chosen)
            )
        os.chmod(path, 0o755)

    failures = []
    procs = []

    def start(env_extra, path_prefix=None):
        port = free_port()
        env = dict(os.environ)
        env.pop("TEA_AGENT_DIR_PICKER", None)
        env.update(env_extra)
        if path_prefix:
            env["PATH"] = path_prefix + os.pathsep + env.get("PATH", "")
        proc = subprocess.Popen(
            [
                binary,
                "--config", config,
                "--tea-home", os.path.join(tmp, "home"),
                "--port", str(port),
                "--no-console",
                "--log-file", os.path.join(tmp, "log.txt"),
                "--workspace", os.path.join(tmp, "ws"),
            ],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        procs.append(proc)
        if not wait_health(port):
            raise RuntimeError("server did not start")
        return port

    def call(port, body=None, timeout=40):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        conn.request(
            "POST",
            "/api/dialog/directory",
            body=json.dumps(body or {}),
            headers={"Content-Type": "application/json"},
        )
        r = conn.getresponse()
        raw = r.read()
        try:
            return r.status, json.loads(raw)
        except ValueError:
            return r.status, {"raw": raw.decode("utf-8", "replace")}

    try:
        # ---- 1) the override returns whatever the command prints -----------
        port = start({"TEA_AGENT_DIR_PICKER": picker})
        status, body = call(port, {"initial": tmp})
        if status != 200 or not body.get("ok") or body.get("path") != chosen:
            failures.append("1: override picker -> %s %r" % (status, body))
        elif body.get("tool") != "TEA_AGENT_DIR_PICKER":
            failures.append("1: unexpected tool name %r" % body.get("tool"))

        # ---- 2) a path with spaces survives verbatim -----------------------
        status, body = call(port, {"initial": ""})
        if body.get("path") != chosen:
            failures.append("2: path with a space was mangled: %r" % body.get("path"))

        # ---- 3) health advertises the picker -------------------------------
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/health")
        health = json.loads(conn.getresponse().read())
        if health.get("dir_picker") is not True:
            failures.append("3: /api/health should report dir_picker=true, got %r"
                            % health.get("dir_picker"))

        # ---- 4) KDE desktop prefers kdialog -------------------------------
        port = start({"XDG_CURRENT_DESKTOP": "KDE"}, path_prefix=fakebin)
        status, body = call(port)
        if body.get("tool") != "kdialog" or body.get("path") != chosen:
            failures.append("4: KDE should use kdialog, got %r" % (body,))
        args_file = os.path.join(fakebin, "kdialog.args")
        if os.path.exists(args_file):
            args = open(args_file, encoding="utf-8").read()
            if "--getexistingdirectory" not in args:
                failures.append("4: kdialog was called without --getexistingdirectory: %r" % args)
        else:
            failures.append("4: kdialog was never invoked")

        # ---- 5) GNOME desktop falls back to zenity ------------------------
        port = start({"XDG_CURRENT_DESKTOP": "GNOME"}, path_prefix=fakebin)
        status, body = call(port)
        if body.get("tool") != "zenity" or body.get("path") != chosen:
            failures.append("5: GNOME should use zenity, got %r" % (body,))
        args_file = os.path.join(fakebin, "zenity.args")
        if os.path.exists(args_file):
            args = open(args_file, encoding="utf-8").read()
            if "--directory" not in args:
                failures.append("5: zenity was called without --directory: %r" % args)
        else:
            failures.append("5: zenity was never invoked")

        # ---- 6) cancelling is not an error --------------------------------
        port = start({"TEA_AGENT_DIR_PICKER": cancel})
        status, body = call(port)
        if status != 200 or body.get("cancelled") is not True or body.get("ok") is not False:
            failures.append("6: cancel should be 200 + cancelled, got %s %r" % (status, body))

        # ---- 7) a broken picker reports the failure ------------------------
        port = start({"TEA_AGENT_DIR_PICKER": broken})
        status, body = call(port)
        if status != 500 or body.get("ok") is not False or "boom" not in body.get("error", ""):
            failures.append("7: broken picker -> %s %r" % (status, body))

        # ---- 8) no picker at all: 501 + unavailable ------------------------
        # An empty PATH leaves `command -v` unable to find anything; /bin/sh is
        # absolute, so the server itself still starts.
        port = start({}, path_prefix=None)
        empty_path = os.path.join(tmp, "emptybin")
        os.makedirs(empty_path)
        # Restart with a PATH that contains only the empty dir.
        procs[-1].terminate()
        procs[-1].wait(timeout=5)
        procs.pop()
        port = free_port()
        env = dict(os.environ)
        env.pop("TEA_AGENT_DIR_PICKER", None)
        env["PATH"] = empty_path
        proc = subprocess.Popen(
            [binary, "--config", config, "--tea-home", os.path.join(tmp, "home"),
             "--port", str(port), "--no-console", "--log-file", os.path.join(tmp, "log2.txt"),
             "--workspace", os.path.join(tmp, "ws")],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        procs.append(proc)
        if not wait_health(port):
            failures.append("8: server did not start with an empty PATH")
        else:
            status, body = call(port)
            if status != 501 or body.get("unavailable") is not True:
                failures.append("8: expected 501 unavailable, got %s %r" % (status, body))
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("GET", "/api/health")
            health = json.loads(conn.getresponse().read())
            if health.get("dir_picker") is not False:
                failures.append("8: /api/health should report dir_picker=false")

        # ---- 9) the chosen path is accepted by the workspace endpoint ------
        port = start({"TEA_AGENT_DIR_PICKER": picker})
        status, body = call(port)
        picked = body.get("path")
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("POST", "/api/workspaces",
                     body=json.dumps({"path": picked, "name": "picked"}),
                     headers={"Content-Type": "application/json"})
        r = conn.getresponse()
        raw = r.read()
        if r.status != 200:
            failures.append("9: the picked directory was rejected by /api/workspaces: %s %r"
                            % (r.status, raw[:200]))
        else:
            entry = json.loads(raw)
            if not os.path.isdir(entry.get("path", "")):
                failures.append("9: registered path is not a directory: %r" % entry.get("path"))
    finally:
        for proc in procs:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)

    if failures:
        print("FAIL")
        for f in failures:
            print(" -", f)
        return 1
    print("PASS: native directory picker (override, kdialog/zenity selection, cancel, failure, unavailable)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
