#!/usr/bin/env python3
"""Smoke test: a picture must reach the provider exactly as it is on disk, and
a picture the provider refuses must not lock the session.

Two bugs are pinned here:

  * `read_file()` trims the edges of a file to valid UTF-8. That is right for
    text (the JSON layer refuses invalid UTF-8) and fatal for images: a JPEG
    ends with FF D9, and 0xD9 is the lead byte of a two-byte sequence, so the
    copy under .tea_agent_run/artifacts/images/ lost its last byte. The
    provider rejected the picture ("invalid params, 400 (2013)" on MiniMax,
    verified 2026-10-30 against api.minimax.cn) and the broken picture stayed
    in the session history - so every later request on that session was
    rejected the same way and the session was locked for good.
  * A provider that refuses a request carrying a picture refuses the *whole*
    request. The pictures are therefore dropped from the request and the turn
    goes on without them, instead of dying - and the session stays usable.

The mock provider plays a provider that answers 400 for every request that
carries an image (the MiniMax wording, byte for byte).
"""

import base64
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

# The MiniMax wording of "this request carries something I refuse".
REJECT_BODY = {
    "type": "error",
    "error": {
        "type": "bad_request_error",
        "message": "invalid params, 400 (2013)",
        "http_code": "400",
    },
    "request_id": "073348d2502ac5ea598af163028d43be",
}


def make_jpeg(payload=b"x" * 64):
    """A JPEG-looking file that ends with FF D9 - the byte read_file() used to
    trim (0xD9 is the lead byte of a two-byte UTF-8 sequence)."""
    return b"\xff\xd8\xff\xe0" + b"\x00" * 16 + payload + b"\xff\xd9"


