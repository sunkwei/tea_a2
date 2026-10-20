#!/usr/bin/env python3
"""Smoke test for toolkit_load_image (images from disk into the conversation).

Images used to enter a conversation only by a human pasting them: toolkit_file
returns bytes as text, which a vision model cannot read. This tool takes a path,
verifies the payload really is an image (magic bytes, not the extension), stores
a copy under .tea_agent_run/artifacts/images/ and hands it to the agent loop,
which appends it as a follow-up *user* message - tool results are text-only in
the wire format, so that message is the only place an image can ride on.

What this pins:

  * a vision model gets the tool; a text-only model does not (and calling it
    anyway explains why instead of silently dropping the picture);
  * the follow-up request carries the image as a data: image_url part whose
    base64 payload is byte-identical to the file on disk;
  * the image is copied into artifacts/images/, and the source file can be
    deleted afterwards without breaking the next turn (the copy is what is
    re-sent);
  * the session stores the attachment as a workspace-relative path, so it can
    be served back via /raw;
  * the tool result itself stays text (the model must not get a megabyte of
    base64 in the tool output);
  * non-images, missing files and paths outside the workspace are refused with
    a message that says what to do instead;
  * the per-turn cap is enforced: extra images are reported as not attached
    instead of blowing up the context.
"""

import base64
import http.client
import json
import os
import random
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def make_png(width=4, height=3, color=(200, 30, 30)):
    """A tiny but valid PNG, so the magic-byte sniff and the browser both work."""
    raw = b""
    for _ in range(height):
        raw += b"\x00" + bytes(color) * width

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data +
                struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) +
            chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


