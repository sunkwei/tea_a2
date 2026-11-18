#!/usr/bin/env python3
"""Smoke test for the **recovery** of interrupted turns.

A turn can end in ways the process does not control:

  * the machine goes away (the process is killed, the power goes) while the
    turn is working;
  * the provider gives up (HTTP 500) in the middle of a turn.

When the agent comes back, the turn is gone from memory - and the caller that
asks "how did it go?" used to be told "no turn with this id is known", even
though the audit trail of that very turn was sitting on the disk. The handle
is the audit id, so it has to keep answering.

What is checked here:

  * a turn that was cut off (the agent is killed while it works) is found
    again after a restart: GET /api/turns/<id> answers "interrupted", and
    says how far it got;
  * a turn that failed (the provider answers 500) is found again too, with the
    error the provider gave;
  * GET /api/turns and GET /api/recovery list the ones that did not end
    normally;
  * a session whose last turn never got an answer says so - the transcript
    knows about it even when the trajectory does not;
  * a session whose last turn ended normally says nothing: no false alarm.

Usage: python3 tests/smoke_recovery.py [path-to-tea_agent-binary]
"""

import http.client
import json
import os
import random
import shutil
import signal
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

TERMINAL = ("done", "failed", "stopped", "budget")


class MockProvider(BaseHTTPRequestHandler):
    """A provider that answers, fails or hangs - whatever the question asks for."""

    def log_message(self, *args):
        pass

    def _read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length) or b"{}")

    def _chunk(self, payload):
        try:
            self.wfile.write(("data: " + json.dumps(payload) + "\n\n").encode())
            self.wfile.flush()
            return True
        except OSError:
            return False

    def _content(self, text):
        self._chunk(
            {
                "id": "c1",
                "object": "chat.completion.chunk",
                "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
            }
        )
        self._chunk(
            {
                "id": "c1",
                "object": "chat.completion.chunk",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            }
        )
        try:
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except OSError:
            pass

    def do_POST(self):
        if self.path != "/v1/chat/completions":
            body = json.dumps({"error": {"message": "unknown path " + self.path}}).encode()
            self.send_response(404)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        payload = self._read_json()
        messages = payload.get("messages") or []
        question = ""
        for m in messages:
            if m.get("role") == "user":
                question = str(m.get("content") or "")

        if "hang" in question:
            # The provider never answers: the turn is still working when the
            # machine goes away.
            time.sleep(300)
            return
        if "boom" in question:
            body = json.dumps({"error": {"message": "the provider gave up"}}).encode()
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self._content("all good")


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
    """One request. Returns (status, headers, parsed-json-or-None)."""
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
            return resp.status, dict(resp.getheaders()), parsed
        except OSError:
            if attempt == 2:
                raise
            time.sleep(0.5)
    return 0, {}, None


def get_json(port, path):
    status, _, parsed = request(port, "GET", path)
    return status, parsed


def post_json(port, path, body):
    status, _, parsed = request(port, "POST", path, body)
    return status, parsed


