#!/usr/bin/env python3
"""Smoke test for pasted-image (vision) input.

Covers the whole path: POST /api/chat with data URLs -> validation -> artifacts
on disk -> multimodal wire format -> session persistence -> /raw playback.

A local mock provider records the exact JSON body it receives, so the test can
assert the OpenAI vision shape without spending money or needing a real key.

Usage: python3 tests/smoke_vision.py [path-to-tea_agent-binary]
"""

import base64
import http.client
import json
import os
import random
import shutil
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


def make_png(width=4, height=4, rgb=(200, 60, 60)):
    """A tiny valid PNG, built by hand so the test needs no image library."""
    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))

    def chunk(tag, data):
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


PNG = make_png()
PNG_DATA_URL = "data:image/png;base64," + base64.b64encode(PNG).decode()


class MockProvider(BaseHTTPRequestHandler):
    records = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}")
        if self.path.endswith("/chat/completions"):
            MockProvider.records.append(payload)
            body = (
                "".join(
                    "data: %s\n\n" % json.dumps(c)
                    for c in [
                        {
                            "id": "c",
                            "choices": [
                                {"index": 0, "delta": {"content": "ok"}, "finish_reason": None}
                            ],
                        },
                        {
                            "id": "c",
                            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                        },
                    ]
                )
                + "data: [DONE]\n\n"
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body.encode())
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()


def free_port():
    while True:
        port = random.randint(20000, 60000)
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue


