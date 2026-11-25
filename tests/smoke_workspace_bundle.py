#!/usr/bin/env python3
"""Smoke test for the workspace bundle: export a workspace, import it elsewhere.

A bundle is one zip file with everything a workspace needs to be *the same
workspace* on another device: every topic (session) with its transcript, the
trajectory of every turn, the pictures that were pasted into the conversations,
the memory notes and the workspace's agent.md.

What this pins:

  * GET /api/workspaces/{id}/bundle answers a zip with a manifest, the session
    files and the trajectories;
  * POST /api/workspaces/{id}/bundle/import restores those topics into another
    workspace, with the same ids, the same messages and the trajectory;
  * **the local data wins**: a topic that is already there is not overwritten,
    the report says so, and importing the same bundle twice changes nothing;
  * the memory notes and agent.md travel with the bundle;
  * a file that is not a bundle is refused with a clear error.

Usage: python3 tests/smoke_workspace_bundle.py [path-to-tea_agent-binary]
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
    """One request. Returns (status, headers, parsed-json-or-None)."""
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
    """One request with a body that is not JSON. Returns (status, headers, bytes)."""
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


def run_turn(port, workspace_id, message):
    """One full SSE turn; returns the events."""
    body = {"workspace_id": workspace_id, "message": message}
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

    mock = ThreadingHTTPServer(("127.0.0.1", 0), MockProvider)
    mock.daemon_threads = True
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    mock_port = mock.server_address[1]

    tmp = tempfile.mkdtemp(prefix="tea_bundle_")
    ws_a = os.path.join(tmp, "ws-a")
    ws_b = os.path.join(tmp, "ws-b")
    os.makedirs(ws_a)
    os.makedirs(ws_b)

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
        "--log-file", os.path.join(tmp, "tea_agent.log"), "--workspace", ws_a,
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    failures = []
    checks = []

    def check(name, ok, detail=""):
        checks.append(name)
        print(("PASS " if ok else "FAIL ") + name + (("  " + str(detail)) if not ok else ""))
        if not ok:
            failures.append(name)

    try:
        if not wait_health(agent_port):
            print("agent did not become healthy")
            return 1

        _, _, listing = request(agent_port, "GET", "/api/workspaces")
        ws_a_id = listing["workspaces"][0]["id"]
        status, _, added = request(agent_port, "POST", "/api/workspaces",
                                   {"path": ws_b, "name": "b"})
        check("the second workspace is registered", status == 200, (status, added))
        ws_b_id = added["id"]

        # ---- something to carry over ------------------------------------
        events = run_turn(agent_port, ws_a_id, "hello, this is the topic")
        started = [e for e in events if e.get("type") == "start"]
        check("the turn produced a topic", bool(started), events[:2])
        session_id = started[0].get("session_id") if started else ""
        check("the topic has an id", bool(session_id), started[:1])

        status, _, notes = request(agent_port, "POST",
                                   "/api/workspaces/%s/memory" % ws_a_id,
                                   {"text": "the build command is cmake --build build-mingw",
                                    "source": "user"})
        check("a memory note is written", status == 200, (status, notes))

        status, _, doc = request(agent_port, "PUT",
                                 "/api/workspaces/%s/agent-doc" % ws_a_id,
                                 {"content": "# experience\n\n- the bundle carries this\n"})
        check("agent.md is written", status == 200, (status, doc))

        # ---- a topic with a picture --------------------------------------
        # The picture of a message is stored as a path; the bytes live in the
        # runtime directory. The path here points at another device (that is
        # what a bundle that travelled looks like), so the import has to
        # rewrite it to where the picture is on *this* device.
        img_name = "img_bundle_test.png"
        img_bytes = b"\x89PNG\r\n\x1a\n" + b"bundle-picture-bytes"
        run_a = os.path.join(ws_a, ".tea_agent_run")
        img_dir = os.path.join(run_a, "artifacts", "images")
        if not os.path.isdir(img_dir):
            os.makedirs(img_dir)
        with open(os.path.join(img_dir, img_name), "wb") as f:
            f.write(img_bytes)
        img_session_id = "s_bundleimg01"
        img_session = {
            "id": img_session_id,
            "title": "the topic with a picture",
            "created_at": "2026-11-25T00:00:00Z",
            "updated_at": "2026-11-25T00:00:00Z",
            "messages": [
                {"role": "user", "content": "look at this",
                 "images": [{"path": "C:/another/device/.tea_agent_run/artifacts/images/" + img_name,
                             "mime": "image/png"}]},
                {"role": "assistant", "content": "I see it"},
            ],
        }
        with open(os.path.join(run_a, "sessions", img_session_id + ".json"), "w",
                  encoding="utf-8") as f:
            json.dump(img_session, f, ensure_ascii=False, indent=2)

        # ---- the export --------------------------------------------------
        status, headers, blob = raw_request(agent_port, "GET",
                                            "/api/workspaces/%s/bundle" % ws_a_id)
        check("GET the bundle answers", status == 200, status)
        check("the answer is a zip",
              headers.get("Content-Type", "").startswith("application/zip"),
              headers.get("Content-Type"))
        check("the zip has bytes", len(blob) > 0, len(blob))
        check("the answer says how many topics went in",
              headers.get("X-Bundle-Sessions") == "2",
              headers.get("X-Bundle-Sessions"))
        check("the answer is a download",
              "attachment" in (headers.get("Content-Disposition") or ""),
              headers.get("Content-Disposition"))

        names = []
        manifest = {}
        session_entry = {}
        try:
            zf = zipfile.ZipFile(io.BytesIO(blob))
            names = zf.namelist()
            check("the bundle is a readable zip", zf.testzip() is None, names[:5])
            check("it has a manifest", "manifest.json" in names, names[:8])
            manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
            check("the manifest says what it is",
                  manifest.get("kind") == "tea_a2.workspace.bundle", manifest.get("kind"))
            listed = sorted(s.get("id") for s in manifest.get("sessions") or [])
            check("the manifest lists the topics",
                  listed == sorted([session_id, img_session_id]), listed)
            session_entry = "sessions/%s.json" % session_id
            check("the topic is in the bundle", session_entry in names, names[:8])
            check("the trajectory is in the bundle",
                  "trajectories/%s.jsonl" % session_id in names, names[:8])
            check("the memory is in the bundle", "memory.json" in names, names[:8])
            check("agent.md is in the bundle", "agent.md" in names, names[:8])
            check("the picture is in the bundle",
                  "images/%s/%s" % (img_session_id, img_name) in names, names[:10])
            exported = json.loads(zf.read(session_entry).decode("utf-8"))
            check("the exported topic has the messages",
                  len(exported.get("messages") or []) >= 2,
                  len(exported.get("messages") or []))
        except zipfile.BadZipFile as exc:
            check("the bundle is a readable zip", False, exc)

        # ---- the import into the other workspace ------------------------
        status, _, report = raw_request(
            agent_port, "POST", "/api/workspaces/%s/bundle/import" % ws_b_id,
            body=blob, content_type="application/zip")
        parsed = {}
        try:
            parsed = json.loads(report.decode("utf-8"))
        except ValueError:
            pass
        check("the import answers", status == 200, (status, report[:200]))
        check("it says the topics were restored", parsed.get("sessions") == 2, parsed)
        check("it says nothing was in conflict", parsed.get("sessions_conflict") == 0, parsed)
        check("it says the trajectory came along",
              parsed.get("trajectories") == 1, parsed)
        check("it says the picture came along", parsed.get("images") == 1, parsed)

        _, _, sessions = request(agent_port, "GET",
                                 "/api/workspaces/%s/sessions" % ws_b_id)
        ids = [s.get("id") for s in sessions.get("sessions") or []]
        check("the other workspace has the topic now", session_id in ids, ids)
        # The restored file on the other device, byte for byte: a topic that
        # comes back different is not "restored".
        run_b = os.path.join(ws_b, ".tea_agent_run")
        restored_path = os.path.join(run_b, "sessions", session_id + ".json")
        check("the topic file is there", os.path.exists(restored_path), restored_path)
        restored_bytes = b""
        if os.path.exists(restored_path):
            with open(restored_path, "rb") as f:
                restored_bytes = f.read()
        exported_bytes = b""
        try:
            zf = zipfile.ZipFile(io.BytesIO(blob))
            exported_bytes = zf.read(session_entry)
        except Exception:
            pass
        check("the restored topic is byte for byte the exported one",
              restored_bytes == exported_bytes,
              (len(restored_bytes), len(exported_bytes)))
        restored = json.loads(restored_bytes.decode("utf-8")) if restored_bytes else {}
        check("the messages came along",
              len(restored.get("messages") or []) >= 2,
              len(restored.get("messages") or []))
        traj_path = os.path.join(run_b, "trajectories", session_id + ".jsonl")
        check("the trajectory file is there", os.path.exists(traj_path), traj_path)

        _, _, memory = request(agent_port, "GET",
                               "/api/workspaces/%s/memory" % ws_b_id)
        texts = [n.get("text") for n in (memory or {}).get("notes") or []]
        check("the memory note came along",
              any("cmake --build build-mingw" in (t or "") for t in texts), texts)

        _, _, agent_doc = request(agent_port, "GET",
                                  "/api/workspaces/%s/agent-doc" % ws_b_id)
        check("agent.md came along",
              "the bundle carries this" in ((agent_doc or {}).get("content") or ""),
              agent_doc)

        # The picture landed here, and the topic points at it now.
        local_img = os.path.join(run_b, "artifacts", "images", img_name)
        check("the picture is on the other device", os.path.exists(local_img), local_img)
        if os.path.exists(local_img):
            with open(local_img, "rb") as f:
                check("the picture is byte for byte the exported one",
                      f.read() == img_bytes)
        img_restored_path = os.path.join(run_b, "sessions", img_session_id + ".json")
        check("the topic with the picture is there",
              os.path.exists(img_restored_path), img_restored_path)
        if os.path.exists(img_restored_path):
            with open(img_restored_path, "r", encoding="utf-8") as f:
                img_restored = json.load(f)
            stored = (img_restored.get("messages") or [{}])[0].get("images") or [{}]
            new_path = stored[0].get("path") or ""
            check("the topic points at the picture on this device",
                  new_path.endswith(img_name) and "another/device" not in new_path,
                  new_path)
            check("the picture the topic points at is really there",
                  os.path.exists(os.path.join(ws_b, new_path.replace("/", os.sep))),
                  new_path)

        # ---- the local data wins ----------------------------------------
        # The local copy is changed by hand (the file and the index): the
        # import must not touch it.
        with open(restored_path, "r", encoding="utf-8") as f:
            local = json.load(f)
        local["title"] = "the local title"
        with open(restored_path, "w", encoding="utf-8") as f:
            json.dump(local, f, ensure_ascii=False, indent=2)
        index_path = os.path.join(run_b, "sessions", "index.json")
        with open(index_path, "r", encoding="utf-8") as f:
            index = json.load(f)
        for entry in index:
            if entry.get("id") == session_id:
                entry["title"] = "the local title"
        with open(index_path, "w", encoding="utf-8") as f:
            json.dump(index, f, ensure_ascii=False, indent=2)
        local_bytes = b""
        with open(restored_path, "rb") as f:
            local_bytes = f.read()

        status, _, report2 = raw_request(
            agent_port, "POST", "/api/workspaces/%s/bundle/import" % ws_b_id,
            body=blob, content_type="application/zip")
        parsed2 = {}
        try:
            parsed2 = json.loads(report2.decode("utf-8"))
        except ValueError:
            pass
        check("the second import answers", status == 200, status)
        check("it restores nothing", parsed2.get("sessions") == 0, parsed2)
        check("it reports the conflict",
              parsed2.get("sessions_conflict") == 2, parsed2)
        check("it says which one was kept",
              any("already here" in c for c in parsed2.get("conflicts") or []),
              parsed2.get("conflicts"))

        _, _, sessions = request(agent_port, "GET",
                                 "/api/workspaces/%s/sessions" % ws_b_id)
        titles = {s.get("id"): s.get("title") for s in sessions.get("sessions") or []}
        check("the local title was kept", titles.get(session_id) == "the local title", titles)
        with open(restored_path, "rb") as f:
            after_bytes = f.read()
        check("the local file was not touched", after_bytes == local_bytes,
              (len(after_bytes), len(local_bytes)))
        check("the local memory note was kept too",
              parsed2.get("notes_conflict") == 1, parsed2)

        # ---- a file that is not a bundle ---------------------------------
        status, _, body = raw_request(
            agent_port, "POST", "/api/workspaces/%s/bundle/import" % ws_b_id,
            body=b"this is not a zip file", content_type="application/zip")
        error = {}
        try:
            error = (json.loads(body.decode("utf-8")) or {}).get("error") or {}
        except ValueError:
            pass
        check("a file that is not a bundle is refused", status == 400, status)
        check("the refusal names the code", error.get("code") == "bad_bundle", error)

        status, _, body = raw_request(
            agent_port, "POST", "/api/workspaces/%s/bundle/import" % ws_b_id,
            body=b"", content_type="application/zip")
        check("an empty body is refused", status == 400, status)

        # ---- importing into the source keeps everything ------------------
        status, _, report3 = raw_request(
            agent_port, "POST", "/api/workspaces/%s/bundle/import" % ws_a_id,
            body=blob, content_type="application/zip")
        parsed3 = {}
        try:
            parsed3 = json.loads(report3.decode("utf-8"))
        except ValueError:
            pass
        check("importing into the source answers", status == 200, status)
        check("the source keeps its own topic",
              parsed3.get("sessions_conflict") == 2 and parsed3.get("sessions") == 0,
              parsed3)
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