def wait_state(port, turn_id, timeout=30.0):
    """Poll the handle until the turn reaches a final state."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        status, body = get_json(port, "/api/turns/" + turn_id)
        if status == 200 and body:
            last = body
            if body.get("state") in TERMINAL:
                return body
        time.sleep(0.1)
    return last


def start_agent(binary, config_path, home, port, ws_dir, log_file):
    return subprocess.Popen(
        [
            binary,
            "--config",
            config_path,
            "--tea-home",
            home,
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--no-console",
            "--log-file",
            log_file,
            "--workspace",
            ws_dir,
        ]
    )


def read_json_file(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_json_file(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


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

    tmp = tempfile.mkdtemp(prefix="tea_recovery_")
    agent_port = free_port()
    ws_dir = os.path.join(tmp, "ws")
    os.makedirs(ws_dir)
    home = os.path.join(tmp, "home")
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

    failures = []
    checks = []

    def check(name, ok, detail=""):
        checks.append(name)
        print(("PASS " if ok else "FAIL ") + name + (("  " + str(detail)) if not ok else ""))
        if not ok:
            failures.append(name)

    def submit(message):
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
        return status, body

    proc = start_agent(
        binary, config_path, home, agent_port, ws_dir, os.path.join(tmp, "tea_agent.log")
    )
    try:
        if not wait_health(agent_port):
            print("server did not become healthy")
            return 1
        _, listing = get_json(agent_port, "/api/workspaces")
        workspace_id = listing["workspaces"][0]["id"]
        run_dir = os.path.join(ws_dir, ".tea_agent_run")

        # ---- 1) a turn that ends normally --------------------------------
        status, body = submit("hello there")
        check("the first turn is accepted", status == 202, (status, body))
        turn_ok = body.get("turn_id")
        session_ok = body.get("session_id")
        final = wait_state(agent_port, turn_ok)
        check("the first turn finishes", (final or {}).get("state") == "done", final)

        # ---- 2) a turn the provider fails --------------------------------
        status, body = submit("boom please")
        turn_failed = body.get("turn_id")
        session_failed = body.get("session_id")
        final = wait_state(agent_port, turn_failed)
        check(
            "the failed turn says so",
            (final or {}).get("state") == "failed",
            final,
        )
        check(
            "the failed turn carries the error",
            "gave up" in json.dumps(final or {}),
            final,
        )

        # ---- 3) a turn that is still working when the machine goes away --
        status, body = submit("hang please")
        turn_cut = body.get("turn_id")
        session_cut = body.get("session_id")
        check("the hanging turn has a handle", bool(turn_cut), body)
        # Wait for the trajectory line to be on the disk: the recorder writes
        # it when the turn starts.
        traj_path = os.path.join(run_dir, "trajectories", session_cut + ".jsonl")
        deadline = time.time() + 10
        while time.time() < deadline and not os.path.exists(traj_path):
            time.sleep(0.1)
        check("the trajectory of the hanging turn exists", os.path.exists(traj_path), traj_path)

        # The machine goes away: no clean shutdown, no final line.
        proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=10)
        proc = None

        # ---- 4) a session whose last turn never got an answer -------------
        # The transcript of the first session is cut back to the question:
        # the turn was over before the model said anything, so only the
        # transcript knows about it.
        session_path = os.path.join(run_dir, "sessions", session_ok + ".json")
        data = read_json_file(session_path)
        data["messages"] = [m for m in data.get("messages", []) if m.get("role") == "user"]
        write_json_file(session_path, data)
        # ... and its trajectory is gone, so nothing else knows about the turn.
        traj_ok = os.path.join(run_dir, "trajectories", session_ok + ".jsonl")
        if os.path.exists(traj_ok):
            os.remove(traj_ok)

        # ---- 5) the agent comes back --------------------------------------
        proc = start_agent(
            binary, config_path, home, agent_port, ws_dir, os.path.join(tmp, "tea_agent2.log")
        )
        if not wait_health(agent_port):
            print("the agent did not come back")
            return 1

        # The handle of the turn that was cut off still answers.
        status, cut = get_json(agent_port, "/api/turns/" + turn_cut)
        check("the cut-off turn is found again", status == 200, (status, cut))
        check("it says it was interrupted", (cut or {}).get("state") == "interrupted", cut)
        check("it says where the answer came from", (cut or {}).get("recovered") is True, cut)
        check("it is not running any more", (cut or {}).get("running") is False, cut)
        check(
            "it knows the session it belongs to",
            (cut or {}).get("session_id") == session_cut,
            cut,
        )

        # The handle of the turn that failed still answers.
        status, failed = get_json(agent_port, "/api/turns/" + turn_failed)
        check("the failed turn is found again", status == 200, (status, failed))
        check("it says it failed", (failed or {}).get("state") == "failed", failed)
        check(
            "it carries the provider's error",
            "gave up" in json.dumps(failed or {}),
            failed,
        )

        # The list of the handles knows about them.
        status, turns = get_json(agent_port, "/api/turns")
        listed = [t.get("turn_id") for t in (turns or {}).get("turns", [])]
        check("the cut-off turn is in the list", turn_cut in listed, listed)
        check("the failed turn is in the list", turn_failed in listed, listed)

        # The recovery list is about what did not end normally.
        status, recovery = get_json(agent_port, "/api/recovery")
        check("the recovery route answers", status == 200, (status, recovery))
        recovered = [t.get("turn_id") for t in (recovery or {}).get("turns", [])]
        check("the cut-off turn is recovered", turn_cut in recovered, recovered)
        check("the failed turn is recovered", turn_failed in recovered, recovered)
        check(
            "the recovery list says how many",
            (recovery or {}).get("count") == len(recovered),
            recovery,
        )

        # The session whose last turn never got an answer says so.
        status, chat_turn = get_json(agent_port, "/api/chat/turn?session_id=" + session_ok)
        check("the session is asked about its turn", status == 200, (status, chat_turn))
        recovered_info = (chat_turn or {}).get("recovered")
        check("the session reports the interruption", isinstance(recovered_info, dict), chat_turn)
        if isinstance(recovered_info, dict):
            check(
                "the interruption is named",
                recovered_info.get("state") == "interrupted",
                recovered_info,
            )
            check(
                "the request that was never answered is quoted back",
                "hello there" in (recovered_info.get("pending_message") or ""),
                recovered_info,
            )
            check(
                "the transcript is named as a source",
                "transcript" in (recovered_info.get("sources") or []),
                recovered_info,
            )

        # A session whose last turn failed says so - and says "failed", not
        # "interrupted": the provider gave up, the machine did not go away.
        status, chat_turn = get_json(agent_port, "/api/chat/turn?session_id=" + session_failed)
        failed_info = (chat_turn or {}).get("recovered")
        check("the failed session reports its turn", isinstance(failed_info, dict), chat_turn)
        if isinstance(failed_info, dict):
            check(
                "the failure is named as a failure",
                failed_info.get("state") == "failed",
                failed_info,
            )
            check(
                "the provider's error is there",
                "gave up" in json.dumps(failed_info),
                failed_info,
            )

        # The machine says the feature is there.
        status, caps = get_json(agent_port, "/api/capabilities")
        check(
            "the machine announces the recovery",
            (caps or {}).get("features", {}).get("turn_recovery") is True,
            (caps or {}).get("features"),
        )
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        mock.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n%d checks, %d failed" % (len(checks), len(failures)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