def main():
    binary = (
        sys.argv[1] if len(sys.argv) > 1 else os.path.join(REPO_ROOT, "build", "bin", "tea_agent")
    )
    if not os.path.exists(binary):
        print("binary not found:", binary)
        return 1

    mock = ThreadingHTTPServer(("127.0.0.1", 0), MockProvider)
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    mock_port = mock.server_address[1]

    tmp = tempfile.mkdtemp(prefix="tea_vision_")
    ws = os.path.join(tmp, "ws")
    os.makedirs(ws)
    config = os.path.join(tmp, "provider.yaml")
    with open(config, "w", encoding="utf-8") as f:
        f.write(
            """version: 1
providers:
  mock_vision:
    api_url: http://127.0.0.1:%d
    api_key: sk-test
    default_model: see
    models:
      see:
        supports_tools: true
        supports_vision: true
  mock_blind:
    api_url: http://127.0.0.1:%d
    api_key: sk-test
    default_model: blind
    models:
      blind:
        supports_tools: true
        supports_vision: false
"""
            % (mock_port, mock_port)
        )

    port = free_port()
    proc = subprocess.Popen(
        [
            binary,
            "--config",
            config,
            "--tea-home",
            os.path.join(tmp, "home"),
            "--port",
            str(port),
            "--no-console",
            "--log-file",
            os.path.join(tmp, "log.txt"),
            "--workspace",
            ws,
        ]
    )
    failures = []

    def req(method, path, body=None, timeout=30):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        c.request(
            method,
            path,
            body=json.dumps(body) if body is not None else None,
            headers={"Content-Type": "application/json"} if body is not None else {},
        )
        r = c.getresponse()
        return r.status, r.read()

    def chat(message, images=None, provider="mock_vision", model="see", session=None):
        body = {"message": message, "workspace_id": workspace_id, "provider": provider}
        if model:
            body["model"] = model
        if images is not None:
            body["images"] = images
        if session:
            body["session_id"] = session
        status, raw = req("POST", "/api/chat", body, timeout=60)
        events = []
        if status == 200:
            for line in raw.decode("utf-8", "replace").splitlines():
                line = line.strip()
                if not line.startswith("data: "):
                    continue
                data = line[6:]
                if data == "[DONE]":
                    break
                try:
                    events.append(json.loads(data))
                except ValueError:
                    pass
        return status, raw, events

    try:
        for _ in range(100):
            try:
                if req("GET", "/api/health")[0] == 200:
                    break
            except OSError:
                pass
            time.sleep(0.15)
        workspace_id = json.loads(req("GET", "/api/workspaces")[1])["workspaces"][0]["id"]

        # ---- 1) a model without vision refuses, and says why ----------------
        status, raw, _ = chat("look", [PNG_DATA_URL], provider="mock_blind", model="blind")
        err = json.loads(raw)["error"]
        if status != 400 or err["code"] != "vision_unsupported":
            failures.append("1: expected 400 vision_unsupported, got %s %r" % (status, raw[:160]))
        elif "supports_vision" not in err["message"]:
            failures.append("1: error message should mention supports_vision: %r" % err["message"])
        if len(MockProvider.records) != 0:
            failures.append("1: a blind model must not reach the provider")

        # ---- 2) malformed payloads are rejected before any work ------------
        for label, images, expect in [
            ("2a invalid base64", ["data:image/png;base64,!!!not-base64!!!"], "bad_image"),
            (
                "2b not an image",
                ["data:image/png;base64," + base64.b64encode(b"hello").decode()],
                "bad_image",
            ),
            ("2c too many", [PNG_DATA_URL] * 5, "too_many_images"),
            ("2d empty", [""], "bad_request"),
        ]:
            status, raw, _ = chat("x", images)
            code = json.loads(raw)["error"]["code"] if status != 200 else "200"
            if code != expect:
                failures.append("%s: expected %s, got %s (%r)" % (label, expect, code, raw[:120]))

        # 2e: no text and no images must not become an empty turn.
        status, raw, _ = chat("", [])
        code = json.loads(raw)["error"]["code"] if status != 200 else "200"
        if code != "bad_request":
            failures.append("2e empty message + empty list: expected bad_request, got %s" % code)

        # ---- 3) a valid image reaches the provider as multimodal content ---
        before = len(MockProvider.records)
        status, raw, events = chat("这张图是什么颜色？", [PNG_DATA_URL])
        if status != 200:
            failures.append("3: valid upload failed: %s %r" % (status, raw[:200]))
        elif len(MockProvider.records) != before + 1:
            failures.append("3: provider was not called exactly once")
        else:
            wire = MockProvider.records[-1]["messages"]
            user = [m for m in wire if m.get("role") == "user"][-1]
            content = user.get("content")
            if not isinstance(content, list):
                failures.append("3: user content is not a multimodal array: %r" % (content,))
            else:
                kinds = [p.get("type") for p in content]
                if kinds != ["text", "image_url"]:
                    failures.append("3: expected [text, image_url], got %r" % (kinds,))
                elif content[0].get("text") != "这张图是什么颜色？":
                    failures.append("3: text part lost: %r" % content[0])
                else:
                    url = content[1].get("image_url", {}).get("url", "")
                    if not url.startswith("data:image/png;base64,"):
                        failures.append("3: image url is not a png data URL: %r" % url[:60])
                    elif base64.b64decode(url.split(",", 1)[1]) != PNG:
                        failures.append("3: image bytes changed in transit")

        # ---- 4) the attachment is stored in the workspace -----------------
        arts = os.path.join(ws, ".tea_agent_run", "artifacts")
        stored = sorted(os.listdir(arts)) if os.path.isdir(arts) else []
        pngs = [n for n in stored if n.endswith(".png")]
        if len(pngs) != 1:
            failures.append("4: expected exactly one stored png, found %r" % stored)
        else:
            with open(os.path.join(arts, pngs[0]), "rb") as f:
                if f.read() != PNG:
                    failures.append("4: stored bytes differ from the upload")

        # ---- 5) the session records the path, not the bytes ---------------
        sessions = os.path.join(ws, ".tea_agent_run", "sessions")
        found = None
        for name in os.listdir(sessions):
            if name == "index.json" or not name.endswith(".json"):
                continue
            data = json.load(open(os.path.join(sessions, name), encoding="utf-8"))
            for m in data.get("messages", []):
                if m.get("role") == "user" and m.get("images"):
                    found = m["images"]
        if not found:
            failures.append("5: no session message carries images")
        else:
            img = found[0]
            if not img.get("path", "").startswith(".tea_agent_run/artifacts/"):
                failures.append("5: unexpected stored path %r" % img.get("path"))
            if img.get("mime") != "image/png":
                failures.append("5: unexpected mime %r" % img.get("mime"))
            if "base64" in json.dumps(found):
                failures.append("5: the session must not embed the image bytes")

            # ---- 6) /raw serves it back -----------------------------------
            status, raw = req(
                "GET",
                "/api/workspaces/%s/raw?path=%s"
                % (workspace_id, img["path"].replace("/", "%2F")),
            )
            if status != 200 or raw != PNG:
                failures.append(
                    "6: /raw did not return the image (%s, %d bytes)" % (status, len(raw))
                )

        # ---- 7) image-only message is allowed -----------------------------
        status, raw, _ = chat("", [PNG_DATA_URL])
        if status != 200:
            failures.append("7: image-only message rejected: %s %r" % (status, raw[:160]))
        else:
            user = [m for m in MockProvider.records[-1]["messages"] if m.get("role") == "user"][-1]
            if [p.get("type") for p in user.get("content", [])] != ["image_url"]:
                failures.append("7: image-only turn should have a single image part")

        # ---- 8) text-only turns are unchanged ------------------------------
        status, raw, _ = chat("just text")
        if status != 200:
            failures.append("8: plain text turn failed")
        else:
            user = [m for m in MockProvider.records[-1]["messages"] if m.get("role") == "user"][-1]
            if not isinstance(user.get("content"), str):
                failures.append("8: text-only turn must keep a string content")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        mock.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)

    if failures:
        print("FAIL")
        for f in failures:
            print(" -", f)
        return 1
    print("PASS: image attachments validate, store, travel as multimodal content and play back")
    return 0


if __name__ == "__main__":
    sys.exit(main())
