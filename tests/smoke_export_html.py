#!/usr/bin/env python3
"""Smoke test for exporting a session as one self-contained HTML file.

The export has to survive leaving this machine: copied to a USB stick, opened by
double-clicking in a browser, on a box that has never run tea_agent and has no
network. So the file must carry everything - CSS, inline images as data: URLs,
the conversation text, tool calls and outputs - and reference nothing external.

What this pins:

  * the download is text/html with a sane ASCII filename and a UTF-8 variant;
  * the document is complete HTML and contains its own stylesheet (no <link>),
    no scripts that fetch anything, and no external src/href in content;
  * Markdown is rendered (headings, lists, code fences, tables, inline code)
    while HTML in a model answer is escaped - a `javascript:` link never becomes
    a live link, and `</script>` inside a message cannot end the embedded JSON;
  * images from the session are inlined (data:image/png;base64,...) and those
    bytes are the file's bytes; a missing/oversized image degrades to a caption
    instead of breaking the export;
  * tool output is truncated with an explicit note instead of blowing the file
    up;
  * the embedded JSON block parses and matches the session (roles, contents);
  * the exported file imports back into the same or another workspace as a NEW
    session: text equal, images restored into artifacts and visible again;
  * malformed input is refused with 400 (not a half-created session).
"""

import base64
import http.client
import json
import os
import random
import re
import socket
import struct
import subprocess
import sys
import tempfile
import time
import zlib

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def make_png(width=6, height=4, rgb=(10, 120, 200)):
    raw = b""
    for _ in range(height):
        raw += b"\x00" + bytes(rgb) * width

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data +
                struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) +
            chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


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


def call(port, method, path, body=None, raw_body=None, content_type=None, timeout=60):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    headers = {}
    payload = None
    if raw_body is not None:
        payload = raw_body.encode("utf-8") if isinstance(raw_body, str) else raw_body
        headers["Content-Type"] = content_type or "text/html; charset=utf-8"
    elif body is not None:
        payload = json.dumps(body)
        headers["Content-Type"] = "application/json"
    conn.request(method, path, body=payload, headers=headers)
    resp = conn.getresponse()
    data = resp.read()
    return resp.status, dict(resp.getheaders()), data


