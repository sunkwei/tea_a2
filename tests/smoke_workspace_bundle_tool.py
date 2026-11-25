#!/usr/bin/env python3
"""Smoke test for the standalone workspace bundle tool (tools/workspace_bundle.py).

The tool packs a workspace into one zip and restores it on another machine -
without touching the agent binary. It speaks the **same format** as the
server side (`GET /api/workspaces/{id}/bundle` / `POST .../bundle/import`), so
a bundle made by one is readable by the other.

What this pins:

  * export packs the manifest, the session files, the trajectories, the
    pictures, the memory and agent.md;
  * import restores them into another workspace, byte for byte, and updates the
    sidebar index;
  * **the local data wins**: a topic that is already there is not overwritten,
    and importing the same bundle twice changes nothing;
  * the tool and the server understand each other: a bundle the tool made is
    imported by the agent, and a bundle the agent made is imported by the tool.

Usage: python3 tests/smoke_workspace_bundle_tool.py [path-to-tea_agent-binary]
"""

import http.client
import io
import json
import os
import random
import socket
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
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
TOOL = os.path.join(REPO_ROOT, "tools", "workspace_bundle.py")


class MockProvider(BaseHTTPRequestHandler):
    """A mock model that answers with one line."""

    def log_message(self, *args):
        pass

    def do_POST(self):  # noqa: N802 (http.server API)
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for frame in ({"type": "content_delta", "text": "hello from the mock"},
                      {"type": "done", "content": "hello from the mock", "iterations": 1}):
            self.wfile.write(("data: " + json.dumps(frame) + "\n\n").encode())
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


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


def request(port, method, path, body=None, headers=None, timeout=60):
    head = dict(headers or {})
    payload = None
    if body is not None:
        payload = json.dumps(body).encode()
        head["Content-Type"] = "application/json"
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    conn.request(method, path, body=payload, headers=head)
    resp = conn.getresponse()
    raw = resp.read()
    try:
        parsed = json.loads(raw or b"null")
    except ValueError:
        parsed = None
    return resp.status, dict(resp.getheaders()), parsed


def raw_request(port, method, path, body=None, content_type=None, timeout=120):
    head = {}
    payload = None
    if body is not None:
        payload = body
        if content_type:
            head["Content-Type"] = content_type
        head["Content-Length"] = str(len(body))
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    conn.request(method, path, body=payload, headers=head)
    resp = conn.getresponse()
    data = resp.read()
    return resp.status, dict(resp.getheaders()), data


def run_tool(*args):
    """Run the tool; returns (returncode, parsed-json-or-None, stderr)."""
    proc = subprocess.run([sys.executable, TOOL] + list(args) + ["--json"],
                          capture_output=True, text=True)
    payload = None
    try:
        payload = json.loads(proc.stdout or "null")
    except ValueError:
        payload = None
    return proc.returncode, payload, proc.stderr


def make_workspace(root, session_id, title):
    """A workspace that looks like the ones the agent makes."""
    run = os.path.join(root, ".tea_agent_run")
    for part in ("sessions", "trajectories", "memory", os.path.join("artifacts", "images")):
        target = os.path.join(run, part)
        if not os.path.isdir(target):
            os.makedirs(target)
    session = {
        "id": session_id,
        "title": title,
        "created_at": "2026-11-25T00:00:00Z",
        "updated_at": "2026-11-25T00:01:00Z",
        "messages": [
            {"role": "user", "content": "hello",
             "images": [{"path": ".tea_agent_run/artifacts/images/img_tool.png",
                         "mime": "image/png"}]},
            {"role": "assistant", "content": "hi"},
        ],
        "todos": [{"id": "t1", "text": "do it", "status": "done"}],
    }
    with io.open(os.path.join(run, "sessions", session_id + ".json"), "w", encoding="utf-8") as f:
        f.write(json.dumps(session, indent=2, ensure_ascii=False, sort_keys=True) + "\n")
    with io.open(os.path.join(run, "sessions", "index.json"), "w", encoding="utf-8") as f:
        json.dump([{"id": session_id, "title": title}], f, ensure_ascii=False)
    with io.open(os.path.join(run, "trajectories", session_id + ".jsonl"), "w", encoding="utf-8") as f:
        f.write(json.dumps({"turn_id": "t_1", "steps": []}) + "\n")
    with io.open(os.path.join(run, "memory", "memory.json"), "w", encoding="utf-8") as f:
        json.dump({"notes": [{"id": "m_tool001", "text": "a note from the tool",
                               "source": "user", "weight_base": 1.0,
                               "created_at": "2026-11-25T00:00:00Z"}]},
                  f, ensure_ascii=False, sort_keys=True)
    with io.open(os.path.join(run, "agent.md"), "w", encoding="utf-8") as f:
        f.write("# experience\n\n- the tool carries this\n")
    with open(os.path.join(run, "artifacts", "images", "img_tool.png"), "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\ntool-picture")
    return run


