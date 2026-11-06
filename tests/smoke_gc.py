#!/usr/bin/env python3
"""Smoke test for the **history cleaner** (GC): age, budget and compression.

The history of a workspace (session transcripts + turn trajectories) is the one
thing in a run_dir that only ever grows. This test pins what the cleaner
promises:

  * a dry run reports what would happen and changes nothing;
  * sessions and trajectories older than the age limit are removed, and the
    session index follows;
  * cold data is gzipped instead of deleted - and stays readable (the API
    returns the very same transcript);
  * deleting a session takes its trajectory with it (no orphans on disk);
  * a run_dir over its byte budget loses its oldest sessions until it fits;
  * the cleaner can be told to do nothing (limits of 0 = off).

Usage: python3 tests/smoke_gc.py [path-to-tea_agent-binary]
"""

import http.client
import json
import os
import random
import shutil
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

DAY = 86400


class MockProvider(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        try:
            self.rfile.read(length)
        except OSError:
            pass
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        try:
            self.wfile.write(b'data: {"id":"c1","choices":[{"index":0,"delta":{"content":"ok"},'
                             b'"finish_reason":"stop"}]}\n\n')
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


def call(port, method, path, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    headers = {"Content-Type": "application/json"} if body is not None else {}
    conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
    resp = conn.getresponse()
    raw = resp.read()
    try:
        return resp.status, json.loads(raw or b"null")
    except ValueError:
        return resp.status, {"raw": raw.decode("utf-8", "replace")}


def run_turn(port, workspace_id, session_id, message):
    """One full SSE turn; returns the events."""
    body = {"workspace_id": workspace_id, "session_id": session_id, "message": message}
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=120)
    conn.request("POST", "/api/chat", body=json.dumps(body),
                 headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    events = []
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
            events.append(json.loads(data))
        except ValueError:
            continue
    return events


def write_trajectory(run_dir, session_id, turns=2):
    """A trajectory file with a couple of turns in it."""
    traj_dir = os.path.join(run_dir, "trajectories")
    if not os.path.isdir(traj_dir):
        os.makedirs(traj_dir)
    path = os.path.join(traj_dir, session_id + ".jsonl")
    with open(path, "w", encoding="utf-8") as f:
        for i in range(turns):
            f.write(json.dumps({
                "turn_id": "t_%s_%d" % (session_id[2:8], i),
                "session_id": session_id,
                "steps": [{"tool": "exec", "output": "hello %d" % i}],
            }) + "\n")
    return path


def age(path, days):
    """Make a file look like it was last touched `days` ago."""
    when = time.time() - days * DAY
    os.utime(path, (when, when))


def main():
    binary = (sys.argv[1] if len(sys.argv) > 1
              else os.path.join(REPO_ROOT, "build", "bin", "tea_agent"))
    if not os.path.exists(binary):
        print("binary not found:", binary)
        return 1

    mock = ThreadingHTTPServer(("127.0.0.1", 0), MockProvider)
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    mock_port = mock.server_address[1]

    tmp = tempfile.mkdtemp(prefix="tea_gc_")
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
        if not ok:
            failures.append((name, detail))
            print("FAIL: %s  %s" % (name, detail))
        else:
            print("ok  : %s" % name)

    try:
        if not wait_health(agent_port):
            print("the agent did not come up")
            return 1
        status, payload = call(agent_port, "GET", "/api/workspaces")
        ws_id = (payload.get("workspaces") or [{}])[0].get("id")
        check("the workspace is registered", bool(ws_id), str(payload))
        run_dir = os.path.join(ws_dir, ".tea_agent_run")

        # ---- three sessions, each with a trajectory ----------------------
        sids = []
        for i in range(3):
            status, payload = call(agent_port, "POST",
                                   "/api/workspaces/%s/sessions" % ws_id,
                                   {"title": "gc session %d" % i})
            sid = (payload or {}).get("session_id") or (payload or {}).get("id")
            if not sid:
                status, payload = call(agent_port, "GET",
                                       "/api/workspaces/%s/sessions" % ws_id)
                sid = payload["sessions"][0]["id"]
            sids.append(sid)
            write_trajectory(run_dir, sid)
        check("three sessions exist", len(set(sids)) == 3, str(sids))

        sessions_dir = os.path.join(run_dir, "sessions")
        old_sid = sids[0]
        old_file = os.path.join(sessions_dir, old_sid + ".json")
        old_traj = os.path.join(run_dir, "trajectories", old_sid + ".jsonl")
        age(old_file, 40)
        age(old_traj, 40)

        # ---- a dry run changes nothing ----------------------------------
        status, report = call(agent_port, "POST", "/api/workspaces/%s/gc" % ws_id,
                              {"dry_run": True, "max_age_days": 30})
        check("the dry run answers 200", status == 200, str(report))
        check("the dry run says what it would do",
              report.get("sessions_removed") == 1, str(report))
        check("the dry run changes nothing", os.path.exists(old_file))
        check("the dry run keeps the trajectory", os.path.exists(old_traj))

        # ---- the age limit removes the old session and its turns ---------
        status, report = call(agent_port, "POST", "/api/workspaces/%s/gc" % ws_id,
                              {"max_age_days": 30})
        check("the cleaner answers 200", status == 200, str(report))
        check("the old session is gone", not os.path.exists(old_file))
        check("its trajectory is gone too (no orphans)", not os.path.exists(old_traj))
        check("the report counts what it removed",
              report.get("sessions_removed") == 1 and report.get("trajectories_removed") == 1,
              str(report))
        status, payload = call(agent_port, "GET", "/api/workspaces/%s/sessions" % ws_id)
        listed = [s.get("id") for s in payload.get("sessions") or []]
        check("the session index follows", old_sid not in listed, str(listed))
        check("the other sessions are still there",
              sids[1] in listed and sids[2] in listed, str(listed))

        # ---- cold data is gzipped, not lost ------------------------------
        cold_sid = sids[1]
        cold_file = os.path.join(sessions_dir, cold_sid + ".json")
        cold_traj = os.path.join(run_dir, "trajectories", cold_sid + ".jsonl")
        age(cold_file, 40)
        age(cold_traj, 40)
        status, report = call(agent_port, "POST", "/api/workspaces/%s/gc" % ws_id,
                              {"compress_after_days": 30})
        check("the compression answers 200", status == 200, str(report))
        check("the cold session is gzipped", os.path.exists(cold_file + ".gz"))
        check("the plain copy is gone", not os.path.exists(cold_file))
        check("the cold trajectory is gzipped", os.path.exists(cold_traj + ".gz"))
        check("the compression saved space",
              report.get("compressed_saved", 0) > 0, str(report))
        # and it is still readable
        # The compressed file is a normal gzip file: gunzip reads it.
        import gzip
        try:
            with gzip.open(cold_file + ".gz", "rb") as f:
                plain = json.loads(f.read().decode("utf-8"))
            check("the compressed file is a normal gzip file", plain.get("id") == cold_sid,
                  str(plain)[:120])
        except Exception as exc:      # noqa: BLE001 - the message is the point
            check("the compressed file is a normal gzip file", False, repr(exc))
        status, payload = call(agent_port, "GET", "/api/sessions/%s" % cold_sid)
        check("a gzipped session is still readable", status == 200, str(payload)[:200])
        status, payload = call(agent_port, "GET", "/api/sessions/%s/trajectory" % cold_sid)
        check("a gzipped trajectory is still readable",
              status == 200 and len(payload or []) > 0, str(payload)[:200])
        status, payload = call(agent_port, "GET",
                               "/api/workspaces/%s/sessions/search?q=gc%%20session" % ws_id)
        check("the search still finds the gzipped session",
              (payload.get("count") or 0) > 0, str(payload)[:200])

        # ---- a cold session that becomes hot again is unpacked ------------
        events = run_turn(agent_port, ws_id, cold_sid, "hello again")
        check("the turn ran", any(e.get("type") == "done" for e in events),
              str(events)[:200])
        check("the transcript is plain again", os.path.exists(cold_file))
        check("its compressed copy is gone", not os.path.exists(cold_file + ".gz"))
        check("the trajectory is plain again",
              os.path.exists(os.path.join(run_dir, "trajectories", cold_sid + ".jsonl")))
        check("its compressed copy is gone",
              not os.path.exists(os.path.join(run_dir, "trajectories", cold_sid + ".jsonl.gz")))
        status, payload = call(agent_port, "GET", "/api/sessions/%s/trajectory" % cold_sid)
        check("the old turns are still in the trail",
              status == 200 and len(payload or []) >= 2, str(payload)[:200])

        # ---- deleting a session takes its trajectory with it -------------
        victim = sids[2]
        victim_traj = os.path.join(run_dir, "trajectories", victim + ".jsonl")
        check("the victim has a trajectory", os.path.exists(victim_traj))
        status, payload = call(agent_port, "DELETE", "/api/sessions/%s" % victim)
        check("the session is deleted", status == 200, str(payload))
        check("its trajectory went with it", not os.path.exists(victim_traj))

        # ---- the byte budget --------------------------------------------
        fat_sid = "s_fatfatfat"
        fat_file = os.path.join(sessions_dir, fat_sid + ".json")
        with open(fat_file, "w", encoding="utf-8") as f:
            f.write(json.dumps({"id": fat_sid, "title": "fat", "created_at": "2020-01-01T00:00:00Z",
                                "updated_at": "2020-01-01T00:00:00Z",
                                "messages": [{"role": "user", "content": "x" * 200000}]}))
        age(fat_file, 90)
        status, report = call(agent_port, "POST", "/api/workspaces/%s/gc" % ws_id,
                              {"max_bytes": 4096, "keep_recent": 0})
        check("the budget answers 200", status == 200, str(report))
        check("the fat session went first", not os.path.exists(fat_file), str(report))
        check("the report counts it", report.get("sessions_removed", 0) >= 1, str(report))

        # ---- limits of 0 mean "do nothing" -------------------------------
        status, report = call(agent_port, "POST", "/api/workspaces/%s/gc" % ws_id,
                              {"max_age_days": 0, "max_bytes": 0, "compress_after_days": 0})
        check("a cleaner without limits is a no-op",
              status == 200 and report.get("sessions_removed") == 0 and
              report.get("sessions_compressed") == 0, str(report))
        status, payload = call(agent_port, "GET", "/api/workspaces/%s/sessions" % ws_id)
        listed = [x.get("id") for x in payload.get("sessions") or []]
        check("the cleaned session is still listed", cold_sid in listed, str(listed))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        mock.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n%d checks, %d failures" % (len(checks), len(failures)))
    for name, detail in failures:
        print("  FAILED: %s  %s" % (name, detail))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
