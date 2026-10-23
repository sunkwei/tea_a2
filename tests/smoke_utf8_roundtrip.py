#!/usr/bin/env python3
"""Every byte string that reaches a JSON document must still be valid UTF-8.

The JSON parser accepts bytes that are not valid UTF-8 inside a string; the
serialiser refuses them ("invalid UTF-8 byte at index N"), and that used to
kill the whole turn. The text that goes into a document comes from many
places, and each of them can be cut at a byte boundary:

  * the model's own answer (a gateway that re-encodes can put a raw byte in
    a string - the parser takes it, the serialiser does not);
  * a compaction transcript, cut per message and as a whole;
  * a file the agent read, a command's output, a file name.

What this pins:

  * a model that streams a raw byte does not crash the turn, the event frames
    are valid JSON, and the session on disk is valid UTF-8;
  * the request the provider receives is always valid UTF-8 - also when the
    history is compacted (the transcript is cut at 4000 bytes per message and
    at 60000 bytes as a whole, both of which used to be able to split a
    character);
  * the tool call's id and name are cleaned as well.

Usage: python3 tests/smoke_utf8_roundtrip.py [path-to-tea_agent-binary]
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

# A lead byte followed by a space: the shape the crash report had (the byte
# the message names is the one *after* the broken sequence).
BROKEN = b"\xe4 "


class MockProvider(BaseHTTPRequestHandler):
    """Answers with a raw byte in the text, and watches what it is sent."""

    # The first scenario wants the raw byte; the compaction scenario wants
    # clean answers, so that a replacement character in the request can only
    # come from a cut.
    emit_broken = True

    def log_message(self, *args):
        pass

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        # The request itself has to be valid UTF-8 - that is the whole point.
        try:
            raw.decode("utf-8")
            self.server.requests.append({"valid": True, "body": raw})
        except UnicodeDecodeError:
            self.server.requests.append({"valid": False, "body": raw})
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            body = {}
        streaming = bool(body.get("stream"))
        self.send_response(200)
        if streaming:
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            # A raw byte inside a JSON string: the parser refuses it, so the
            # agent has to clean the bytes before it parses the event.
            if MockProvider.emit_broken:
                self.wfile.write(b'data: {"id":"c","choices":[{"index":0,"delta":'
                                 b'{"content":"broken ' + BROKEN + b'byte"}}]}\n\n')
            self.wfile.write(b'data: {"id":"c","choices":[{"index":0,"delta":'
                             b'{"content":" and the rest of the answer."}}]}\n\n')
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        else:
            # The summary request (compaction): a plain answer.
            payload = json.dumps({"id": "c", "choices": [
                {"index": 0, "message": {"role": "assistant", "content": "这是摘要。"},
                 "finish_reason": "stop"}]}).encode("utf-8")
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)


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


def run_turn(port, workspace_id, message, session_id=None):
    body = {"workspace_id": workspace_id, "message": message}
    if session_id:
        body["session_id"] = session_id
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=120)
    conn.request("POST", "/api/chat", body=json.dumps(body),
                 headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    events = []
    broken = []
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
            events.append(json.loads(data.decode("utf-8")))
        except (ValueError, UnicodeDecodeError):
            broken.append(data[:200])
    return events, broken


def session_files(ws_dir):
    out = []
    sessions = os.path.join(ws_dir, ".tea_agent_run", "sessions")
    if not os.path.isdir(sessions):
        return out
    for name in sorted(os.listdir(sessions)):
        if name.endswith(".json"):
            out.append(os.path.join(sessions, name))
    return out


def main():
    binary = (sys.argv[1] if len(sys.argv) > 1
              else os.path.join(REPO_ROOT, "build", "bin", "tea_agent"))
    if not os.path.exists(binary):
        print("binary not found:", binary)
        return 1

    mock = ThreadingHTTPServer(("127.0.0.1", 0), MockProvider)
    mock.requests = []
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    mock_port = mock.server_address[1]

    tmp = tempfile.mkdtemp(prefix="tea_utf8rt_")
    port = free_port()
    ws_dir = os.path.join(tmp, "ws")
    os.makedirs(ws_dir)
    config = os.path.join(tmp, "provider.yaml")
    with open(config, "w", encoding="utf-8") as f:
        f.write("""version: 1
providers:
  mock:
    api_url: http://127.0.0.1:%d
    api_key: sk-test
    default_model: m
    models:
      m:
        max_context_tokens: 20000
        max_output_tokens: 2000
        supports_tools: true