class MockProvider(BaseHTTPRequestHandler):
    records = []
    # scripted: the first request of a turn asks for the tool, the follow-up
    # (which contains the image) answers with text.
    tool_args = {"path": "shot.png"}
    many_paths = []
    # One tool-call round per turn: the test sets this before each turn and the
    # mock clears it, so the follow-up request (tool result / injected image)
    # always gets a plain text answer instead of another tool call.
    emit_tool = True

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

    def _done(self):
        try:
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except OSError:
            pass

    def _content(self, text):
        self._chunk({"id": "c1", "choices": [{"index": 0, "delta": {"content": text},
                                              "finish_reason": None}]})
        self._chunk({"id": "c1", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
        self._done()

    def _tool_call(self, name, args, call_id):
        self._tool_calls([(name, args, call_id)])

    def _tool_calls(self, calls):
        """Emit several tool calls in one assistant message (they run in one iteration)."""
        self._chunk({
            "id": "c1",
            "choices": [{
                "index": 0,
                "delta": {
                    "role": "assistant",
                    "tool_calls": [{
                        "index": i, "id": call_id, "type": "function",
                        "function": {"name": name, "arguments": json.dumps(args)},
                    } for i, (name, args, call_id) in enumerate(calls)],
                },
                "finish_reason": None,
            }],
        })
        self._chunk({"id": "c1", "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]})
        self._done()

    def do_POST(self):
        if self.path != "/v1/chat/completions":
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        payload = self._read_json()
        MockProvider.records.append(payload)
        msgs = payload.get("messages") or []
        # The follow-up of a tool call ends with the tool result (or with the
        # injected image message right after it): answer in text.
        after_tool = bool(msgs) and (msgs[-1].get("role") == "tool" or
                                     (msgs[-1].get("role") == "user" and
                                      isinstance(msgs[-1].get("content"), list) and
                                      any(p.get("type") == "image_url"
                                          for p in msgs[-1]["content"]
                                          if isinstance(p, dict))))

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()

        if after_tool or not MockProvider.emit_tool:
            self._content("I can see the image.")
            return
        MockProvider.emit_tool = False

        last_user = ""
        for m in reversed(msgs):
            if m.get("role") == "user":
                c = m.get("content")
                last_user = c if isinstance(c, str) else json.dumps(c)
                break
        if "SCENARIO=load_many" in last_user:
            self._tool_calls([("toolkit_load_image", {"path": p}, "call_many_%d" % i)
                              for i, p in enumerate(MockProvider.many_paths)])
        elif "SCENARIO=load" in last_user:
            self._tool_call("toolkit_load_image", MockProvider.tool_args, "call_load")
        else:
            self._content("plain answer")


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
    MockProvider.emit_tool = True
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


def tool_results(events):
    return [e for e in events if e.get("type") == "tool_result"]


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


def tool_names(request):
    return sorted(t["function"]["name"] for t in (request.get("tools") or []))


def main():
    binary = (sys.argv[1] if len(sys.argv) > 1
              else os.path.join(REPO_ROOT, "build", "bin", "tea_agent"))
    if not os.path.exists(binary):
        print("binary not found:", binary)
        return 1

    MockProvider.records = []
    mock = ThreadingHTTPServer(("127.0.0.1", 0), MockProvider)
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    mock_port = mock.server_address[1]

    tmp = tempfile.mkdtemp(prefix="tea_toolimg_")
    port = free_port()
    ws_dir = os.path.join(tmp, "ws")
    os.makedirs(ws_dir)
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
      textonly:
        max_context_tokens: 32768
        max_output_tokens: 4096
        supports_tools: true
        supports_vision: false
roles:
  main:
    provider: mock
    model: vision
""" % mock_port)

    proc = subprocess.Popen([
        binary, "--config", config_path, "--tea-home", os.path.join(tmp, "home"),
        "--host", "127.0.0.1", "--port", str(port), "--no-console",
        "--log-file", os.path.join(tmp, "tea_agent.log"), "--workspace", ws_dir,
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    failures = []
    checks = []

    def check(name, ok, detail=""):
        checks.append(name)
        print(("PASS " if ok else "FAIL ") + name + (("  " + str(detail)) if not ok else ""))
        if not ok:
            failures.append(name)

    png = make_png()
    shot = os.path.join(ws_dir, "shot.png")
    with open(shot, "wb") as f:
        f.write(png)
    with open(os.path.join(ws_dir, "notes.txt"), "w", encoding="utf-8") as f:
        f.write("not an image\n")
    outside = os.path.join(tmp, "outside.png")
    with open(outside, "wb") as f:
        f.write(png)

    try:
        if not wait_health(port):
            print("server did not become healthy")
            return 1
        _, listing = call(port, "GET", "/api/workspaces")
        wid = listing["workspaces"][0]["id"]
        _, sess = call(port, "POST", "/api/workspaces/%s/sessions" % wid, {"title": "images"})
        sid = sess["id"]

        # ---- the tool is advertised to a vision model ---------------------
        before = len(MockProvider.records)
        events = run_turn(port, wid, sid, "load")
        check("the turn finished", any(e.get("type") == "done" for e in events), events[-2:])
        check("toolkit_load_image is advertised for a vision model",
              "toolkit_load_image" in tool_names(MockProvider.records[before]),
              tool_names(MockProvider.records[before]))

        # ---- the tool result is text, the image rides on a user message ---
        results = tool_results(events)
        check("the tool ran", len(results) == 1 and results[0].get("name") == "toolkit_load_image",
              results)
        check("the tool reported success", results and results[0].get("ok") is True, results)
        check("the tool result is text, not base64",
              results and "image/png" in results[0].get("output", "")
              and len(results[0].get("output", "")) < 2000, results and len(results[0].get("output", "")))
        check("a stored copy is named in the result",
              results and ".tea_agent_run/artifacts/images/" in results[0].get("output", ""),
              results and results[0].get("output"))

        events_types = [e.get("type") for e in events]
        check("a tool_images event announced the attachment",
              "tool_images" in events_types, events_types)
        attach_ev = next((e for e in events if e.get("type") == "tool_images"), {})
        check("the event names the image", len(attach_ev.get("images") or []) == 1,
              attach_ev)
        check("the event describes the message",
              "toolkit_load_image" in (attach_ev.get("content") or ""), attach_ev.get("content"))

        follow_up = MockProvider.records[-1]
        parts = image_parts(follow_up)
        check("the follow-up request carries exactly one image", len(parts) == 1, parts)
        check("the image is a PNG", parts and parts[0][0] == "image/png", parts and parts[0][0])
        check("the bytes on the wire are the file's bytes",
              parts and parts[0][1] == png, parts and len(parts[0][1]))
        user_msgs = [m for m in follow_up.get("messages") or [] if m.get("role") == "user"]
        check("the image rides on a user message",
              len(user_msgs) >= 1 and isinstance(user_msgs[-1].get("content"), list), user_msgs[-1])

        # ---- persisted as a workspace-relative path ------------------------
        _, stored = call(port, "GET", "/api/sessions/%s" % sid)
        msgs = stored.get("messages") or []
        with_images = [m for m in msgs if m.get("images")]
        check("the session stored the attachment", len(with_images) == 1, msgs)
        img_path = (with_images[0]["images"][0] or {}).get("path", "") if with_images else ""
        check("it is stored workspace-relative",
              img_path.startswith(".tea_agent_run/artifacts/images/"), img_path)
        on_disk = os.path.join(ws_dir, img_path.replace("/", os.sep))
        check("the copy exists on disk", os.path.isfile(on_disk), on_disk)
        check("the copy is byte-identical", os.path.isfile(on_disk)
              and open(on_disk, "rb").read() == png)
        status, raw = call(port, "GET", "/api/workspaces/%s/raw?path=%s" % (wid, img_path))
        check("the copy is served by /raw", status == 200, status)

        # ---- the copy is what keeps working after the source disappears ---
        os.remove(shot)
        before = len(MockProvider.records)
        events = run_turn(port, wid, sid, "already_has_image")
        parts = image_parts(MockProvider.records[before])
        check("a later turn still sends the image", len(parts) == 1, parts)
        check("it is still the same bytes", parts and parts[0][1] == png)

        # ---- refusals -------------------------------------------------------
        cases = [
            ("missing.png", "file not found", "a missing file is refused"),
            ("notes.txt", "not a PNG", "a non-image is refused"),
            ("../outside.png", None, "a path outside the workspace is refused"),
        ]
        for arg, marker, label in cases:
            MockProvider.tool_args = {"path": arg}
            events = run_turn(port, wid, sid, "load")
            res = tool_results(events)
            ok = bool(res) and res[0].get("ok") is False
            if marker:
                ok = ok and marker in res[0].get("output", "")
            check(label, ok, res and res[0].get("output", "")[:160])
            check("no image was attached for " + arg,
                  not any(e.get("type") == "tool_images" for e in events))
        MockProvider.tool_args = {"path": "shot.png"}

        # ---- a text-only model does not get the tool ------------------------
        with open(shot, "wb") as f:
            f.write(png)
        before = len(MockProvider.records)
        events = run_turn_textonly(port, wid, sid)
        check("toolkit_load_image is hidden from a text-only model",
              "toolkit_load_image" not in tool_names(MockProvider.records[before]),
              tool_names(MockProvider.records[before]))

        # ---- the per-turn cap ----------------------------------------------
        many = []
        for i in range(8):
            p = os.path.join(ws_dir, "img%d.png" % i)
            with open(p, "wb") as f:
                f.write(make_png(color=(10 + i, 20, 30)))
            many.append("img%d.png" % i)
        MockProvider.many_paths = many
        events = run_turn_many(port, wid, sid, many)
        attach = next((e for e in events if e.get("type") == "tool_images"), {})
        attached = len(attach.get("images") or [])
        check("at most 6 images are attached in one turn", attached == 6, attached)
        capped = [r for r in tool_results(events)
                  if r.get("name") == "toolkit_load_image"
                  and "per-turn image limit" in r.get("output", "")]
        check("the capped calls say so", len(capped) == 2, len(capped))
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


def run_turn_textonly(port, wid, sid):
    MockProvider.emit_tool = True
    body = {"workspace_id": wid, "session_id": sid, "model": "textonly",
            "message": "SCENARIO=load"}
    return _post_turn(port, body)


def run_turn_many(port, wid, sid, paths):
    MockProvider.emit_tool = True
    body = {"workspace_id": wid, "session_id": sid, "message": "SCENARIO=load_many"}
    return _post_turn(port, body)


def _post_turn(port, body):
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


if __name__ == "__main__":
    sys.exit(main())
