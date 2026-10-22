#!/usr/bin/env python3
"""Make the Python side of tea_a2 run on Python 3.6 as well.

Two things stand between the code and 3.6:

  * http.server.ThreadingHTTPServer arrived in 3.7 (before that:
    socketserver.ThreadingMixIn + HTTPServer);
  * subprocess.run(capture_output=...) and text=... arrived in 3.7 (before
    that: stdout=PIPE/stderr=PIPE and universal_newlines=True).

This script rewrites both, in place, over sched/ and tests/.

    python3 scripts/py36_compat.py            # rewrite
    python3 scripts/py36_compat.py --check    # only report
"""

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGETS = ("sched", "tests")

IMPORT_OLD = "from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer"
IMPORT_NEW = '''from http.server import BaseHTTPRequestHandler

try:
    # Python 3.7+
    from http.server import ThreadingHTTPServer
except ImportError:  # Python 3.6
    from http.server import HTTPServer
    from socketserver import ThreadingMixIn

    class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
        """The 3.6 spelling of http.server.ThreadingHTTPServer."""
        daemon_threads = True'''


def fix_source(text):
    out = text
    if IMPORT_OLD in out:
        out = out.replace(IMPORT_OLD, IMPORT_NEW)
    # subprocess.run(capture_output=True) -> stdout/stderr PIPE
    out = re.sub(r"capture_output=True", "stdout=subprocess.PIPE, stderr=subprocess.PIPE", out)
    # text=True -> universal_newlines=True
    out = re.sub(r"\btext=True\b", "universal_newlines=True", out)
    return out


def main():
    check_only = "--check" in sys.argv
    changed = []
    for target in TARGETS:
        base = os.path.join(ROOT, target)
        if not os.path.isdir(base):
            continue
        for dirpath, _dirs, names in os.walk(base):
            for name in sorted(names):
                if not name.endswith(".py"):
                    continue
                path = os.path.join(dirpath, name)
                with open(path, encoding="utf-8") as f:
                    text = f.read()
                fixed = fix_source(text)
                if fixed == text:
                    continue
                rel = os.path.relpath(path, ROOT)
                changed.append(rel)
                if not check_only:
                    with open(path, "w", encoding="utf-8") as f:
                        f.write(fixed)
    if check_only:
        print("would change: %s" % (", ".join(changed) if changed else "(nothing)"))
    else:
        print("rewrote %d file(s): %s" % (len(changed), ", ".join(changed)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