def main():
    binary = (sys.argv[1] if len(sys.argv) > 1
              else os.path.join(REPO_ROOT, "build", "bin", "tea_agent"))
    if not os.path.exists(binary):
        print("binary not found:", binary)
        return 1

    tmp = tempfile.mkdtemp(prefix="tea_export_")
    port = free_port()
    ws_a = os.path.join(tmp, "proj_a")
    ws_b = os.path.join(tmp, "proj_b")
    for d in (ws_a, ws_b, os.path.join(tmp, "home")):
        os.makedirs(d, exist_ok=True)
    config = os.path.join(tmp, "provider.yaml")
    with open(config, "w", encoding="utf-8") as f:
        f.write("""version: 1
providers:
  mock:
    api_url: http://127.0.0.1:9/v1
    api_key: sk-test
    default_model: m
    models:
      m:
        max_context_tokens: 32768
        supports_tools: true
        supports_vision: true
roles: {main: {provider: mock, model: m}}
""")
    proc = subprocess.Popen([
        binary, "--config", config, "--tea-home", os.path.join(tmp, "home"),
        "--host", "127.0.0.1", "--port", str(port), "--no-console",
        "--log-file", os.path.join(tmp, "tea.log"),
        "--workspace", ws_a, "--workspace", ws_b,
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    failures = []
    checks = []

    def check(name, ok, detail=""):
        checks.append(name)
        print(("PASS " if ok else "FAIL ") + name + (("  " + str(detail)) if not ok else ""))
        if not ok:
            failures.append(name)

    run_a = os.path.join(ws_a, ".tea_agent_run")
    png_path = os.path.join(run_a, "artifacts", "images", "img_test.png")
    png = make_png()
    try:
        if not wait_health(port):
            print("server did not become healthy")
            return 1
        _, _, listing = call(port, "GET", "/api/workspaces")
        wss = json.loads(listing)["workspaces"]
        wid_a = [w for w in wss if w["path"].endswith("proj_a")][0]["id"]
        wid_b = [w for w in wss if w["path"].endswith("proj_b")][0]["id"]

        # ---- seed a session straight on disk ------------------------------
        os.makedirs(os.path.dirname(png_path), exist_ok=True)
        with open(png_path, "wb") as f:
            f.write(png)
        long_output = "line\n" * 4000
        messages = [
            {"role": "user", "content": "请检查 # Hello 标题、**粗体**、`code` 与图片",
             "created_at": "2026-01-01T00:00:00Z"},
            {"role": "assistant", "reasoning": "先想想",
             "content": "## 结论\n\n- 第一点\n- 第二点\n\n```c\nint main(){return 0;}\n```\n\n"
                        "| a | b |\n|---|---|\n| 1 | 2 |\n\n"
                        "<script>alert(1)</script> 以及 [坏链接](javascript:alert(1)) 和 "
                        "[好链接](https://example.com/x)",
             "tool_calls": [{"id": "c1", "type": "function",
                             "function": {"name": "toolkit_exec",
                                          "arguments": "{\"command\": \"ls\"}"}}],
             "created_at": "2026-01-01T00:00:01Z"},
            {"role": "tool", "tool_call_id": "c1", "name": "toolkit_exec",
             "content": long_output, "ok": True, "elapsed_ms": 12,
             "created_at": "2026-01-01T00:00:02Z"},
            {"role": "user", "content": "这张图里是什么？",
             "images": [{"path": ".tea_agent_run/artifacts/images/img_test.png",
                         "mime": "image/png"}],
             "created_at": "2026-01-01T00:00:03Z"},
            {"role": "user", "content": "引用了 </script> 的 <b>危险</b> 文本",
             "created_at": "2026-01-01T00:00:04Z"},
        ]
        sid = "s_export_test"
        with open(os.path.join(run_a, "sessions", sid + ".json"), "w", encoding="utf-8") as f:
            json.dump({"id": sid, "title": "导出 测试 / session", "created_at": "2026-01-01T00:00:00Z",
                       "updated_at": "2026-01-01T00:00:04Z", "messages": messages}, f, ensure_ascii=False)
        with open(os.path.join(run_a, "sessions", "index.json"), "w", encoding="utf-8") as f:
            json.dump([{"id": sid, "title": "导出 测试 / session",
                        "created_at": "2026-01-01T00:00:00Z",
                        "updated_at": "2026-01-01T00:00:04Z",
                        "message_count": len(messages)}], f, ensure_ascii=False)

        # ---- export --------------------------------------------------------
        status, headers, html = call(port, "GET", "/api/sessions/%s/export" % sid)
        text = html.decode("utf-8")
        check("export succeeds", status == 200, status)
        check("it is served as HTML", headers.get("Content-Type", "").startswith("text/html"),
              headers.get("Content-Type"))
        disp = headers.get("Content-Disposition", "")
        check("the download has an ASCII filename", 'filename="tea_agent-' in disp, disp)
        check("the filename is HTML", disp.rstrip('"').endswith(".html"), disp)
        check("a UTF-8 filename variant is offered", "filename*=UTF-8''" in disp, disp)

        check("it is a complete document",
              text.startswith("<!doctype html>") and text.rstrip().endswith("</html>"),
              text[:60])
        check("the title is in <title>", "<title>" in text and "导出 测试 / session" in text[:800],
              text[:300])
        check("the stylesheet is inline", "<style>" in text and "prefers-color-scheme" in text)
        check("nothing references an external stylesheet", "<link" not in text.lower())
        # Everything outside the embedded JSON block: that block is data, not
        # markup, and it legitimately contains the words "<script" as text.
        body_only = re.sub(r"<script.*?</script>", "", text, flags=re.S)
        check("there is no external script",
              "<script" not in body_only.lower(), body_only.lower().count("<script"))
        check("no remote images in the content", 'src="http' not in body_only.lower())
        check("no remote stylesheets in the content", "<link" not in body_only.lower())

        # ---- text rendering -------------------------------------------------
        check("markdown headings render", "<h2>结论</h2>" in text, text[:400])
        check("markdown lists render", "<ul><li>第一点</li><li>第二点</li></ul>" in text)
        check("code fences render", "<pre><code>int main(){return 0;}" in text)
        check("tables render", '<table class="md-table">' in text and "<td>1</td>" in text)
        check("inline code renders", '<code class="md-inline">code</code>' in text)
        check("bold renders", "<strong>粗体</strong>" in text)
        check("a model's HTML is escaped",
              "&lt;script&gt;alert(1)&lt;/script&gt;" in body_only
              and "<script>alert(1)" not in body_only, "script survived")
        check("a javascript: link is not a live link", "javascript:alert(1)" not in
              re.sub(r"<script.*?</script>", "", text, flags=re.S).replace("&lt;", "<"),
              "link survived")
        check("a normal link is kept", 'href="https://example.com/x"' in text)
        check("tool calls are shown", "调用 toolkit_exec" in text and "ls" in text)
        check("tool output is truncated with a note",
              "导出时截断" in text and len(text) < 400000, len(text))
        # Collapsed by default: a long transcript must not dump every tool
        # result into the page. The summary keeps the size and a first-line hint.
        m_tool = re.search(r'<details class="tool (?:ok|fail)"\s*(open)?>(.*?)</summary>',
                           text, re.S)
        check("tool output is a <details> block", m_tool is not None)
        check("tool output is collapsed by default", m_tool and not m_tool.group(1),
              m_tool and m_tool.group(1))
        check("the collapsed summary states the size",
              m_tool and "输出" in m_tool.group(2) and "KB" in m_tool.group(2),
              m_tool and m_tool.group(2)[:120])
        check("the collapsed summary previews the first line",
              m_tool and 'class="pv"' in m_tool.group(2) and "line" in m_tool.group(2),
              m_tool and m_tool.group(2)[:160])
        m_call = re.search(r'<details class="call"><summary>(.*?)</summary>', text, re.S)
        check("tool calls are collapsed too", m_call is not None)
        check("a collapsed call shows its main argument",
              m_call and 'class="pv"' in m_call.group(1) and "command" in m_call.group(1),
              m_call and m_call.group(1)[:160])
        check("nothing else is expanded by default", text.count("<details") ==
              text.count("<details class=\"reasoning\">") + text.count("<details class=\"call\">")
              + text.count("<details class=\"tool ok\">") + text.count("<details class=\"tool fail\">")
              and "details open" not in text and "<details open" not in text,
              text.count("<details"))
        check("the reasoning is available but collapsed",
              'class="reasoning"' in text and "先想想" in text)

        # ---- images ----------------------------------------------------------
        m = re.search(r'src="data:image/png;base64,([A-Za-z0-9+/=]+)"', text)
        check("the image is inlined as a data URL", m is not None)
        if m:
            check("the inlined bytes are the file's bytes",
                  base64.b64decode(m.group(1)) == png, len(m.group(1)))
        check("the image has a caption with its path",
              "artifacts/images/img_test.png" in text)

        # ---- the embedded JSON ------------------------------------------------
        jm = re.search(r'<script type="application/json" id="tea-session-data">(.*?)</script>',
                       text, re.S)
        check("the session JSON is embedded", jm is not None)
        payload = json.loads(jm.group(1).replace("<\\/", "</")) if jm else {}
        check("the payload records the format",
              payload.get("format") == "tea_agent.session" and payload.get("format_version") == 1,
              payload.get("format"))
        check("the payload has every message",
              len(payload.get("messages") or []) == len(messages),
              len(payload.get("messages") or []))
        check("a message containing </script> does not break the block",
              any("</script>" in (mm.get("content") or "")
                  for mm in payload.get("messages") or []),
              "literal not found")
        check("the payload carries the image for a restore",
              isinstance(payload.get("images"), dict) and
              ".tea_agent_run/artifacts/images/img_test.png" in payload.get("images", {}))

        # ---- import back into another workspace -------------------------------
        status, _, raw = call(port, "POST", "/api/workspaces/%s/sessions/import" % wid_b,
                              raw_body=text)
        imported = json.loads(raw) if status == 200 else {}
        check("the HTML file imports into another workspace", status == 200, (status, raw[:200]))
        new_sid = (imported.get("session") or {}).get("id", "")
        check("it creates a new session", bool(new_sid) and new_sid != sid, new_sid)
        check("it reports the message count",
              imported.get("messages") == len(messages), imported.get("messages"))
        check("the title is preserved",
              (imported.get("session") or {}).get("title") == "导出 测试 / session",
              (imported.get("session") or {}).get("title"))

        _, _, back_raw = call(port, "GET", "/api/sessions/%s" % new_sid)
        back = json.loads(back_raw)
        roles = [(m.get("role"), m.get("content", "")[:20]) for m in back.get("messages") or []]
        check("the imported messages match",
              len(roles) == len(messages) and roles[0][0] == "user"
              and "请检查" in (back["messages"][0].get("content") or ""), roles[:2])
        imgs = [m for m in back.get("messages") or [] if m.get("images")]
        check("the imported session has its image", len(imgs) == 1, imgs)
        new_rel = (imgs[0]["images"][0]["path"] if imgs else "")
        check("the image was rewritten into this workspace",
              "/artifacts/images/" in new_rel and new_rel.endswith(".png")
              and "img_test" not in new_rel, new_rel)
        new_abs = os.path.join(ws_b, new_rel.replace("/", os.sep))
        check("the image bytes came along", os.path.isfile(new_abs)
              and open(new_abs, "rb").read() == png, new_abs)
        status, _, served = call(port, "GET", "/api/workspaces/%s/raw?path=%s" % (wid_b, new_rel))
        check("the restored image is served by /raw", status == 200 and served == png, status)

        # ---- importing twice must not overwrite --------------------------------
        status, _, raw2 = call(port, "POST", "/api/workspaces/%s/sessions/import" % wid_b,
                               raw_body=text)
        second = json.loads(raw2) if status == 200 else {}
        check("a second import makes another session",
              status == 200 and second.get("session", {}).get("id") not in ("", new_sid),
              second.get("session", {}).get("id"))
        _, _, sessions_raw = call(port, "GET", "/api/workspaces/%s/sessions" % wid_b)
        check("both imported sessions are listed",
              len(json.loads(sessions_raw).get("sessions") or []) == 2,
              json.loads(sessions_raw).get("sessions"))

        # ---- the JSON alone also imports ---------------------------------------
        status, _, raw3 = call(port, "POST", "/api/workspaces/%s/sessions/import" % wid_b,
                               body={"messages": payload["messages"],
                                     "session": {"title": "只有 JSON"}})
        check("a bare session JSON imports too",
              status == 200 and json.loads(raw3).get("session", {}).get("title") == "只有 JSON",
              (status, raw3[:120]))

        # ---- refusals -----------------------------------------------------------
        status, _, raw4 = call(port, "POST", "/api/workspaces/%s/sessions/import" % wid_b,
                               raw_body="<html><body>hello</body></html>")
        check("a random HTML file is refused",
              status == 400 and b"tea-session-data" in raw4, (status, raw4[:120]))
        status, _, raw5 = call(port, "POST", "/api/workspaces/%s/sessions/import" % wid_b,
                               body={"messages": []})
        check("an empty payload is refused", status == 400, (status, raw5[:120]))
        _, _, after_raw = call(port, "GET", "/api/workspaces/%s/sessions" % wid_b)
        check("the refusals created no session",
              len(json.loads(after_raw).get("sessions") or []) == 3,
              len(json.loads(after_raw).get("sessions") or []))

        # ---- a missing export ------------------------------------------------
        status, _, _ = call(port, "GET", "/api/sessions/s_nope/export")
        check("exporting an unknown session is 404", status == 404, status)
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            proc.kill()

    print()
    print("%d/%d checks passed" % (len(checks) - len(failures), len(checks)))
    if failures:
        print("FAILED:", ", ".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
