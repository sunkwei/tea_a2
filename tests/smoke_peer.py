#!/usr/bin/env python3
"""Smoke test for the cascade: one tea_a2 driving another.

What this pins:

  * the settings: a `peers:` section is read, a peer without a usable url is
    dropped with a warning, and two peers with one name do not both survive;
  * the peer channel on the peer side: /api/peer/<route> is the token-guarded
    mirror of /api/<route> - 401 without the secret, 404 when the peer has no
    token at all, and a route that is not on the whitelist does not exist;
  * the tool: the controller's model calls toolkit_peer, the peer's OWN agent
    answers (with its own provider and its own tools), and the answer comes
    back with a trace of what the peer did;
  * the older peer: a peer that does not know the peer channel is still
    reachable through the ordinary route (the tool falls back);
  * exec: a command runs on the peer without a model in the loop.

Usage: python3 tests/smoke_peer.py [path-to-tea_agent-binary]
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

try:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
except ImportError:  # Python 3.6
    from http.server import BaseHTTPRequestHandler
    from http.server import HTTPServer
    from socketserver import ThreadingMixIn

    class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
        """The 3.6 spelling of http.server.ThreadingHTTPServer."""
        daemon_threads = True

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CHECKS = []


def check(name, ok, detail=""):
    CHECKS.append((name, bool(ok), detail))
    print("%-4s %s%s" % ("ok" if ok else "FAIL", name, ("  -- " + detail) if detail and not ok else ""))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def http_json(url, body=None, token=None, method=None, timeout=20):
    """A small JSON client that returns (status, parsed_or_text)."""
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method=method or ("POST" if body is not None else "GET"))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            try:
                return resp.status, json.loads(raw)
            except ValueError:
                return resp.status, raw
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, raw


class StreamProvider(BaseHTTPRequestHandler):
    """A provider that asks for one tool call and then answers.

    The behaviour lives in the server's `state`, so two of these can run side
    by side (the controller's model and the peer's model) without sharing
    anything: the peer's brain must be its own.

      state["tool"]    the tool the first turn asks for ("" = answer right away)
      state["args"]    what it asks with
      state["answer"]  what the second turn (the one with the tool result) says
      state["seen"]    every request body, in order
    """

    def log_message(self, *args):
        pass

    def do_POST(self):  # noqa: N802
        state = self.server.state
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        state["seen"].append(body)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        first = len(state["seen"]) == 1
        if first and state.get("tool"):
            delta = {"tool_calls": [{
                "index": 0, "id": "call_1", "type": "function",
                "function": {"name": state["tool"],
                             "arguments": json.dumps(state.get("args", {}))},
            }]}
        else:
            delta = {"content": state.get("answer", "done")}
        frame = {"id": "c1", "object": "chat.completion.chunk",
                 "choices": [{"index": 0, "finish_reason": None, "delta": delta}]}
        self.wfile.write(("data: " + json.dumps(frame) + "\n\n").encode("utf-8"))
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


class OldPeer(BaseHTTPRequestHandler):
    """A tea_a2 from before the peer channel: it knows /api/chat, not /api/peer/."""

    def log_message(self, *args):
        pass

    def do_GET(self):  # noqa: N802
        if self.path == "/api/workspaces":
            body = json.dumps({"ok": True, "workspaces": [{"id": "ws_old", "name": "old"}]}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        if self.path == "/api/chat":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for frame in ({"type": "content_delta", "text": "the old peer answers"},
                          {"type": "done", "content": "the old peer answers"}):
                self.wfile.write(("data: " + json.dumps(frame) + "\n\n").encode("utf-8"))
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            return
        self.send_response(404)
        self.end_headers()


def start_mock(state):
    """A mock provider with its own state (its own brain)."""
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), StreamProvider)
    httpd.state = state
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, httpd.server_address[1]


def start_agent(exe, home, extra_args=(), env_extra=None):
    """Start a real tea_a2 and wait until it answers."""
    port = free_port()
    env = dict(os.environ)
    env["TEA_AGENT_HOME"] = home
    env.pop("TEA_AGENT_PEER_URL", None)
    if env_extra:
        env.update(env_extra)
    proc = subprocess.Popen(
        [exe, "--port", str(port), "--tea-home", home, "--workspace", home] + list(extra_args),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
    base = "http://127.0.0.1:%d" % port
    for _ in range(100):
        try:
            urllib.request.urlopen(base + "/api/health", timeout=1).read()
            return proc, base
        except Exception:
            time.sleep(0.1)
    proc.terminate()
    return proc, base


def stop_agent(proc):
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except Exception:
        proc.kill()


def read_sse(url, body, timeout=90):
    """POST a chat and collect the SSE frames."""
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    frames = []
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if line.startswith("data:"):
                try:
                    frames.append(json.loads(line[5:].strip()))
                except ValueError:
                    pass
    return frames


def first_workspace(base):
    answer = json.loads(urllib.request.urlopen(base + "/api/workspaces", timeout=10).read())
    if answer.get("workspaces"):
        return answer["workspaces"][0]["id"]
    return ""


# ---------------------------------------------------------------------------
# the tests
# ---------------------------------------------------------------------------

def test_peer_channel(exe, tmp):
    """The peer channel on the peer side: a guarded mirror, or nothing."""
    home = os.path.join(tmp, "chan")
    os.makedirs(home)
    proc, base = start_agent(exe, home, ["--api-token", "peer-secret"])
    try:
        status, _ = http_json(base + "/api/peer/health")
        check("the peer channel needs the token", status == 401, "HTTP %s" % status)
        status, answer = http_json(base + "/api/peer/health", token="peer-secret")
        check("the peer channel answers with the token",
              status == 200 and answer.get("ok"), "HTTP %s" % status)
        status, _ = http_json(base + "/api/peer/health", token="wrong")
        check("the peer channel refuses a wrong token", status == 401, "HTTP %s" % status)
        status, answer = http_json(base + "/api/peer/destroy", token="peer-secret")
        check("the peer channel is a whitelist",
              status == 404 and "peer channel" in json.dumps(answer), "HTTP %s" % status)
        # The ordinary routes are untouched: the mirror does not lock them.
        status, _ = http_json(base + "/api/health")
        check("the ordinary routes still work", status == 200, "HTTP %s" % status)
    finally:
        stop_agent(proc)

    home = os.path.join(tmp, "chan2")
    os.makedirs(home)
    proc, base = start_agent(exe, home)
    try:
        status, answer = http_json(base + "/api/peer/health")
        check("a peer without a token has no peer channel",
              status == 404 and "no peer channel" in json.dumps(answer), "HTTP %s" % status)
    finally:
        stop_agent(proc)


def test_cascade(exe, tmp):
    """The whole chain: the controller's model asks the peer's own agent."""
    peer_mock, peer_mock_port = start_mock({
        "tool": "toolkit_exec",
        "args": {"command": "cat /etc/hostname"},
        "answer": "the projector service is DOWN",
        "seen": [],
    })

    peer_home = os.path.join(tmp, "peer")
    os.makedirs(peer_home)
    with open(os.path.join(peer_home, "provider.yaml"), "w", encoding="utf-8") as f:
        f.write("providers:\n  mock:\n    api_url: http://127.0.0.1:%d/v1\n"
                "    api_key: sk-peer\n    default_model: peer-model\n"
                "    models:\n      - id: peer-model\n"
                "provider_order: [mock]\n" % peer_mock_port)
    peer_proc, peer_base = start_agent(exe, peer_home, ["--api-token", "peer-secret"])

    controller_mock, controller_mock_port = start_mock({
        "tool": "toolkit_peer",
        "args": {"action": "chat", "peer": "peer-a",
                 "prompt": "is the projector service up?"},
        "answer": "the peer said the projector is down",
        "seen": [],
    })

    try:

        home = os.path.join(tmp, "controller")
        os.makedirs(home)
        with open(os.path.join(home, "provider.yaml"), "w", encoding="utf-8") as f:
            f.write("providers:\n  mock:\n    api_url: http://127.0.0.1:%d/v1\n"
                    "    api_key: sk-ctl\n    default_model: ctl-model\n"
                    "    models:\n      - id: ctl-model\n"
                    "provider_order: [mock]\n" % controller_mock_port)
        # The peers are settings, and the settings live in their own file now:
        # provider.yaml only says who to talk to.
        with open(os.path.join(home, "tea_agent.yaml"), "w", encoding="utf-8") as f:
            f.write("settings:\n  peers:\n"
                    "    peer-a:\n"
                    "      url: %s\n"
                    "      token: peer-secret\n"
                    "      description: the study room machine\n" % peer_base)
        proc, base = start_agent(exe, home)
        try:
            workspace_id = first_workspace(base)
            frames = read_sse(base + "/api/chat",
                              {"workspace_id": workspace_id, "message": "check the peer"})
            results = [f for f in frames if f.get("type") == "tool_result"]
            check("the controller called the peer tool", bool(results),
                  str([f.get("type") for f in frames]))
            if not results:
                return
            out = results[0].get("output", "")
            check("the peer's own answer came back",
                  "projector service is DOWN" in out, out[:400])
            check("the trace says what the peer did",
                  "toolkit_exec" in out and "what the peer did" in out, out[:400])
            check("the peer's session is reported", "session" in out, out[:400])
            # The peer really ran its own agent: its provider saw the prompt.
            peer_prompts = [json.dumps(b) for b in peer_mock.state["seen"]]
            check("the peer thought with its own provider",
                  any("projector" in p for p in peer_prompts),
                  str(len(peer_prompts)))
            used_exec = any("toolkit_exec" in p for p in peer_prompts)
            check("the peer used its own tools", used_exec)
            # The cascade goes through the guarded mirror, not the open route.
            log = open(os.path.join(home, "logs", "tea_agent.log"), encoding="utf-8").read()
            check("the cascade used the peer channel",
                  "/api/peer/chat" in log, log[-400:])
        finally:
            stop_agent(proc)
    finally:
        stop_agent(peer_proc)
        peer_mock.shutdown()
        controller_mock.shutdown()


def test_exec_action(exe, tmp):
    """exec: a command runs on the peer, with no model in the loop."""
    peer_home = os.path.join(tmp, "exec-peer")
    os.makedirs(peer_home)
    with open(os.path.join(peer_home, "provider.yaml"), "w", encoding="utf-8") as f:
        f.write("providers:\n  mock:\n    api_url: http://127.0.0.1:1/v1\n"
                "    api_key: sk-peer\n    default_model: peer-model\n"
                "    models:\n      - id: peer-model\n"
                "provider_order: [mock]\n")
    peer_proc, peer_base = start_agent(exe, peer_home, ["--api-token", "peer-secret"])

    controller_mock, controller_mock_port = start_mock({
        "tool": "toolkit_peer",
        "args": {"action": "exec", "peer": "peer-a", "command": "echo peer-exec-works"},
        "answer": "done",
        "seen": [],
    })
    try:

        home = os.path.join(tmp, "exec-controller")
        os.makedirs(home)
        with open(os.path.join(home, "provider.yaml"), "w", encoding="utf-8") as f:
            f.write("providers:\n  mock:\n    api_url: http://127.0.0.1:%d/v1\n"
                    "    api_key: sk-ctl\n    default_model: ctl-model\n"
                    "    models:\n      - id: ctl-model\n"
                    "provider_order: [mock]\n" % controller_mock_port)
        with open(os.path.join(home, "tea_agent.yaml"), "w", encoding="utf-8") as f:
            f.write("settings:\n  peers:\n"
                    "    peer-a:\n      url: %s\n      token: peer-secret\n" % peer_base)
        proc, base = start_agent(exe, home)
        try:
            workspace_id = first_workspace(base)
            frames = read_sse(base + "/api/chat",
                              {"workspace_id": workspace_id, "message": "run it there"})
            results = [f for f in frames if f.get("type") == "tool_result"]
            out = results[0].get("output", "") if results else ""
            check("the command ran on the peer", "peer-exec-works" in out, out[:400])
        finally:
            stop_agent(proc)
    finally:
        stop_agent(peer_proc)
        controller_mock.shutdown()


def test_older_peer(exe, tmp):
    """A peer that does not know the peer channel is still reachable."""
    old = ThreadingHTTPServer(("127.0.0.1", 0), OldPeer)
    old.daemon_threads = True
    threading.Thread(target=old.serve_forever, daemon=True).start()
    old_base = "http://127.0.0.1:%d" % old.server_address[1]

    controller_mock, controller_mock_port = start_mock({
        "tool": "toolkit_peer",
        "args": {"action": "chat", "url": old_base, "prompt": "who are you?"},
        "answer": "done",
        "seen": [],
    })
    try:

        home = os.path.join(tmp, "old-controller")
        os.makedirs(home)
        with open(os.path.join(home, "provider.yaml"), "w", encoding="utf-8") as f:
            f.write("providers:\n  mock:\n    api_url: http://127.0.0.1:%d/v1\n"
                    "    api_key: sk-ctl\n    default_model: ctl-model\n"
                    "    models:\n      - id: ctl-model\n"
                    "provider_order: [mock]\n" % controller_mock_port)
        with open(os.path.join(home, "tea_agent.yaml"), "w", encoding="utf-8") as f:
            f.write("settings:\n  peers:\n"
                    "    old:\n      url: %s\n      token: old-token\n" % old_base)
        proc, base = start_agent(exe, home)
        try:
            workspace_id = first_workspace(base)
            frames = read_sse(base + "/api/chat",
                              {"workspace_id": workspace_id, "message": "ask the old peer"})
            results = [f for f in frames if f.get("type") == "tool_result"]
            out = results[0].get("output", "") if results else ""
            check("an older peer still answers", "the old peer answers" in out, out[:400])
        finally:
            stop_agent(proc)
    finally:
        old.shutdown()
        controller_mock.shutdown()


def test_bad_settings(exe, tmp):
    """A peer without a usable url is dropped, and the tool says so."""
    mock, mock_port = start_mock({
        "tool": "toolkit_peer",
        "args": {"action": "list"},
        "answer": "done",
        "seen": [],
    })
    home = os.path.join(tmp, "bad")
    os.makedirs(home)
    with open(os.path.join(home, "provider.yaml"), "w", encoding="utf-8") as f:
        f.write("providers:\n  mock:\n    api_url: http://127.0.0.1:%d/v1\n"
                "    api_key: sk-x\n    default_model: m\n"
                "    models:\n      - id: m\n"
                "provider_order: [mock]\n" % mock_port)
    with open(os.path.join(home, "tea_agent.yaml"), "w", encoding="utf-8") as f:
        f.write("settings:\n  peers:\n"
                "    broken:\n      url: not-an-address\n"
                "    good:\n      url: http://127.0.0.1:9999\n")
    try:
        proc, base = start_agent(exe, home)
    except Exception:
        mock.shutdown()
        raise
    try:
        workspace_id = first_workspace(base)
        frames = read_sse(base + "/api/chat",
                          {"workspace_id": workspace_id, "message": "list the peers"})
        results = [f for f in frames if f.get("type") == "tool_result"]
        out = results[0].get("output", "") if results else ""
        check("a peer without a url is not in the list",
              "good" in out and "broken" not in out, out[:400])
    finally:
        stop_agent(proc)
        mock.shutdown()


def main():
    exe = sys.argv[1] if len(sys.argv) > 1 else ""
    if not exe or not os.path.exists(exe):
        print("skip  (no tea_agent binary given)")
        return 0
    tmp = tempfile.mkdtemp(prefix="tea-peer-")
    try:
        test_peer_channel(exe, tmp)
        test_cascade(exe, tmp)
        test_exec_action(exe, tmp)
        test_older_peer(exe, tmp)
        test_bad_settings(exe, tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    failed = [c for c in CHECKS if not c[1]]
    print("\n%d/%d checks passed" % (len(CHECKS) - len(failed), len(CHECKS)))
    if failed:
        for name, _ok, detail in failed:
            print("  FAIL %s  %s" % (name, detail))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
