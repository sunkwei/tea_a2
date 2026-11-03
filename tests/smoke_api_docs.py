#!/usr/bin/env python3
"""The API documentation must exist, be reachable, and not lie.

A device with no network and no repository still has to be able to read what
the API looks like - so docs/api.md goes into the binary at build time and
`GET /api/doc` serves it from there. A documentation endpoint is only worth
having if the documentation is right, so this also checks the other direction:
every route the code registers has to be mentioned in docs/api.md. A route that
nobody wrote down is a route nobody uses.

What is pinned here:

  * every route in src/server.cpp is mentioned in docs/api.md;
  * GET /api/doc serves the reference as a web page;
  * ?format=md serves the same text as the file in the repository, byte for
    byte (a stale binary is caught here);
  * ?format=json serves the version, the Markdown and the rendered HTML;
  * `Accept: text/markdown` asks for the Markdown without a query string;
  * an unknown format is a 400, not a silent fallback.

Usage: python3 tests/smoke_api_docs.py [path-to-tea_agent-binary]
"""

import http.client
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(REPO_ROOT, "src", "server.cpp")
DOC = os.path.join(REPO_ROOT, "docs", "api.md")

# The queue server's routes (Python side, sched/) - documented in docs/tasks.md.
QUEUE_PREFIXES = ("/api/fleet", "/api/peer", "/api/tasks", "/api/projects",
                  "/api/events", "/api/audit", "/api/turns")


def canon(path):
    """A route as one shape: /a/([^/]+)/b, /a/{id}/b and /a/b?x=1 all become
    /a/{}/b. The code and the document spell the same route differently."""
    p = path.split("?")[0].rstrip("/")
    p = re.sub(r"\(\[\^/\]\+\)", "{}", p)
    p = re.sub(r"\{[^}]*\}", "{}", p)
    return p


def routes_in_code():
    src = io.open(SRC, encoding="utf-8").read()
    out = {}
    for rx in (r'svr\.(Get|Post|Put|Patch|Delete)\(\s*R"\((/api/[^"]*)\)"',
               r'svr\.(Get|Post|Put|Patch|Delete)\(\s*"(/api/[^"]*)"'):
        for m in re.finditer(rx, src, re.S):
            out.setdefault(m.group(2), set()).add(m.group(1).upper())
    return out


# The peer channel mirrors every /api/* route under /api/peer/* (token
# guarded). The catch-all that does the mirroring is not an endpoint of its
# own, so it is not expected in the documentation.
MIRROR_ROUTES = ("/api/.*",)


def paths_in_doc():
    """Every /api/... path the document mentions.

    A path may be written on its own (`/api/x`), together with the method
    (`GET /api/x`) or with a query string - so every /api/ token counts. A
    coverage check that is too strict only produces false alarms; one that is
    too loose misses a route. This one is deliberately loose.
    """
    doc = io.open(DOC, encoding="utf-8").read()
    found = set()
    for m in re.finditer(r"/api/[A-Za-z0-9_./{}*-]*", doc):
        p = m.group(0).rstrip("/.,")
        if len(p) > 5:
            found.add(p)
    return found


def wait_health(port, timeout=30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            conn.request("GET", "/api/health")
            if conn.getresponse().status == 200:
                return True
        except OSError:
            pass
        time.sleep(0.2)
    return False


def get(port, path, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    conn.request("GET", path, headers=headers or {})
    resp = conn.getresponse()
    body = resp.read()
    return resp.status, dict(resp.getheaders()), body


def main():
    binary = (sys.argv[1] if len(sys.argv) > 1
              else os.path.join(REPO_ROOT, "build", "bin", "tea_agent"))

    failures = []
    checks = []

    def check(name, ok, detail=""):
        checks.append(name)
        print(("PASS " if ok else "FAIL ") + name + (("  " + str(detail)) if not ok else ""))
        if not ok:
            failures.append(name)

    # ---- 1) every route is written down -----------------------------------
    routes = routes_in_code()
    doc_paths = paths_in_doc()
    doc_canon = set(canon(p) for p in doc_paths)
    code_canon = set(canon(p) for p in routes)
    missing = []
    for path in sorted(routes):
        if path in MIRROR_ROUTES:
            continue
        if canon(path) in doc_canon:
            continue
        # A doc entry may name a family of routes ("/api/workspaces/{id}").
        if any(c.startswith(canon(path)) or canon(path).startswith(c)
               for c in doc_canon):
            continue
        missing.append(path)
    check("every route is mentioned in docs/api.md", not missing, missing)

    # ---- 2) the endpoint serves it -----------------------------------------
    if not os.path.exists(binary):
        print("binary not found:", binary)
        return 1
    tmp = tempfile.mkdtemp(prefix="tea_doc_")
    os.makedirs(os.path.join(tmp, "ws"))
    config = os.path.join(tmp, "provider.yaml")
    with open(config, "w", encoding="utf-8") as f:
        f.write("""version: 1
providers:
  mock:
    api_url: http://127.0.0.1:1
    api_key: sk-test
    default_model: m
    models:
      m:
        max_context_tokens: 32768
""")
    port = 8400 + (os.getpid() % 500)
    proc = subprocess.Popen([
        binary, "--config", config, "--tea-home", os.path.join(tmp, "home"),
        "--host", "127.0.0.1", "--port", str(port), "--no-console",
        "--log-file", os.path.join(tmp, "tea.log"),
        "--workspace", os.path.join(tmp, "ws"),
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if not wait_health(port):
            print("server did not become healthy")
            return 1

        status, headers, body = get(port, "/api/doc")
        check("GET /api/doc answers", status == 200, status)
        check("it is a web page",
              "text/html" in headers.get("Content-Type", ""), headers.get("Content-Type"))
        check("the page has the reference in it",
              b"HTTP" in body and b"/api/health" in body, len(body))

        status, headers, body = get(port, "/api/doc?format=md")
        check("?format=md answers", status == 200, status)
        check("?format=md is served as markdown",
              "text/markdown" in headers.get("Content-Type", ""),
              headers.get("Content-Type"))
        on_disk = io.open(DOC, "rb").read()
        check("the binary carries the very same text",
              body == on_disk,
              "the binary has %d bytes, the file %d - rebuild it"
              % (len(body), len(on_disk)))

        status, headers, body = get(port, "/api/doc", {"Accept": "text/markdown"})
        check("Accept: text/markdown asks for the markdown",
              status == 200 and "text/markdown" in headers.get("Content-Type", ""),
              (status, headers.get("Content-Type")))

        status, headers, body = get(port, "/api/doc?format=json")
        check("?format=json answers", status == 200, status)
        try:
            payload = json.loads(body)
        except ValueError:
            payload = {}
        check("the JSON form carries the version", bool(payload.get("version")), payload.keys())
        check("the JSON form carries the markdown",
              payload.get("markdown") == on_disk.decode("utf-8"),
              len(payload.get("markdown", "")))
        check("the JSON form carries the rendered page",
              "<html" in (payload.get("html") or ""), len(payload.get("html", "")))

        status, headers, body = get(port, "/api/doc?format=xml")
        check("an unknown format is refused", status == 400, (status, body[:120]))
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            proc.kill()

    print()
    print("%d checks, %d failed" % (len(checks), len(failures)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
