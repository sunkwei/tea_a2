#!/usr/bin/env python3
"""Tests for GET /api/workspaces/{id}/archive - the .tea_agent_run zip download.

The point of the endpoint is that an operator can take a workspace's run data
(sessions, trajectories, logs, memory, agent.md) off the device in one click, so
what matters is that the bytes are a *real* zip: entry names that unpack to the
directory they came from, contents identical to what is on disk, and the sandbox
still enforced on the `path` parameter.

Usage: python3 tests/smoke_run_archive.py [path-to-tea_agent-binary]
"""

import http.client
import io
import json
import os
import random
import shutil
import socket
import subprocess
import sys
import time
import zipfile

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


def wait_health(port, timeout=20.0):
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

    tmp = os.path.abspath(os.path.join(
        os.environ.get("TEMP", "/tmp") if os.name == "nt" else "/tmp",
        "tea_archive_%s" % os.urandom(4).hex()))
    os.makedirs(os.path.join(tmp, "home"))
    ws = os.path.join(tmp, "ws")
    os.makedirs(ws)

    config = os.path.join(tmp, "provider.yaml")
    with open(config, "w", encoding="utf-8") as f:
        f.write(
            """version: 1
providers:
  mock:
    api_url: http://127.0.0.1:1
    api_key: sk-test
    default_model: m
    models: {m: {}}
"""
        )

    port = free_port()
    proc = subprocess.Popen(
        [binary, "--config", config, "--tea-home", os.path.join(tmp, "home"),
         "--port", str(port), "--no-console", "--log-file", os.path.join(tmp, "log.txt"),
         "--workspace", ws],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    failures = []

    def check(name, ok, detail=""):
        if not ok:
            failures.append("%s %s" % (name, detail))
        print(("PASS " if ok else "FAIL ") + name + (("  " + str(detail)) if detail and not ok else ""))

    def get(path, timeout=120):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        conn.request("GET", path)
        r = conn.getresponse()
        body = r.read()
        conn.close()
        return r.status, dict(r.getheaders()), body

    try:
        if not wait_health(port):
            print("server did not start")
            return 1

        status, _, body = get("/api/workspaces")
        ws_id = json.loads(body)["workspaces"][0]["id"]

        run = os.path.join(ws, ".tea_agent_run")
        check("the run directory exists", os.path.isdir(run), run)

        # Make it interesting: a nested file, an empty directory, and something
        # highly compressible (so the test can tell compression happened).
        os.makedirs(os.path.join(run, "scripts"), exist_ok=True)
        os.makedirs(os.path.join(run, "empty-dir"), exist_ok=True)
        with open(os.path.join(run, "scripts", "hello.py"), "w", encoding="utf-8") as f:
            f.write("print('hi')\n")
        payload = ("茶 agent 归档测试\n" * 4000).encode("utf-8")
        with open(os.path.join(run, "note.txt"), "wb") as f:
            f.write(payload)

        # ---- 1) the download itself --------------------------------------
        status, headers, body = get("/api/workspaces/%s/archive" % ws_id)
        check("GET archive returns 200", status == 200, status)
        check("it is served as a zip", headers.get("Content-Type") == "application/zip",
              headers.get("Content-Type"))
        disp = headers.get("Content-Disposition", "")
        check("it is an attachment with a .zip name",
              disp.startswith("attachment;") and ".zip" in disp, disp)
        check("the zip magic is there", body[:4] == b"PK\x03\x04", body[:4].hex())
        check("the archive is not empty", len(body) > 200, len(body))

        zf = zipfile.ZipFile(io.BytesIO(body))
        check("the archive passes zipfile.testzip()", zf.testzip() is None, zf.testzip())
        names = zf.namelist()
        check("every entry lives under .tea_agent_run/",
              all(n.startswith(".tea_agent_run/") for n in names),
              [n for n in names if not n.startswith(".tea_agent_run/")][:3])
        check("the leading dot survives (it is part of the real name)",
              any(n.startswith(".tea_agent_run/") for n in names), names[:2])
        for expected in (".tea_agent_run/agent.md", ".tea_agent_run/memory/memory.json",
                         ".tea_agent_run/logs/agent.log", ".tea_agent_run/scripts/hello.py",
                         ".tea_agent_run/note.txt"):
            check("contains %s" % expected, expected in names, expected)
        check("an empty directory is stored as an entry",
              ".tea_agent_run/empty-dir/" in names, names)

        check("note.txt comes back byte-identical", zf.read(".tea_agent_run/note.txt") == payload,
              len(zf.read(".tea_agent_run/note.txt")))
        info = zf.getinfo(".tea_agent_run/note.txt")
        check("text is actually compressed", info.compress_size * 4 < info.file_size,
              "%d -> %d" % (info.file_size, info.compress_size))

        check("X-Archive-Entries matches the zip",
              headers.get("X-Archive-Entries") == str(len(names)),
              "%s vs %d" % (headers.get("X-Archive-Entries"), len(names)))
        check("nothing was skipped", headers.get("X-Archive-Skipped") == "0",
              headers.get("X-Archive-Skipped"))
        check("X-Archive-Uncompressed reports the raw size",
              int(headers.get("X-Archive-Uncompressed", "0")) >= len(payload),
              headers.get("X-Archive-Uncompressed"))

        # ---- 2) a subdirectory -------------------------------------------
        status, headers, body = get("/api/workspaces/%s/archive?path=.tea_agent_run/scripts" % ws_id)
        sub = zipfile.ZipFile(io.BytesIO(body))
        check("a subdirectory archives on its own", status == 200 and
              sub.namelist() == [".tea_agent_run/scripts/hello.py"], sub.namelist())

        # ---- 3) the sandbox and the error paths ---------------------------
        status, _, body = get("/api/workspaces/%s/archive?path=../.." % ws_id)
        check("escaping the workspace is refused", status == 403, (status, body[:120]))
        status, _, body = get("/api/workspaces/%s/archive?path=nope" % ws_id)
        check("a missing directory is a 404", status == 404, (status, body[:120]))
        status, _, body = get("/api/workspaces/%s/archive?path=.tea_agent_run/note.txt" % ws_id)
        check("a file is refused with not_a_directory",
              status == 400 and b"not_a_directory" in body, (status, body[:120]))
        status, _, _ = get("/api/workspaces/ws_doesnotexist/archive")
        check("an unknown workspace is a 404", status == 404, status)

        # ---- 4) an oversized file is skipped, not fatal -------------------
        big = os.path.join(run, "big.bin")
        with open(big, "wb") as f:
            f.write(b"\0" * (33 * 1024 * 1024))     # just over the 32 MB per-file cap
        status, headers, body = get("/api/workspaces/%s/archive" % ws_id)
        zf2 = zipfile.ZipFile(io.BytesIO(body))
        check("the archive still succeeds with an oversized file inside", status == 200, status)
        check("the oversized file was skipped",
              ".tea_agent_run/big.bin" not in zf2.namelist() and
              int(headers.get("X-Archive-Skipped", "0")) == 1,
              (headers.get("X-Archive-Skipped"), ".tea_agent_run/big.bin" in zf2.namelist()))
        check("everything else is still there", ".tea_agent_run/note.txt" in zf2.namelist())
        os.remove(big)
    finally:
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
    print("PASS: the workspace archive is a real zip, sandboxed, and skips what does not fit")
    return 0


if __name__ == "__main__":
    sys.exit(main())