class MockProvider(BaseHTTPRequestHandler):
    records = []
    steps = {}

    def log_message(self, *args):
        pass

    def _read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length) or b"{}")

    def _sse_header(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

    def _write(self, text):
        data = text.encode()
        self.wfile.write(("%x\r\n" % len(data)).encode() + data + b"\r\n")
        self.wfile.flush()

    def _chunk(self, payload):
        try:
            self._write("data: " + json.dumps(payload) + "\n\n")
            return True
        except OSError:
            return False

    def _done(self):
        try:
            self._write("data: [DONE]\n\n")
            self._write("")
        except OSError:
            pass

    def _content(self, text):
        self._sse_header()
        self._chunk({"id": "c1", "choices": [{"index": 0, "delta": {"content": text},
                                              "finish_reason": None}]})
        self._chunk({"id": "c1", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
        self._done()

    def _tool_call(self, name, args, call_id):
        self._sse_header()
        self._chunk({
            "id": "c1",
            "choices": [{
                "index": 0,
                "delta": {
                    "role": "assistant",
                    "tool_calls": [{
                        "index": 0,
                        "id": call_id,
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(args)},
                    }],
                },
                "finish_reason": None,
            }],
        })
        self._chunk({"id": "c1", "choices": [{"index": 0, "delta": {},
                                             "finish_reason": "tool_calls"}]})
        self._done()

    def _reject(self):
        body = json.dumps(REJECT_BODY).encode()
        self.send_response(400)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path != "/v1/chat/completions":
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        payload = self._read_json()
        MockProvider.records.append(payload)
        msgs = payload.get("messages") or []

        # The scenario is the text of the first user message.
        scenario = ""
        for m in msgs:
            if m.get("role") == "user":
                c = m.get("content")
                scenario = c if isinstance(c, str) else json.dumps(c)
                break
        step = MockProvider.steps.get(scenario, 0)
        MockProvider.steps[scenario] = step + 1

        # Every request that carries a picture is refused.
        if image_parts(payload):
            self._reject()
            return

        if step == 0:
            self._tool_call("toolkit_load_image", {"path": "photo.jpg"}, "call_load")
            return
        self._content("the turn is still alive")


def image_parts(request):
    """Every image_url part in the request, as (mime, payload bytes)."""
    out = []
    for m in request.get("messages") or []:
        c = m.get("content")
        if not isinstance(c, list):
            continue
        for p in c:
            if not isinstance(p, dict) or p.get("type") != "image_url":
                continue
            url = (p.get("image_url") or {}).get("url", "")
            if not url.startswith("data:"):
                continue
            head, _, payload = url.partition(";base64,")
            try:
                out.append((head[5:], base64.b64decode(payload)))
            except Exception:  # noqa: BLE001
                out.append((head[5:], b""))
    return out


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


def run_turn(port, workspace_id, session_id, scenario):
    body = {"workspace_id": workspace_id, "session_id": session_id,
            "message": "SCENARIO=" + scenario}
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


def main():
    binary = (sys.argv[1] if len(sys.argv) > 1
              else os.path.join(REPO_ROOT, "build", "bin", "tea_agent"))
    if not os.path.exists(binary):
        print("binary not found:", binary)
        return 1

    MockProvider.records = []
    MockProvider.steps = {}
    mock = ThreadingHTTPServer(("127.0.0.1", 0), MockProvider)
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    mock_port = mock.server_address[1]

    tmp = tempfile.mkdtemp(prefix="tea_imgint_")
    port = free_port()
    ws_dir = os.path.join(tmp, "ws")
    os.makedirs(ws_dir)
    log_path = os.path.join(tmp, "tea_agent.log")
    config_path = os.path.join(tmp, "provider.yaml")
    with open(config_path, "w", encoding="utf-8") as f:
        f.write("""version: 1
providers:
  mock:
    api_url: http://127.0.0.1:%d
    api_key: sk-test
    default_model: vision
    models:
      vision:
        max_context_tokens: 32768
        max_output_tokens: 4096
        supports_tools: true
        supports_vision: true
""" % mock_port)

    proc = subprocess.Popen([
        binary, "--config", config_path, "--tea-home", os.path.join(tmp, "home"),
        "--host", "127.0.0.1", "--port", str(port), "--no-console",
        "--log-file", log_path, "--workspace", ws_dir,
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    failures = []
    checks = []

    def check(name, ok, detail=""):
        checks.append(name)
        print(("PASS " if ok else "FAIL ") + name + (("  " + str(detail)) if not ok else ""))
        if not ok:
            failures.append(name)

    jpeg = make_jpeg()
    with open(os.path.join(ws_dir, "photo.jpg"), "wb") as f:
        f.write(jpeg)

    try:
        if not wait_health(port):
            print("server did not become healthy")
            return 1
        _, listing = call(port, "GET", "/api/workspaces")
        wid = listing["workspaces"][0]["id"]
        _, sess = call(port, "POST", "/api/workspaces/%s/sessions" % wid, {"title": "images"})
        sid = sess["id"]

        # ---- 1. the copy keeps every byte of the file ---------------------
        # The mock refuses every request that carries an image, so the turn
        # still shows what the agent put on the wire.
        before = len(MockProvider.records)
        events = run_turn(port, wid, sid, "load")
        check("the turn finished", any(e.get("type") == "done" for e in events), events[-2:])
        refused = [r for r in MockProvider.records[before:] if image_parts(r)]
        check("the request carried the picture", len(refused) == 1, len(refused))
        parts = image_parts(refused[0]) if refused else []
        check("the picture is a JPEG", parts and parts[0][0] == "image/jpeg",
              parts and parts[0][0])
        check("the bytes on the wire are the file's bytes",
              parts and parts[0][1] == jpeg,
              parts and "%d bytes, the file has %d" % (len(parts[0][1]), len(jpeg)))
        check("the last byte (FF D9) survived",
              parts and parts[0][1].endswith(b"\xff\xd9"),
              parts and parts[0][1][-4:].hex())

        # ---- 2. the stored copy is byte-identical --------------------------
        _, stored = call(port, "GET", "/api/sessions/%s" % sid)
        msgs = stored.get("messages") or []
        with_images = [m for m in msgs if m.get("images")]
        check("the session stored the attachment", len(with_images) == 1, len(with_images))
        img_path = (with_images[0]["images"][0] or {}).get("path", "") if with_images else ""
        on_disk = os.path.join(ws_dir, img_path.replace("/", os.sep))
        check("the copy exists on disk", os.path.isfile(on_disk), on_disk)
        if os.path.isfile(on_disk):
            copy_bytes = open(on_disk, "rb").read()
            check("the copy is byte-identical", copy_bytes == jpeg,
                  "%d bytes, the file has %d" % (len(copy_bytes), len(jpeg)))
            check("the copy still ends with FF D9", copy_bytes.endswith(b"\xff\xd9"),
                  copy_bytes[-4:].hex())
        status, _ = call(port, "GET", "/api/workspaces/%s/raw?path=%s" % (wid, img_path))
        check("the copy is served by /raw", status == 200, status)

        # ---- 3. a provider that refuses the picture does not kill the turn -
        _, sess2 = call(port, "POST", "/api/workspaces/%s/sessions" % wid, {"title": "refused"})
        sid2 = sess2["id"]
        before = len(MockProvider.records)
        events = run_turn(port, wid, sid2, "reject")
        check("the turn finished", any(e.get("type") == "done" for e in events), events[-2:])
        check("the turn reported no error",
              not any(e.get("type") == "error" for e in events),
              [e for e in events if e.get("type") == "error"])
        seen = MockProvider.records[before:]
        check("the picture was sent and refused", len([r for r in seen if image_parts(r)]) == 1,
              len(seen))
        retried = [r for r in seen if not image_parts(r)]
        check("the request was retried without the picture", len(retried) >= 1, len(seen))
        check("the retry kept the conversation",
              retried and len(retried[-1].get("messages") or []) > 1,
              retried and len(retried[-1].get("messages") or []))
        answers = [e for e in events if e.get("type") == "done"]
        check("the turn has an answer", bool(answers), events[-3:])

        # ---- 4. the session is still usable afterwards ---------------------
        before = len(MockProvider.records)
        events = run_turn(port, wid, sid2, "reject")
        check("a later turn in the same session finished",
              any(e.get("type") == "done" for e in events), events[-2:])
        check("and reported no error",
              not any(e.get("type") == "error" for e in events),
              [e for e in events if e.get("type") == "error"])

        # ---- 5. the log says what happened --------------------------------
        log = ""
        if os.path.exists(log_path):
            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                log = f.read()
        check("the log mentions the dropped picture", "image" in log and "dropp" in log.lower(),
              log[-400:])
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
