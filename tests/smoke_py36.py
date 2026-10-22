#!/usr/bin/env python3
"""The Python side must run on Python 3.6 (the devices are old).

The queue and the WeChat bot are meant to run next to the agent on a device
where nobody installs a newer Python. Two things stand in the way:

  * http.server.ThreadingHTTPServer arrived in 3.7 (before that:
    socketserver.ThreadingMixIn + HTTPServer);
  * subprocess.run(capture_output=..., text=...) arrived in 3.7 (before that:
    stdout=PIPE/stderr=PIPE and universal_newlines=True).

This test greps for them - and for the other things that quietly need a newer
Python - so the requirement does not rot the next time somebody edits a file.

Usage: python3 tests/smoke_py36.py
"""

import os
import re
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCES = ("sched",)

# (name, pattern, what it needs) - the third column is the version that broke it.
FORBIDDEN = [
    ("http.server.ThreadingHTTPServer without the 3.6 shim",
     r"^from http\.server import .*\bThreadingHTTPServer\b", "3.7"),
    ("subprocess.run(capture_output=...)",
     r"capture_output\s*=", "3.7"),
    ("subprocess text=... (universal_newlines is the 3.6 spelling)",
     r"\btext\s*=\s*True", "3.7"),
    ("the walrus operator", r":=", "3.8"),
    ("f-string = specifier", r"\{[A-Za-z_][A-Za-z0-9_]*=\}", "3.8"),
    ("str.removeprefix / removesuffix", r"\.remove(prefix|suffix)\(", "3.9"),
    ("dataclasses", r"^\s*(from dataclasses import|import dataclasses)", "3.7"),
    ("from __future__ import annotations", r"from __future__ import annotations", "3.7"),
    ("asyncio.run", r"\basyncio\.run\(", "3.7"),
    ("typing.Protocol", r"\bProtocol\b", "3.8"),
]


def main():
    failures = []
    checks = []

    def check(name, ok, detail=""):
        checks.append(name)
        print(("PASS " if ok else "FAIL ") + name + (("  " + str(detail)) if not ok else ""))
        if not ok:
            failures.append(name)

    files = []
    for target in SOURCES:
        base = os.path.join(REPO_ROOT, target)
        for name in sorted(os.listdir(base)):
            if name.endswith(".py"):
                files.append(os.path.join(base, name))
    check("there are python sources to check", bool(files), files)

    for path in files:
        rel = os.path.relpath(path, REPO_ROOT)
        with open(path, encoding="utf-8") as f:
            lines = f.read().split("\n")
        has_shim = any("except ImportError" in l for l in lines)
        for name, pattern, version in FORBIDDEN:
            hit = None
            for i, line in enumerate(lines, 1):
                if re.search(pattern, line, re.M):
                    hit = (i, line.strip())
                    break
            if hit is None:
                continue
            if name.startswith("http.server.ThreadingHTTPServer") and has_shim:
                # The file defines its own 3.6 spelling - fine.
                continue
            check("%s does not use %s (needs %s)" % (rel, name, version), False,
                  "L%d: %s" % hit)

    # The 3.6 spelling of the threaded server has to actually work, not just
    # be present: the queue serves its API with it on an old Python.
    import http.client
    import http.server
    import socketserver
    import threading

    class Shim(socketserver.ThreadingMixIn, http.server.HTTPServer):
        daemon_threads = True

    class Hello(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = b"hello"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = Shim(("127.0.0.1", 0), Hello)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        port = server.server_address[1]
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/")
        answer = conn.getresponse().read()
        conn.close()
        check("the 3.6 spelling of the threaded server works", answer == b"hello", answer)
    finally:
        server.shutdown()

    check("no python source needs more than 3.6", not failures, failures)
    print()
    print("%d/%d checks passed" % (len(checks) - len(failures), len(checks)))
    if failures:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
