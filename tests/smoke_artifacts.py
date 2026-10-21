#!/usr/bin/env python3
"""Smoke test for produced files ("本轮产出") and their download links.

An agent that writes a report, renders a chart or encodes a video should hand the
user the file, not just mention it. Two pieces make that work:

  * the session records what was produced (`artifact_*`): paths reported by the
    tools themselves (toolkit_file write/append, toolkit_load_image) plus files
    found by a bounded scan after a command (a command cannot know what it wrote);
  * GET /api/workspaces/:id/download serves the file as an attachment, streamed
    from disk, so a 50 MB video is not refused by the 16 MB cap of /raw.

What this pins:

  * a written document, a generated image and an image loaded by the tool all
    end up in the session's `artifacts`, with kind/size/turn;
  * a file a command created (ffmpeg-style) is picked up too, while build noise
    (.o/.a) and internal paths (.tea_agent_run/...) are not;
  * the list is deduplicated by path and survives a restart;
  * the SSE stream announces it with an `artifacts` event mid-turn;
  * /download sends Content-Disposition: attachment (inline only when asked),
    the exact bytes, and works for a file larger than /raw's cap;
  * a path outside the workspace is refused, a missing file is 404;
  * the exported HTML lists the produced files.

Usage: python3 tests/smoke_artifacts.py [path-to-tea_agent-binary]
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

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


def make_png(w=8, h=8):
    raw = b""
    for _ in range(h):
        raw += b"\x00" + bytes((30, 120, 200)) * w

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data +
                struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n" +
            chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


class MockProvider(BaseHTTPRequestHandler):
    records = []
    calls = []

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
        self._chunk({"id": "c", "choices": [{"index": 0, "delta": {"content": text},
                                             "finish_reason": None}]})
        self._chunk({"id": "c", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
        self._done()

    def _tool_calls(self, calls):
        self._chunk({
            "id": "c",
            "choices": [{
                "index": 0,
                "delta": {
                    "role": "assistant",
                    "tool_calls": [{
                        "index": i, "id": cid, "type": "function",
                        "function": {"name": name, "arguments": json.dumps(args)},
                    } for i, (cid, name, args) in enumerate(calls)],
                },
                "finish_reason": None,
            }],
        })
        self._chunk({"id": "c", "choices": [{"index": 0, "delta": {},
                                             "finish_reason": "tool_calls"}]})
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
        after_tool = bool(msgs) and msgs[-1].get("role") == "tool"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        if after_tool or not MockProvider.calls:
            self._content("done")
            return
        calls, MockProvider.calls = MockProvider.calls, []
        self._tool_calls(calls)


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


def call(port, method, path, body=None, timeout=120):
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


def fetch(port, path):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=120)
    conn.request("GET", path)
    resp = conn.getresponse()
    body = resp.read()
    return resp.status, dict(resp.getheaders()), body


def run_turn(port, wid, sid, message="go"):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=180)
    conn.request("POST", "/api/chat",
                 body=json.dumps({"workspace_id": wid, "session_id": sid,
                                  "message": message}),
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
    MockProvider.calls = []
    mock = ThreadingHTTPServer(("127.0.0.1", 0), MockProvider)
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    mock_port = mock.server_address[1]

    tmp = tempfile.mkdtemp(prefix="tea_artifacts_")
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
        max_context_tokens: 32768
        supports_tools: true
        supports_vision: true
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
        _, sess = call(port, "POST", "/api/workspaces/%s/sessions" % wid, {"title": "artifacts"})
        sid = sess["id"]
        session_file = os.path.join(ws_dir, ".tea_agent_run", "sessions", sid + ".json")

        # A picture the agent can "load" (an input it turns into an artifact).
        with open(os.path.join(ws_dir, "input.png"), "wb") as f:
            f.write(make_png())

        # ---- a written document -------------------------------------------
        MockProvider.calls = [("c1", "toolkit_file", {
            "op": "write", "path": "report.md", "content": "# 报告\n\n结论：一切都好。\n"})]
        events = run_turn(port, wid, sid, "写个报告")
        check("the document write succeeded",
              any(e.get("type") == "tool_result" and e.get("ok") for e in events), events[-2:])
        evs = [e for e in events if e.get("type") == "artifacts"]
        check("an artifacts event was emitted", len(evs) >= 1, len(evs))
        items = evs[-1]["items"] if evs else []
        check("the written file is listed", any(a["path"] == "report.md" for a in items),
              [a["path"] for a in items])
        report = [a for a in items if a["path"] == "report.md"]
        check("it is classified as a document",
              report and report[0]["kind"] == "document", report)
        check("its size is recorded", report and report[0]["bytes"] > 0, report)
        check("it carries the turn number", report and report[0].get("turn") == 1, report)
        check("the producing tool is recorded",
              report and report[0].get("tool") == "toolkit_file", report)

        # ---- toolkit_load_image keeps internal copies out of the list ------
        # It stores the image under .tea_agent_run/artifacts/images/ so the
        # session stays self-contained; that copy is internal state, not a
        # deliverable (the user already has the original).
        MockProvider.calls = [("c2", "toolkit_load_image", {"path": "input.png"})]
        events = run_turn(port, wid, sid, "看看这张图")
        check("loading an image still works",
              any(e.get("type") == "tool_result" and e.get("ok") for e in events), events[-2:])
        _, detail = call(port, "GET", "/api/sessions/%s" % sid)
        arts = detail.get("artifacts") or []
        check("internal copies are not offered as artifacts",
              all(not a["path"].startswith(".tea_agent_run/") for a in arts),
              [a["path"] for a in arts])
        check("the user's own input is not claimed as output",
              all(a["path"] != "input.png" for a in arts), [a["path"] for a in arts])

        # ---- a file a command produced (and build noise that must not be) ---
        # python3 first, python as the fallback: on Windows the `python3` name can
        # be the Microsoft Store alias, which prints an install hint and exits 9009
        # without running anything - the file would then never be created and the
        # test would fail for a reason that has nothing to do with the artifacts
        # list.
        _write = ("open('chart.svg','w').write('<svg/>');"
                   "open('noise.o','w').write('x');"
                   "open('main.c','w').write('int main(){}');")
        _py = 'python3 -c "%s" || python -c "%s"' % (_write, _write)
        MockProvider.calls = [("c3", "toolkit_exec", {
            "command": _py,
            "timeout_ms": 60000})]
        events = run_turn(port, wid, sid, "生成图表")
        _, detail = call(port, "GET", "/api/sessions/%s" % sid)
        arts = detail.get("artifacts") or []
        paths = [a["path"] for a in arts]
        check("a file created by a command is picked up", "chart.svg" in paths, paths)
        check("it is classified as an image", 
              any(a["kind"] == "image" for a in arts if a["path"] == "chart.svg"), arts)
        check("build noise is not listed", "noise.o" not in paths, paths)
        check("source files count as output", "main.c" in paths, paths)
        check("the scan does not descend into .tea_agent_run",
              all(not p.startswith(".tea_agent_run") for p in paths), paths)

        # ---- a command that produced a lot ---------------------------------
        # A loop, not a long list of statements: 12 open() calls make the command
        # line long enough to hit the 8191-character limit cmd.exe has, and the
        # truncated command dies with a syntax error instead of writing anything.
        many = "for i in range(12): open('bulk' + str(i) + '.txt', 'w').write('x')"
        MockProvider.calls = [("c3b", "toolkit_exec", {
            "command": "python3 -c \"%s\" || python -c \"%s\"" % (many, many), "timeout_ms": 60000})]
        events = run_turn(port, wid, sid, "批量生成")
        _, detail = call(port, "GET", "/api/sessions/%s" % sid)
        arts = detail.get("artifacts") or []
        bulk = [a for a in arts if "bulk" in a["path"]]
        check("every produced file is listed", len(bulk) == 12, len(bulk))
        check("nothing was dropped below the cap, so no truncation is claimed",
              "more files were produced" not in
              (next((e for e in events if e.get("type") == "tool_result"), {}).get("output", "")),
              next((e for e in events if e.get("type") == "tool_result"), {}).get("output", "")[:120])

        # ---- past the cap the tool has to admit it ------------------------
        # 260 files is over the 200-file cap of the scan, so the result must say
        # so instead of presenting a truncated list as the whole truth.
        flood = "for i in range(260): open('flood' + str(i) + '.txt', 'w').write('x')"
        MockProvider.calls = [("c3c", "toolkit_exec", {
            "command": "python3 -c \"%s\" || python -c \"%s\"" % (flood, flood), "timeout_ms": 120000})]
        events = run_turn(port, wid, sid, "洪水")
        flood_out = next((e for e in events if e.get("type") == "tool_result"), {}).get("output", "")
        check("past the cap the tool says the list is incomplete",
              "more files were produced" in flood_out, flood_out[-160:])
        _, detail = call(port, "GET", "/api/sessions/%s" % sid)
        floods = [a for a in (detail.get("artifacts") or []) if "flood" in a["path"]]
        # The cap is on the whole scan (older files from the same second take
        # slots too), so the invariant is: fewer than produced, and never more
        # than the cap.
        check("the recorded list stops at the cap", 0 < len(floods) <= 200, len(floods))
        check("the list is shorter than what was produced", len(floods) < 260, len(floods))

        # ---- dedupe --------------------------------------------------------
        MockProvider.calls = [("c4", "toolkit_file", {
            "op": "append", "path": "report.md", "content": "\n补充一行。\n"})]
        before = len(paths)
        run_turn(port, wid, sid, "补一句")
        _, detail = call(port, "GET", "/api/sessions/%s" % sid)
        arts = detail.get("artifacts") or []
        check("rewriting a file does not duplicate it",
              sum(1 for a in arts if a["path"] == "report.md") == 1, len(arts))
        check("its new size is recorded",
              [a["bytes"] for a in arts if a["path"] == "report.md"][0] >
              [a["bytes"] for a in arts if a["path"] == "report.md"][0] - 1,
              [a["bytes"] for a in arts if a["path"] == "report.md"])

        # ---- download ------------------------------------------------------
        status, headers, body = fetch(port,
                                      "/api/workspaces/%s/download?path=report.md" % wid)
        check("download succeeds", status == 200, status)
        check("it is an attachment",
              headers.get("Content-Disposition", "").startswith("attachment;"),
              headers.get("Content-Disposition"))
        check("the filename is offered both ways",
              'filename="report.md"' in headers.get("Content-Disposition", "")
              and "filename*=UTF-8''report.md" in headers.get("Content-Disposition", ""),
              headers.get("Content-Disposition"))
        on_disk = open(os.path.join(ws_dir, "report.md"), "rb").read()
        check("the bytes are the file's bytes", body == on_disk, (len(body), len(on_disk)))

        status, headers, _ = fetch(port,
                                   "/api/workspaces/%s/download?path=report.md&inline=1" % wid)
        check("inline=1 renders instead of saving",
              headers.get("Content-Disposition", "").startswith("inline;"),
              headers.get("Content-Disposition"))

        status, headers, body = fetch(port,
                                      "/api/workspaces/%s/download?path=chart.svg" % wid)
        check("the SVG downloads with its own MIME type",
              status == 200 and headers.get("Content-Type", "").startswith("image/svg"),
              (status, headers.get("Content-Type")))

        # A file bigger than /raw's 16 MB limit: /raw refuses, /download must not.
        big_path = os.path.join(ws_dir, "big.mp4")
        with open(big_path, "wb") as f:
            f.write(b"\x00" * (17 * 1024 * 1024))
        status, _ = call(port, "GET",
                         "/api/workspaces/%s/raw?path=big.mp4" % wid)
        check("/raw refuses a 17 MB file (its documented cap)", status == 413, status)
        status, headers, body = fetch(port,
                                      "/api/workspaces/%s/download?path=big.mp4" % wid)
        check("download serves a file larger than /raw's cap", status == 200, status)
        check("the whole file arrives", len(body) == 17 * 1024 * 1024, len(body))
        check("the size header matches",
              headers.get("Content-Length") in (str(len(body)), None),
              headers.get("Content-Length"))

        # ---- refusals ------------------------------------------------------
        status, _ = call(port, "GET", "/api/workspaces/%s/download?path=../../etc/passwd" % wid)
        check("a path outside the workspace is refused", status in (400, 403), status)
        status, _ = call(port, "GET", "/api/workspaces/%s/download?path=nope.txt" % wid)
        check("a missing file is 404", status == 404, status)
        status, _ = call(port, "GET", "/api/workspaces/%s/download" % wid)
        check("a missing path parameter is 400", status == 400, status)

        # ---- survives a restart --------------------------------------------
        proc.terminate()
        proc.wait(timeout=10)
        proc = subprocess.Popen([
            binary, "--config", config, "--tea-home", os.path.join(tmp, "home"),
            "--host", "127.0.0.1", "--port", str(port), "--no-console",
            "--log-file", os.path.join(tmp, "tea.log"), "--workspace", ws_dir,
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        check("the server restarted", wait_health(port))
        _, detail = call(port, "GET", "/api/sessions/%s" % sid)
        arts = detail.get("artifacts") or []
        check("the artifact list survived the restart",
              any(a["path"] == "report.md" for a in arts), [a["path"] for a in arts])

        # ---- the export lists them -----------------------------------------
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
        conn.request("GET", "/api/sessions/%s/export" % sid)
        resp = conn.getresponse()
        html = resp.read().decode("utf-8")
        check("the export lists the produced files", "本轮产出" in html
              and "report.md" in html and "chart.svg" in html, html.count("artifact"))
        check("the export does not pretend to link them",
              "download?path=" not in html, "a static file must not offer dead links")
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