def main():
    binary = (sys.argv[1] if len(sys.argv) > 1
              else os.path.join(REPO_ROOT, "build", "bin", "tea_agent"))

    failures = []
    checks = []

    def check(name, ok, detail=""):
        checks.append(name)
        print(("PASS " if ok else "FAIL ") + name + (("  " + str(detail)) if not ok else ""))
        if not ok:
            failures.append(name)

    tmp = tempfile.mkdtemp(prefix="tea_bundle_tool_")
    src = os.path.join(tmp, "src-ws")
    dst = os.path.join(tmp, "dst-ws")
    os.makedirs(src)
    os.makedirs(dst)
    run_src = make_workspace(src, "s_tool000001", "the tool test")
    bundle = os.path.join(tmp, "bundle.zip")

    # ---- export ----------------------------------------------------------
    code, report, err = run_tool("export", src, "-o", bundle)
    check("the tool exports", code == 0, (code, err))
    check("the export says what went in",
          report and report.get("sessions") == 1 and report.get("trajectories") == 1
          and report.get("images") == 1 and report.get("notes") == 1, report)
    check("the bundle is a zip", os.path.exists(bundle) and zipfile.is_zipfile(bundle), bundle)
    names = []
    manifest = {}
    if os.path.exists(bundle):
        with zipfile.ZipFile(bundle) as zf:
            names = zf.namelist()
            manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
    check("the bundle has a manifest", "manifest.json" in names, names)
    check("the manifest says what it is",
          manifest.get("kind") == "tea_a2.workspace.bundle", manifest.get("kind"))
    check("the topic is in the bundle", "sessions/s_tool000001.json" in names, names)
    check("the trajectory is in the bundle", "trajectories/s_tool000001.jsonl" in names, names)
    check("the picture is in the bundle",
          "images/s_tool000001/img_tool.png" in names, names)
    check("the memory is in the bundle", "memory.json" in names, names)
    check("agent.md is in the bundle", "agent.md" in names, names)

    # ---- inspect ---------------------------------------------------------
    code, report, err = run_tool("inspect", bundle)
    check("the tool inspects a bundle", code == 0, (code, err))
    check("the inspection lists the topic",
          report and [s.get("id") for s in report.get("manifest", {}).get("sessions", [])]
          == ["s_tool000001"], report)

    # ---- import ----------------------------------------------------------
    code, report, err = run_tool("import", dst, bundle)
    check("the tool imports", code == 0, (code, err))
    check("the import says what came in",
          report and report.get("sessions") == 1 and report.get("trajectories") == 1
          and report.get("images") == 1 and report.get("notes") == 1, report)
    check("the import reports no conflict",
          report and report.get("sessions_conflict") == 0, report)
    run_dst = os.path.join(dst, ".tea_agent_run")
    restored = os.path.join(run_dst, "sessions", "s_tool000001.json")
    check("the topic is there", os.path.exists(restored), restored)
    if os.path.exists(restored):
        with open(os.path.join(run_src, "sessions", "s_tool000001.json"), "rb") as f:
            original = f.read()
        with open(restored, "rb") as f:
            check("the topic is byte for byte the original", f.read() == original)
    check("the trajectory is there",
          os.path.exists(os.path.join(run_dst, "trajectories", "s_tool000001.jsonl")))
    check("the picture is there",
          os.path.exists(os.path.join(run_dst, "artifacts", "images", "img_tool.png")))
    check("the memory came along",
          os.path.exists(os.path.join(run_dst, "memory", "memory.json")))
    check("agent.md came along", os.path.exists(os.path.join(run_dst, "agent.md")))
    index_path = os.path.join(run_dst, "sessions", "index.json")
    check("the sidebar index knows the topic",
          os.path.exists(index_path) and
          any(e.get("id") == "s_tool000001"
              for e in json.loads(io.open(index_path, encoding="utf-8").read())), index_path)

    # ---- the local data wins ---------------------------------------------
    with io.open(restored, "r", encoding="utf-8") as f:
        local = json.load(f)
    local["title"] = "the local title"
    with io.open(restored, "w", encoding="utf-8") as f:
        json.dump(local, f, ensure_ascii=False, indent=2, sort_keys=True)
    with open(restored, "rb") as f:
        local_bytes = f.read()
    code, report, err = run_tool("import", dst, bundle)
    check("the second import answers", code == 0, (code, err))
    check("it restores nothing", report and report.get("sessions") == 0, report)
    check("it reports the conflict",
          report and report.get("sessions_conflict") == 1, report)
    with open(restored, "rb") as f:
        check("the local file was not touched", f.read() == local_bytes)

    # ---- the tool and the agent understand each other ---------------------
    if not os.path.exists(binary):
        print("binary not found:", binary, "- the cross-check is skipped")
        print()
        print("%d/%d checks passed" % (len(checks) - len(failures), len(checks)))
        if failures:
            print("FAILED:", ", ".join(failures))
            return 1
        return 0

    mock = ThreadingHTTPServer(("127.0.0.1", 0), MockProvider)
    mock.daemon_threads = True
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    mock_port = mock.server_address[1]

    ws_dir = os.path.join(tmp, "agent-ws")
    os.makedirs(ws_dir)
    config_path = os.path.join(tmp, "provider.yaml")
    with open(config_path, "w", encoding="utf-8") as f:
        f.write("""version: 1
providers:
  mock:
    api_url: http://127.0.0.1:%d/v1
    api_key: sk-test
    default_model: m
    models:
      m:
        max_context_tokens: 32768
        max_output_tokens: 4096
""" % (mock_port,))

    agent_port = free_port()
    proc = subprocess.Popen([
        binary, "--config", config_path, "--tea-home", os.path.join(tmp, "home"),
        "--host", "127.0.0.1", "--port", str(agent_port), "--no-console",
        "--log-file", os.path.join(tmp, "tea_agent.log"), "--workspace", ws_dir,
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if not wait_health(agent_port):
            print("agent did not become healthy")
            return 1
        _, _, listing = request(agent_port, "GET", "/api/workspaces")
        ws_id = listing["workspaces"][0]["id"]

        # The bundle the tool made -> the agent imports it.
        status, _, body = raw_request(agent_port, "POST",
                                      "/api/workspaces/%s/bundle/import" % ws_id,
                                      body=open(bundle, "rb").read(),
                                      content_type="application/zip")
        report = {}
        try:
            report = json.loads(body.decode("utf-8"))
        except ValueError:
            pass
        check("the agent imports a bundle the tool made", status == 200, (status, body[:200]))
        check("the agent restored the topic", report.get("sessions") == 1, report)
        _, _, sessions = request(agent_port, "GET", "/api/workspaces/%s/sessions" % ws_id)
        ids = [s.get("id") for s in sessions.get("sessions") or []]
        check("the agent lists the topic", "s_tool000001" in ids, ids)
        status, _, session = request(agent_port, "GET", "/api/sessions/s_tool000001")
        check("the topic opens on the agent", status == 200, status)
        check("the messages came along",
              len((session or {}).get("messages") or []) == 2,
              len((session or {}).get("messages") or []))

        # The bundle the agent made -> the tool imports it.
        agent_bundle = os.path.join(tmp, "agent-bundle.zip")
        status, headers, blob = raw_request(agent_port, "GET",
                                            "/api/workspaces/%s/bundle" % ws_id)
        check("the agent exports a bundle", status == 200, status)
        with open(agent_bundle, "wb") as f:
            f.write(blob)
        # Into a *third* workspace: the one above already has that topic, and
        # the local data has to win there.
        dst2 = os.path.join(tmp, "dst2-ws")
        os.makedirs(dst2)
        code, report, err = run_tool("import", dst2, agent_bundle)
        check("the tool imports a bundle the agent made", code == 0, (code, err))
        check("it says what came in",
              report and report.get("sessions") >= 1, report)
        run_dst2 = os.path.join(dst2, ".tea_agent_run")
        check("the agent's topic is on disk now",
              os.path.exists(os.path.join(run_dst2, "sessions", "s_tool000001.json")),
              run_dst2)
        code, report, err = run_tool("import", dst, agent_bundle)
        check("importing it where the topic already is keeps the local one",
              report and report.get("sessions") == 0
              and report.get("sessions_conflict") >= 1, report)
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