""" % mock_port)

    proc = subprocess.Popen([
        binary, "--config", config, "--tea-home", os.path.join(tmp, "home"),
        "--host", "127.0.0.1", "--port", str(port), "--no-console",
        "--log-file", os.path.join(tmp, "tea.log"), "--workspace", ws_dir,
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    failures = []
    checks = []

    def check(name, ok, detail=""):
        checks.append(name)
        print(("PASS " if ok else "FAIL ") + name + (("  " + str(detail)) if not ok else ""))
        if not ok:
            failures.append(name)

    try:
        if not wait_health(port):
            print("server did not become healthy")
            return 1
        _, listing = call(port, "GET", "/api/workspaces")
        wid = listing["workspaces"][0]["id"]

        # ---- a model that streams a raw byte ---------------------------
        events, broken = run_turn(port, wid, "说点什么")
        errors = [e for e in events if e.get("type") == "error"]
        check("a raw byte from the model does not crash the turn", not errors,
              errors[0].get("message") if errors else "")
        check("every event frame is valid JSON", not broken, broken[:1])
        deltas = "".join(e.get("text") or "" for e in events if e.get("type") == "content_delta")
        check("the raw byte became the replacement character", "�" in deltas, repr(deltas[:80]))
        check("the rest of the answer is there", "rest of the answer" in deltas, repr(deltas[:80]))

        # ---- the request the provider received --------------------------
        bad = [r for r in mock.requests if not r["valid"]]
        check("every request to the provider was valid UTF-8", not bad,
              bad[0]["body"][:120] if bad else "")

        # ---- the session on disk ----------------------------------------
        broken_files = []
        for path in session_files(ws_dir):
            with open(path, "rb") as f:
                blob = f.read()
            try:
                blob.decode("utf-8")
            except UnicodeDecodeError:
                broken_files.append(os.path.basename(path))
        check("the session file on disk is valid UTF-8", not broken_files, broken_files)

        # ---- a history big enough to be compacted -----------------------
        # The window is tiny (1500 tokens), so the next turn compacts: the
        # transcript is cut per message (4000 bytes) and as a whole (60000),
        # and both cuts have to land on a character boundary.
        long_cjk = "这是一段很长的中文内容，用来把历史撑到需要压缩的程度。" * 600
        # From here on the answers are clean: a replacement character in what
        # the provider receives can then only come from a cut.
        MockProvider.emit_broken = False
        first, _ = run_turn(port, wid, "先说一句")
        # The compaction runs at the *start* of a turn, on the history the
        # previous turns left behind - so the turns have to share one session,
        # there have to be a few of them (it never folds the last few messages,
        # compaction_keep_recent), and the big message has to be in the middle
        # (folding the first message would leave nothing behind).
        session_id = ""
        for e in first:
            if e.get("type") == "start" and e.get("session_id"):
                session_id = e["session_id"]
        check("the turn reported its session", bool(session_id), first[:1])
        # Two long messages: the one that ends up in the summarised part is the
        # one that gets cut at 4000 bytes per message - which is exactly the
        # cut that used to land in the middle of a character.
        run_turn(port, wid, long_cjk, session_id=session_id)
        run_turn(port, wid, "中间说一句", session_id=session_id)
        run_turn(port, wid, long_cjk, session_id=session_id)
        for i in range(2):
            run_turn(port, wid, "再说一句 %d" % i, session_id=session_id)
        events, broken = run_turn(port, wid, "再总结一下", session_id=session_id)
        errors = [e for e in events if e.get("type") == "error"]
        check("a turn that compacts does not crash", not errors,
              errors[0].get("message") if errors else "")
        check("every event frame of that turn is valid JSON", not broken, broken[:1])
        bad = [r for r in mock.requests if not r["valid"]]
        check("the request after the compaction was valid UTF-8", not bad,
              bad[0]["body"][:120] if bad else "")
        summaries = [r for r in mock.requests if b'"stream":' not in r["body"]]
        check("the history was summarised", len(summaries) >= 1, len(mock.requests))
        # A cut in the middle of a character would show up as U+FFFD in what the
        # provider got: the request is valid UTF-8 either way (the serialiser
        # replaces the bad byte), but the character is lost.
        broken_char = [r for r in mock.requests if "\ufffd" in r["body"].decode("utf-8", "replace")]
        check("no character was cut in half on the way to the provider",
              not broken_char,
              broken_char[0]["body"][:160] if broken_char else "")
        broken_files = []
        for path in session_files(ws_dir):
            with open(path, "rb") as f:
                blob = f.read()
            try:
                blob.decode("utf-8")
            except UnicodeDecodeError:
                broken_files.append(os.path.basename(path))
        check("the session file is still valid UTF-8 after the compaction",
              not broken_files, broken_files)
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        mock.shutdown()

    print("")
    print("%d/%d checks passed" % (len(checks) - len(failures), len(checks)))
    if failures:
        print("FAILED: " + ", ".join(failures))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
