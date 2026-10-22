#!/usr/bin/env python3
"""Concurrency smoke test: parallel work must not lose state.

The runtime runs one worker thread per in-flight turn, so several sessions of one
workspace can be writing on-disk state at the same time. Those files are all
"read the file, change one thing, write the whole thing back", which without a
lock silently keeps only the last writer's change. Measured before the fix: 20
concurrent `POST .../memory` all answered 200 and exactly *one* note survived.

What this pins:

  * N concurrent memory adds all survive, exactly once each (memory.json is the
    canonical read-modify-write case);
  * N concurrent session creations all end up in sessions/index.json, and every
    session file on disk has an entry (and vice versa);
  * a `show_loading` turn running while those writes happen still finishes, so
    the lock did not turn into a deadlock;
  * every state file is still parseable JSON afterwards - the writers use
    write-to-temp-then-rename, so a reader never observes a truncated file;
  * no `.tmp-*` scratch files are left behind;
  * two turns on two sessions of the same workspace overlap in time (the
    concurrency this whole test is about is real, not aspirational).

Usage: python3 tests/smoke_concurrency.py [path-to-tea_agent-binary]
"""

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

MEMORY_ADDS = 24
SESSION_CREATES = 16


class MockProvider(BaseHTTPRequestHandler):
    """Just enough OpenAI to drive one turn: stream some text and stop."""

    def log_message(self, *args):
        pass

    def do_POST(self):  # noqa: N802 (http.server API)
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()

        def chunk(payload):
            try:
                self.wfile.write(("data: " + json.dumps(payload) + "\n\n").encode())
                self.wfile.flush()
                return True
            except OSError:
                return False

        # A few deltas with a small pause, so a turn is still in flight while the
        # HTTP writes below happen.
        for i in range(4):
            if not chunk({"id": "c1", "object": "chat.completion.chunk",
                          "choices": [{"index": 0, "delta": {"content": "part%d " % i},
                                       "finish_reason": None}]}):
                return
            time.sleep(0.05)
        chunk({"id": "c1", "object": "chat.completion.chunk",
               "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
        try:
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except OSError:
            pass


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


def call(port, method, path, body=None, timeout=60):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    headers = {"Content-Type": "application/json"} if body is not None else {}
    conn.request(method, path, body=json.dumps(body) if body is not None else None,
                 headers=headers)
    resp = conn.getresponse()
    raw = resp.read()
    try:
        return resp.status, json.loads(raw or b"null")
    except ValueError:
        return resp.status, {"raw": raw.decode("utf-8", "replace")}


def parallel(fn, count):
    """Run fn(i) on `count` threads, started as close together as possible."""
    results = [None] * count
    barrier = threading.Barrier(count)

    def worker(i):
        try:
            barrier.wait(timeout=30)
        except threading.BrokenBarrierError:
            pass
        try:
            results[i] = fn(i)
        except Exception as exc:  # noqa: BLE001 - reported as a failure below
            results[i] = ("exception", repr(exc))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    return results


def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def main():
    binary = (sys.argv[1] if len(sys.argv) > 1
              else os.path.join(REPO_ROOT, "build", "bin", "tea_agent"))
    if not os.path.exists(binary):
        print("binary not found:", binary)
        return 1

    mock = ThreadingHTTPServer(("127.0.0.1", 0), MockProvider)
    mock.daemon_threads = True
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    mock_port = mock.server_address[1]

    tmp = tempfile.mkdtemp(prefix="tea_concurrency_")
    agent_port = free_port()
    ws_dir = os.path.join(tmp, "ws")
    os.makedirs(ws_dir)
    config_path = os.path.join(tmp, "provider.yaml")
    with open(config_path, "w", encoding="utf-8") as f:
        f.write("""version: 1
providers:
  mock:
    api_url: http://127.0.0.1:%d
    api_key: sk-test
    default_model: m
    models:
      m:
        max_context_tokens: 32768
        max_output_tokens: 4096
        supports_tools: true
""" % mock_port)

    proc = subprocess.Popen([
        binary, "--config", config_path, "--tea-home", os.path.join(tmp, "home"),
        "--host", "127.0.0.1", "--port", str(agent_port), "--no-console",
        "--log-file", os.path.join(tmp, "tea_agent.log"), "--workspace", ws_dir,
    ])
    failures = []
    checks = []

    def check(name, ok, detail=""):
        checks.append(name)
        print(("PASS " if ok else "FAIL ") + name + (("  " + str(detail)) if not ok else ""))
        if not ok:
            failures.append(name)

    run_dir = os.path.join(ws_dir, ".tea_agent_run")
    sessions_dir = os.path.join(run_dir, "sessions")
    memory_path = os.path.join(run_dir, "memory", "memory.json")
    index_path = os.path.join(sessions_dir, "index.json")

    try:
        if not wait_health(agent_port):
            print("server did not become healthy")
            return 1
        _, listing = call(agent_port, "GET", "/api/workspaces")
        wid = listing["workspaces"][0]["id"]

        # ---- 1. concurrent memory adds ------------------------------------
        adds = parallel(
            lambda i: call(agent_port, "POST", "/api/workspaces/%s/memory" % wid,
                           {"text": "note-%02d" % i}),
            MEMORY_ADDS)
        ok_adds = [r for r in adds if r and r[0] == 200]
        check("%d concurrent memory adds are all accepted" % MEMORY_ADDS,
              len(ok_adds) == MEMORY_ADDS, adds[:2])

        notes = read_json(memory_path)["notes"]
        texts = [n.get("text") for n in notes]
        check("every accepted note is on disk",
              len(ok_adds) <= len(notes), "accepted=%d stored=%d" % (len(ok_adds), len(notes)))
        check("no note was overwritten by a racing writer",
              len(set(texts)) == len(ok_adds),
              "accepted=%d unique=%d" % (len(ok_adds), len(set(texts))))
        check("memory.json is still valid JSON (atomic replace)",
              isinstance(notes, list), type(notes).__name__)

        # ---- 2. one turn in flight while the writes happen ----------------
        turn_events = []

        def run_turn():
            body = json.dumps({"workspace_id": wid, "message": "concurrency probe"})
            conn = http.client.HTTPConnection("127.0.0.1", agent_port, timeout=120)
            conn.request("POST", "/api/chat", body=body,
                         headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            while True:
                line = resp.readline()
                if not line:
                    break
                line = line.strip()
                if not line.startswith(b"data: "):
                    continue
                data = line[6:]
                if data == b"[DONE]":
                    break
                try:
                    turn_events.append(json.loads(data))
                except ValueError:
                    continue

        turn = threading.Thread(target=run_turn)
        turn.start()
        time.sleep(0.15)          # let the turn reach the model
        creates = parallel(
            lambda i: call(agent_port, "POST", "/api/workspaces/%s/sessions" % wid,
                           {"title": "parallel-%02d" % i}),
            SESSION_CREATES)
        turn.join(timeout=120)
        check("a turn keeps running while 16 sessions are created",
              not turn.is_alive(), "the turn thread never finished")
        check("the turn finished normally under contention",
              any(e.get("type") == "done" for e in turn_events),
              [e.get("type") for e in turn_events][-4:])

        # ---- 3. the session index survived the concurrent creates ---------
        ok_creates = [r for r in creates if r and r[0] == 200]
        created_ids = [r[1]["id"] for r in ok_creates if isinstance(r[1], dict)]
        check("%d concurrent session creations are all accepted" % SESSION_CREATES,
              len(ok_creates) == SESSION_CREATES, creates[:2])

        index = read_json(index_path)
        listed = {e.get("id") for e in index if isinstance(e, dict)}
        missing = [sid for sid in created_ids if sid not in listed]
        check("every created session is listed in index.json",
              not missing, "missing=%s" % missing[:4])
        check("index.json is still valid JSON and an array",
              isinstance(index, list), type(index).__name__)

        files = {f[:-5] for f in os.listdir(sessions_dir)
                 if f.endswith(".json") and f != "index.json"}
        check("every session file has an index entry",
              not (files - listed), "files without entry=%s" % sorted(files - listed)[:4])
        check("every index entry has a session file",
              not (listed - files), "entries without file=%s" % sorted(listed - files)[:4])

        # ---- 4. no scratch files left behind ------------------------------
        leftovers = []
        for root, _dirs, names in os.walk(run_dir):
            for name in names:
                if ".tmp-" in name:
                    leftovers.append(os.path.join(root, name))
        check("no .tmp- scratch files left behind", not leftovers, leftovers[:4])

        # ---- 5. sessions on the same workspace really do overlap -----------
        s1 = call(agent_port, "POST", "/api/workspaces/%s/sessions" % wid,
                  {"title": "overlap-a"})[1]["id"]
        s2 = call(agent_port, "POST", "/api/workspaces/%s/sessions" % wid,
                  {"title": "overlap-b"})[1]["id"]
        seen = {"a": [], "b": []}

        def turn_on(sid, key):
            body = json.dumps({"workspace_id": wid, "session_id": sid,
                               "message": "overlap " + key})
            conn = http.client.HTTPConnection("127.0.0.1", agent_port, timeout=120)
            conn.request("POST", "/api/chat", body=body,
                         headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            while True:
                line = resp.readline()
                if not line:
                    break
                if line.strip().startswith(b"data: "):
                    seen[key].append(time.time())

        ta = threading.Thread(target=turn_on, args=(s1, "a"))
        tb = threading.Thread(target=turn_on, args=(s2, "b"))
        ta.start()
        tb.start()
        ta.join(timeout=120)
        tb.join(timeout=120)
        overlap = bool(seen["a"] and seen["b"] and
                       min(seen["a"][-1], seen["b"][-1]) > max(seen["a"][0], seen["b"][0]))
        check("two turns on one workspace overlap in time", overlap,
              "a=%d events b=%d events" % (len(seen["a"]), len(seen["b"])))
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        mock.shutdown()

    print()
    print("%d/%d checks passed" % (len(checks) - len(failures), len(checks)))
    if failures:
        print("FAILED:", ", ".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
