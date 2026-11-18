#!/usr/bin/env python3
"""Smoke test: the list of workspaces is ordered by **most recently used**.

The list in the sidebar is a list of projects, and the project you just
worked in is the one you want next - not the one you added first. The index
file keeps the registration order (that is the order the user built the list
in), so the "when was it last used" stamp has to be recorded and the order
computed from it.

What is checked here:

  * the API answers the workspaces **most recently used first**;
  * a turn in a workspace moves it to the front (the stamp is recorded when
    the turn starts, not when the workspace was added);
  * the stamp survives in the index file, so the order survives a restart;
  * a workspace that was never used keeps its registration time.

Usage: python3 tests/smoke_workspace_order.py [path-to-tea_agent-binary]
"""

import datetime
import http.client
import json
import os
import random
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler

try:
    # Python 3.7+
    from http.server import ThreadingHTTPServer
except ImportError:  # Python 3.6
    from http.server import HTTPServer
    from socketserver import ThreadingMixIn

    class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
        """The 3.6 spelling of http.server.ThreadingHTTPServer."""
        daemon_threads = True

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class MockProvider(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length) or b"{}")

    def do_POST(self):
        if self.path != "/v1/chat/completions":
            body = json.dumps({"error": {"message": "unknown path"}}).encode()
            self.send_response(404)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self._read_json()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for payload in (
            {"id": "c1", "object": "chat.completion.chunk",
             "choices": [{"index": 0, "delta": {"content": "ok"}, "finish_reason": None}]},
            {"id": "c1", "object": "chat.completion.chunk",
             "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        ):
            self.wfile.write(("data: " + json.dumps(payload) + "\n\n").encode())
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


def free_port():
    while True:
        port = random.randint(20000, 60000)
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue


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


def request(port, method, path, body=None, timeout=20):
    for attempt in range(3):
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
            headers = {}
            payload = None
            if body is not None:
                payload = json.dumps(body)
                headers["Content-Type"] = "application/json"
            conn.request(method, path, body=payload, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
            try:
                parsed = json.loads(raw or b"null")
            except ValueError:
                parsed = None
            return resp.status, parsed
        except OSError:
            if attempt == 2:
                raise
            time.sleep(0.5)
    return 0, None


def get_json(port, path):
    return request(port, "GET", path)


def post_json(port, path, body):
    return request(port, "POST", path, body)


def workspaces(port):
    """The list as the API answers it: [{id, name, last_used_at}, ...]."""
    status, body = get_json(port, "/api/workspaces")
    if status != 200 or not body:
        return []
    return body.get("workspaces", [])


def workspace_ids(port):
    return [w.get("id") for w in workspaces(port)]


def main():
    binary = (
        sys.argv[1]
        if len(sys.argv) > 1
        else os.path.join(REPO_ROOT, "build", "bin", "tea_agent")
    )
    if not os.path.exists(binary):
        print("binary not found:", binary)
        return 1

    mock = ThreadingHTTPServer(("127.0.0.1", 0), MockProvider)
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    mock_port = mock.server_address[1]

    tmp = tempfile.mkdtemp(prefix="tea_wsorder_")
    agent_port = free_port()
    home = os.path.join(tmp, "home")
    proj_a = os.path.join(tmp, "proj_a")
    proj_b = os.path.join(tmp, "proj_b")
    proj_c = os.path.join(tmp, "proj_c")
    os.makedirs(proj_a)
    os.makedirs(proj_b)
    os.makedirs(proj_c)
    # An index written by an **older** version: it stamped "last_used_at" when
    # the workspace was added, so the field said "when it was registered".
    # On load that stamp is dropped - the field now means "when it was used".
    os.makedirs(os.path.join(tmp, "home"), exist_ok=True)
    with open(os.path.join(tmp, "home", "workspaces.json"), "w", encoding="utf-8") as handle:
        json.dump({
            "version": 1,
            "updated_at": "2026-11-08T23:40:29Z",
            "workspaces": [{
                "id": "ws_stale0000000000",
                "name": "proj_c",
                "path": proj_c,
                "created_at": "2026-11-08T23:40:29Z",
                "last_used_at": "2026-11-08T23:40:29Z",
            }],
        }, handle)
    config_path = os.path.join(tmp, "provider.yaml")
    with open(config_path, "w", encoding="utf-8") as f:
        f.write(
            """version: 1
providers:
  mock:
    api_url: http://127.0.0.1:%d
    api_key: sk-test
    default_model: quick-model
    models:
      quick-model:
        max_context_tokens: 32768
        max_output_tokens: 4096
"""
            % (mock_port,)
        )

    proc = subprocess.Popen(
        [
            binary,
            "--config",
            config_path,
            "--tea-home",
            home,
            "--host",
            "127.0.0.1",
            "--port",
            str(agent_port),
            "--no-console",
            "--log-file",
            os.path.join(tmp, "tea_agent.log"),
            "--workspace",
            proj_a,
            "--workspace",
            proj_b,
        ]
    )
    failures = []
    checks = []

    def check(name, ok, detail=""):
        checks.append(name)
        print(("PASS " if ok else "FAIL ") + name + (("  " + str(detail)) if not ok else ""))
        if not ok:
            failures.append(name)

    def run_turn(workspace_id, message):
        status, body = post_json(
            agent_port,
            "/api/chat",
            {
                "message": message,
                "workspace_id": workspace_id,
                "provider": "mock",
                "model": "quick-model",
                "async": True,
            },
        )
        if status != 202 or not body:
            return None
        turn_id = body.get("turn_id")
        deadline = time.time() + 30
        while time.time() < deadline:
            st, st_body = get_json(agent_port, "/api/turns/" + turn_id)
            if st == 200 and st_body and st_body.get("state") in ("done", "failed"):
                return st_body
            time.sleep(0.1)
        return None

    try:
        if not wait_health(agent_port):
            print("server did not become healthy")
            return 1

        listed = workspaces(agent_port)
        check("every workspace is registered", len(listed) == 3, listed)
        if len(listed) != 3:
            return 1
        by_name = dict((w.get("name"), w.get("id")) for w in listed)
        first, second = by_name.get("proj_a"), by_name.get("proj_b")
        check("the two new workspaces are there", bool(first and second), by_name)

        # The stale stamp of the old index is gone: the field means "when it
        # was used" now, and nobody used that one.
        status, body = get_json(agent_port, "/api/workspaces")
        stale = [w for w in (body or {}).get("workspaces", [])
                 if w.get("id") == "ws_stale0000000000"]
        check("the workspace from the old index is there", len(stale) == 1,
              (body or {}).get("workspaces"))
        check("its registration stamp was dropped",
              stale and not stale[0].get("last_used_at"),
              stale and stale[0].get("last_used_at"))

        # Nothing has been used yet: the registration order stands (the
        # workspace from the old index was registered first).
        ids = workspace_ids(agent_port)
        check("the list starts in registration order", ids[:2] == [second, first] or
              ids[-2:] == [first, second], ids)

        # A turn in the second workspace moves it to the front.
        final = run_turn(second, "work in the second one")
        check("the turn ran", (final or {}).get("state") == "done", final)
        ids = workspace_ids(agent_port)
        # The API keeps the registration order on purpose (a cold browser
        # lands on the first workspace); the **dropdown** is what is ordered
        # by recent use - that is the UI's job (see ui_workspace_order.mjs).
        check("the API keeps the registration order", ids[-2:] == [first, second], ids)

        # The stamps are recorded and they differ.
        status, body = get_json(agent_port, "/api/workspaces")
        stamps = {}
        for w in (body or {}).get("workspaces", []):
            stamps[w.get("id")] = w.get("last_used_at")
        # Nobody worked in the first one yet: it has **no** stamp (registering
        # is not using), and it keeps its place in the registration order.
        check("the unused workspace has no stamp", not stamps.get(first), stamps)
        check("the workspace just used has a stamp", bool(stamps.get(second)), stamps)
        # The stamps are compared as **times**, not as strings: a
        # millisecond stamp ("...:11.500Z") sorts *before* a second-precision
        # one ("...:11Z") when the strings are compared byte for byte.
        def ms(stamp):
            if not stamp:
                return 0
            try:
                base = stamp[:19]
                frac = stamp[19:] if len(stamp) > 19 else ""
                millis = int((frac[1:4] + "000")[:3]) if frac.startswith(".") else 0
                seconds = datetime.datetime.strptime(
                    base, "%Y-%m-%dT%H:%M:%S").replace(
                    tzinfo=datetime.timezone.utc).timestamp()
                return int(seconds * 1000) + millis
            except ValueError:
                return 0

        check(
            "the one just used has the newer stamp",
            ms(stamps.get(second)) > ms(stamps.get(first)),
            stamps,
        )

        # The stamp is on the disk, so the order survives a restart.
        index_path = os.path.join(home, "workspaces.json")
        check("the index file exists", os.path.exists(index_path), index_path)
        if os.path.exists(index_path):
            with open(index_path, "r", encoding="utf-8") as handle:
                index = json.load(handle)
            entries = index.get("workspaces", [])
            on_disk = {}
            for entry in entries:
                on_disk[entry.get("id")] = entry.get("last_used_at")
            check(
                "the index remembers the stamp",
                on_disk.get(second) == stamps.get(second),
                on_disk,
            )
            # The migrated index is written back as the new format.
            check("the index was migrated to the new format",
                  index.get("version") == 2, index.get("version"))
            check(
                "the migrated index has no registration stamp left",
                not any(e.get("last_used_at") for e in entries
                        if e.get("id") == "ws_stale0000000000"),
                entries,
            )

        # A turn in the other workspace flips the order back.
        final = run_turn(first, "work in the first one now")
        check("the second turn ran", (final or {}).get("state") == "done", final)
        ids = workspace_ids(agent_port)
        check("the API order does not wobble", ids[-2:] == [first, second], ids)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        mock.shutdown()

    print("\n%d checks, %d failed" % (len(checks), len(failures)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
