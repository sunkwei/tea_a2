#!/usr/bin/env python3
"""Smoke test for the "销毁" (destroy) action.

The settings page can delete every trace this agent left on the machine:
`$TEA_AGENT_HOME` (provider.yaml, tea_agent.yaml, workspaces.json, the global
log - which is *open* while the process runs) and each indexed workspace's
`.tea_agent_run` (sessions, trajectories, memory, agent.md, per-workspace log).
Project directories are never touched.

What this pins:

  * GET /api/destroy previews exactly the paths a wipe would delete, including
    workspaces whose directory is already gone (reported as `exists: false`);
  * POST without an explicit confirmation is refused with 400 and deletes
    nothing - this is not something a stray request may trigger;
  * POST while a turn is running is refused with 409;
  * POST {"confirm": "destroy"} removes tea home (so provider.yaml is gone) and
    every registered `.tea_agent_run`, while the project directories and the
    files in them survive;
  * the running process degrades to its first-run state instead of breaking:
    /api/health reports config_loaded=false, /api/config/status says
    needs_setup, /api/workspaces is empty;
  * a second POST is idempotent (nothing removed, everything skipped);
  * a workspace added again after the wipe works (the in-memory index was
    cleared, so nothing is resurrected from a stale entry).

Usage: python3 tests/smoke_destroy.py [path-to-tea_agent-binary]
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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class MockProvider(BaseHTTPRequestHandler):
    slow = 1.5

    def log_message(self, *args):
        pass

    def do_POST(self):
        if self.path != "/v1/chat/completions":
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        time.sleep(MockProvider.slow)
        for payload in (
            {"id": "c1", "choices": [{"index": 0, "delta": {"content": "ok"}, "finish_reason": None}]},
            {"id": "c1", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        ):
            try:
                self.wfile.write(("data: " + json.dumps(payload) + "\n\n").encode())
                self.wfile.flush()
            except OSError:
                return
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


def call(port, method, path, body=None, timeout=30):
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


def main():
    binary = (sys.argv[1] if len(sys.argv) > 1
              else os.path.join(REPO_ROOT, "build", "bin", "tea_agent"))
    if not os.path.exists(binary):
        print("binary not found:", binary)
        return 1

    mock = ThreadingHTTPServer(("127.0.0.1", 0), MockProvider)
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    mock_port = mock.server_address[1]

    tmp = tempfile.mkdtemp(prefix="tea_destroy_")
    agent_port = free_port()
    home = os.path.join(tmp, "home")
    ws_a = os.path.join(tmp, "proj_a")
    ws_b = os.path.join(tmp, "proj_b")
    for d in (home, ws_a, ws_b):
        os.makedirs(d)
    # A project file that must survive: the wipe only removes .tea_agent_run.
    with open(os.path.join(ws_a, "main.c"), "w", encoding="utf-8") as f:
        f.write("int main(void){return 0;}\n")
    # The global log lives inside the tea home and is held open by the process.
    log_path = os.path.join(home, "logs", "tea_agent.log")
    config_path = os.path.join(home, "provider.yaml")
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
""" % mock_port)

    proc = subprocess.Popen([
        binary, "--config", config_path, "--tea-home", home,
        "--host", "127.0.0.1", "--port", str(agent_port), "--no-console",
        "--log-file", log_path, "--workspace", ws_a, "--workspace", ws_b,
    ])
    failures = []
    checks = []

    def check(name, ok, detail=""):
        checks.append(name)
        print(("PASS " if ok else "FAIL ") + name + (("  " + str(detail)) if not ok else ""))
        if not ok:
            failures.append(name)

    run_a = os.path.join(ws_a, ".tea_agent_run")
    run_b = os.path.join(ws_b, ".tea_agent_run")

    try:
        if not wait_health(agent_port):
            print("server did not become healthy")
            return 1
        status, listing = call(agent_port, "GET", "/api/workspaces")
        ids = [w["id"] for w in listing.get("workspaces", [])]
        check("both workspaces are registered", len(ids) == 2, listing)

        # Give the workspaces some state to lose.
        for run in (run_a, run_b):
            os.makedirs(os.path.join(run, "sessions"), exist_ok=True)
            with open(os.path.join(run, "sessions", "s_test.json"), "w", encoding="utf-8") as f:
                f.write("{}")
        shutil.rmtree(run_b)  # one workspace already lost its state directory

        # ---- preview ------------------------------------------------------
        status, preview = call(agent_port, "GET", "/api/destroy")
        paths = [t.get("path") for t in preview.get("targets", [])]
        check("GET /api/destroy succeeds", status == 200, (status, preview))
        check("the preview starts with the tea home",
              preview.get("tea_home") == home and paths and paths[0] == home, paths)
        check("both workspace run dirs are listed",
              paths.count(run_a) == 1 and paths.count(run_b) == 1, paths)
        kinds = {t.get("kind") for t in preview.get("targets", [])}
        check("the preview labels the kinds", kinds == {"tea_home", "workspace"}, kinds)
        gone = [t for t in preview.get("targets", []) if t.get("path") == run_b]
        check("a missing run dir is reported, not hidden",
              len(gone) == 1 and gone[0].get("exists") is False, gone)

        # ---- a running turn blocks the wipe --------------------------------
        _, sess = call(agent_port, "POST", "/api/workspaces/%s/sessions" % ids[0],
                       {"title": "destroy-guard"})
        sid = (sess or {}).get("id")

        def run_turn():
            try:
                conn = http.client.HTTPConnection("127.0.0.1", agent_port, timeout=60)
                body = json.dumps({"workspace_id": ids[0], "session_id": sid,
                                   "message": "slow please"})
                conn.request("POST", "/api/chat", body=body,
                             headers={"Content-Type": "application/json"})
                conn.getresponse().read()
            except Exception:  # noqa: BLE001 - the body is irrelevant here
                pass

        t = threading.Thread(target=run_turn, daemon=True)
        t.start()
        running = False
        for _ in range(60):
            _, turn = call(agent_port, "GET", "/api/chat/turn?session_id=" + sid)
            if turn and turn.get("running"):
                running = True
                break
            time.sleep(0.1)
        check("a turn is running before the guarded POST", running)
        status, err = call(agent_port, "POST", "/api/destroy", {"confirm": "destroy"})
        check("POST refuses to wipe under a running turn",
              status == 409 and (err.get("error") or {}).get("code") == "turn_running",
              (status, err))
        check("the refused wipe deleted nothing",
              os.path.isfile(config_path) and os.path.isdir(run_a), (config_path, run_a))
        call(agent_port, "POST", "/api/chat/stop", {"session_id": sid})
        t.join(timeout=30)

        # ---- unconfirmed POST ---------------------------------------------
        status, err = call(agent_port, "POST", "/api/destroy", {})
        check("POST without confirmation is refused",
              status == 400 and (err.get("error") or {}).get("code") == "not_confirmed",
              (status, err))
        check("the unconfirmed POST deleted nothing",
              os.path.isfile(config_path) and os.path.isdir(run_a), (config_path, run_a))

        # ---- the wipe ------------------------------------------------------
        status, done = call(agent_port, "POST", "/api/destroy",
                            {"confirm": "destroy"}, timeout=60)
        check("POST with confirmation succeeds", status == 200 and done.get("ok") is True,
              (status, done))
        check("the tea home is gone", not os.path.exists(home), home)
        check("provider.yaml is gone", not os.path.exists(config_path), config_path)
        check("the global log is gone", not os.path.exists(log_path), log_path)
        check("the workspace run dirs are gone",
              not os.path.exists(run_a) and not os.path.exists(run_b), (run_a, run_b))
        check("the project directories survive",
              os.path.isdir(ws_a) and os.path.isdir(ws_b), (ws_a, ws_b))
        check("project files survive",
              os.path.isfile(os.path.join(ws_a, "main.c")), ws_a)

        # ---- the process degrades to first-run state -----------------------
        _, health = call(agent_port, "GET", "/api/health")
        check("health reports an unconfigured agent", health.get("config_loaded") is False,
              health)
        _, cfg = call(agent_port, "GET", "/api/config/status")
        check("config status asks for setup",
              cfg.get("loaded") is False and cfg.get("needs_setup") is True
              and cfg.get("path", "").endswith("provider.yaml"), cfg)
        _, after = call(agent_port, "GET", "/api/workspaces")
        check("the workspace index is empty in memory",
              after.get("workspaces") == [], after)

        # ---- idempotent ----------------------------------------------------
        status, again = call(agent_port, "POST", "/api/destroy", {"confirm": "destroy"})
        # The index was cleared by the first wipe, so only the tea home is left
        # to skip; nothing is re-created just to be deleted again.
        check("a second wipe is a no-op",
              status == 200 and again.get("ok") is True
              and again.get("removed") == [] and again.get("skipped") == [home],
              (status, again))

        # ---- adding a workspace afterwards works ---------------------------
        status, created = call(agent_port, "POST", "/api/workspaces",
                               {"path": ws_a, "name": "again"})
        check("a workspace can be added again after the wipe",
              status in (200, 201) and os.path.isdir(
                  os.path.join(ws_a, ".tea_agent_run")), (status, created))
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
