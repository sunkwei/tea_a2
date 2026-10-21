#!/usr/bin/env python3
"""Multi-session / multi-client smoke test.

The intended setup is a handful of browsers (three, say) sharing one server, each
looking at its own topic. A topic is a session; watching it is a subscription to
that session's running turn. What this pins, and why:

  1. Three sessions of one workspace stream at the same time and all finish. A
     turn is a worker thread keyed by session id - a second tab must never be
     able to block, slow down or cancel the first one.
  2. A browser that joins a turn late replays exactly the same output, so two
     windows on one topic really do see the same thing.
  3. A second message for a session that is already running is QUEUED by default
     (it is adopted at the agent's next iteration), not thrown at a new turn. The
     old behaviour lost data: the retiring worker saved its own Session copy
     after the new turn had written the answer.
  4. `{"interrupt": true}` replaces the running turn, and the replacement starts
     only after the old worker stopped writing - so the new question and answer
     are on disk afterwards, next to what the old turn produced.
  5. Session mutations (todo ticks, rename, delete) are refused with 409 while
     the turn is running, because the worker would overwrite them.
  6. Followers of one turn are capped (429 too_many_subscribers) and a rejected
     follower does not take the server down: /api/health keeps answering. Before
     the pool was made dynamic, the 32nd open stream made every request hang.
  7. Three topics each with their own watcher are live at the same time, and each
     reports its own subscriber count.

Usage: python3 tests/smoke_multi_session.py [path-to-tea_agent-binary]
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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHUNKS = 24          # ~2.4 s per answer with the mock below
CHUNK_DELAY = 0.1
LONG_CHUNKS = 120    # ~12 s: a turn that is still running while the test acts


class MockProvider(BaseHTTPRequestHandler):
    """OpenAI-shaped stream; prompts starting with "long" stream much longer."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw or b"{}")
            users = [str(m.get("content") or "") for m in body.get("messages") or []
                     if m.get("role") == "user"]
            prompt = users[-1] if users else ""
        except ValueError:
            prompt = ""
        chunks = LONG_CHUNKS if prompt.startswith("long") else CHUNKS
        tag = (prompt[:6] or "x").strip() or "x"

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        def write(text):
            data = text.encode()
            self.wfile.write(("%x\r\n" % len(data)).encode() + data + b"\r\n")
            self.wfile.flush()

        def chunk(delta, finish=None):
            write("data: " + json.dumps({
                "id": "c1", "object": "chat.completion.chunk",
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }) + "\n\n")

        try:
            for i in range(chunks):
                chunk({"content": "%s-%02d " % (tag, i)})
                time.sleep(CHUNK_DELAY)
            chunk({}, "stop")
            write("data: [DONE]\n\n")
            write("")
        except OSError:
            pass


def free_port():
    while True:
        port = random.randint(20000, 60000)
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue


def call(port, method, path, body=None, timeout=30):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request(method, path, body=json.dumps(body) if body is not None else None,
                     headers={"Content-Type": "application/json"} if body is not None else {})
        resp = conn.getresponse()
        raw = resp.read()
        try:
            return resp.status, json.loads(raw or b"null")
        except ValueError:
            return resp.status, {"raw": raw.decode("utf-8", "replace")}
    finally:
        conn.close()


def stream(port, path, body, out, timeout=120):
    """Read one SSE response into `out`; never raises, records failures instead."""
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        conn.request("POST", path, body=json.dumps(body),
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        if resp.status != 200:
            out.append({"type": "__http_error__", "status": resp.status,
                        "body": resp.read().decode("utf-8", "replace")})
            conn.close()
            return
        while True:
            line = resp.readline()
            if not line:
                break
            line = line.strip()
            if not line.startswith(b"data: "):
                continue
            payload = line[6:]
            if payload == b"[DONE]":
                break
            try:
                event = json.loads(payload)
            except ValueError:
                continue
            event["__t"] = time.time()
            out.append(event)
        conn.close()
    except Exception as exc:  # noqa: BLE001
        out.append({"type": "__exception__", "error": repr(exc)})


def wait_health(port, timeout=30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if call(port, "GET", "/api/health", timeout=2)[0] == 200:
                return True
        except OSError:
            pass
        time.sleep(0.2)
    return False


def text_of(events):
    return "".join(e.get("text", "") for e in events if e.get("type") == "content_delta")


def types_of(events):
    return [e.get("type") for e in events]


def wait_until(fn, timeout=20.0, step=0.1):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if fn():
            return True
        time.sleep(step)
    return False


def start_session(port, wid, title):
    return call(port, "POST", "/api/workspaces/%s/sessions" % wid, {"title": title})[1]["id"]


def persisted(port, sid):
    return call(port, "GET", "/api/sessions/" + sid)[1]


def main():
    binary = (sys.argv[1] if len(sys.argv) > 1
              else os.path.join(REPO_ROOT, "build", "bin", "tea_agent"))
    if not os.path.exists(binary):
        print("binary not found:", binary)
        return 1

    mock = ThreadingHTTPServer(("127.0.0.1", 0), MockProvider)
    mock.daemon_threads = True
    threading.Thread(target=mock.serve_forever, daemon=True).start()

    tmp = tempfile.mkdtemp(prefix="tea_multi_session_")
    ws_dir = os.path.join(tmp, "ws")
    os.makedirs(ws_dir)
    config_path = os.path.join(tmp, "provider.yaml")
    with open(config_path, "w", encoding="utf-8") as f:
        f.write("""version: 1
providers:
  mock:
    api_url: http://127.0.0.1:%d
    api_key: sk-test
    default_model: m
    models:
      m:
        max_context_tokens: 32768
        max_output_tokens: 4096
        supports_tools: true
""" % mock.server_address[1])

    port = free_port()
    env = dict(os.environ)
    env["TEA_AGENT_MAX_SUBSCRIBERS"] = "3"     # starter + 2 watchers
    proc = subprocess.Popen([
        binary, "--config", config_path, "--tea-home", os.path.join(tmp, "home"),
        "--host", "127.0.0.1", "--port", str(port), "--no-console",
        "--log-file", os.path.join(tmp, "tea_agent.log"), "--workspace", ws_dir,
    ], env=env)

    failures = []
    checks = []

    def check(name, ok, detail=""):
        checks.append(name)
        print(("PASS " if ok else "FAIL ") + name + ((("  [" + str(detail) + "]")) if not ok else ""))
        if not ok:
            failures.append(name)

    try:
        if not wait_health(port):
            print("server did not become healthy")
            return 1
        wid = call(port, "GET", "/api/workspaces")[1]["workspaces"][0]["id"]

        # ---- 1. three sessions, three browsers ----------------------------
        sids = [start_session(port, wid, "topic-%d" % i) for i in range(3)]
        events = {sid: [] for sid in sids}
        threads = []
        for i, sid in enumerate(sids):
            t = threading.Thread(target=stream, args=(
                port, "/api/chat", {"workspace_id": wid, "session_id": sid,
                                    "message": "t%d hello" % i}, events[sid]))
            t.start()
            threads.append(t)
        for t in threads:
            t.join(60)

        def span(evs):
            return (evs[0]["__t"], evs[-1]["__t"]) if evs else (0, 0)

        spans = [span(events[sid]) for sid in sids]
        overlap = all(spans[i][0] < spans[j][1] for i in range(3) for j in range(3) if i != j)
        check("three sessions stream at the same time", overlap, spans)
        check("all three turns finished",
              all("done" in types_of(events[sid]) for sid in sids),
              {sid: types_of(events[sid])[-2:] for sid in sids})
        check("each turn produced its own answer",
              len({text_of(events[sid]) for sid in sids}) == 3,
              [len(text_of(events[sid])) for sid in sids])

        # ---- 2. a late watcher sees exactly the same output ---------------
        sid_a = sids[0]
        first, watcher, second = [], [], []
        t1 = threading.Thread(target=stream, args=(
            port, "/api/chat", {"workspace_id": wid, "session_id": sid_a,
                                "message": "long watch me"}, first))
        t1.start()
        wait_until(lambda: any(e.get("type") == "start" for e in first))
        time.sleep(0.6)
        t2 = threading.Thread(target=stream, args=(
            port, "/api/chat/attach", {"session_id": sid_a, "since": 0}, watcher))
        t2.start()
        t3 = threading.Thread(target=stream, args=(
            port, "/api/chat/attach", {"session_id": sid_a, "since": 0}, second))
        t3.start()
        for t in (t1, t2, t3):
            t.join(120)
        check("two watchers replay the same output as the starter",
              text_of(watcher) == text_of(first) and text_of(second) == text_of(first)
              and len(text_of(first)) > 100,
              "%d / %d / %d chars" % (len(text_of(watcher)), len(text_of(second)),
                                      len(text_of(first))))
        check("no turn is reported after they finished",
              call(port, "GET", "/api/chat/turns")[1]["turns"] == [])

        # ---- 3. same session: queued by default ---------------------------
        first = []
        t1 = threading.Thread(target=stream, args=(
            port, "/api/chat", {"workspace_id": wid, "session_id": sid_a,
                                "message": "long first question"}, first))
        t1.start()
        wait_until(lambda: any(e.get("type") == "content_delta" for e in first))
        status, body = call(port, "POST", "/api/chat",
                            {"workspace_id": wid, "session_id": sid_a,
                             "message": "second question"}, timeout=30)
        queued_ok = (status == 200 and body.get("turn_running") is True
                     and body.get("queued", {}).get("content") == "second question")
        check("a second message for a running session is queued, not started",
              queued_ok, (status, body))
        pending = call(port, "GET", "/api/chat/queue?session_id=" + sid_a)[1]
        check("the queued message is visible on the queue endpoint",
              pending.get("pending") == 1, pending)
        t1.join(120)
        adopted = any(e.get("type") == "queue_adopted" and e.get("content") == "second question"
                      for e in first)
        check("the running turn adopts the queued message", adopted,
              [e.get("type") for e in first][-6:])
        check("the queued message did not cancel the turn",
              "stopped" not in types_of(first), types_of(first)[-3:])
        msgs = persisted(port, sid_a)["messages"]
        users = [str(m.get("content") or "") for m in msgs if m.get("role") == "user"]
        answers = [str(m.get("content") or "") for m in msgs if m.get("role") == "assistant"]
        check("both questions and both answers are on disk",
              users.count("long first question") == 1 and users.count("second question") == 1
              and any(a.startswith("second") for a in answers),
              (users, [a[:12] for a in answers]))

        # ---- 4. explicit interrupt keeps both halves ----------------------
        first, replacement = [], []
        slow_sid = start_session(port, wid, "interrupt-me")
        t1 = threading.Thread(target=stream, args=(
            port, "/api/chat", {"workspace_id": wid, "session_id": slow_sid,
                                "message": "long first"}, first))
        t1.start()
        wait_until(lambda: any(e.get("type") == "content_delta" for e in first))
        t2 = threading.Thread(target=stream, args=(
            port, "/api/chat", {"workspace_id": wid, "session_id": slow_sid,
                                "message": "replacement question", "interrupt": True},
            replacement))
        t2.start()
        t1.join(120)
        t2.join(120)
        check("interrupt stops the old turn", "stopped" in types_of(first), types_of(first)[-3:])
        check("the replacement turn finishes", "done" in types_of(replacement),
              types_of(replacement)[-3:])
        msgs = persisted(port, slow_sid)["messages"]
        users = [str(m.get("content") or "") for m in msgs if m.get("role") == "user"]
        answers = [str(m.get("content") or "") for m in msgs if m.get("role") == "assistant"]
        check("the interrupted turn's question is still there", "long first" in users, users)
        check("the replacement question and answer survived",
              "replacement question" in users
              and any(a.startswith("replac") for a in answers),
              (users, [a[:12] for a in answers]))

        # ---- 5. mutations are refused while the turn runs -----------------
        mutate_sid = start_session(port, wid, "mutation-guard")
        running = []
        t1 = threading.Thread(target=stream, args=(
            port, "/api/chat", {"workspace_id": wid, "session_id": mutate_sid,
                                "message": "long keep running"}, running))
        t1.start()
        wait_until(lambda: any(e.get("type") == "content_delta" for e in running))
        st_todo, br_todo = call(port, "PUT", "/api/sessions/%s/todos" % mutate_sid,
                                {"items": [{"text": "manual tick"}]})
        st_rename, br_rename = call(port, "PATCH", "/api/sessions/" + mutate_sid,
                                    {"title": "renamed while running"})
        st_delete, br_delete = call(port, "DELETE", "/api/sessions/" + mutate_sid)
        check("PUT todos is refused while the turn runs",
              st_todo == 409 and br_todo.get("error", {}).get("code") == "turn_running",
              (st_todo, br_todo))
        check("PATCH rename is refused while the turn runs",
              st_rename == 409 and br_rename.get("error", {}).get("code") == "turn_running",
              (st_rename, br_rename))
        check("DELETE is refused while the turn runs",
              st_delete == 409 and br_delete.get("error", {}).get("code") == "turn_running",
              (st_delete, br_delete))
        call(port, "POST", "/api/chat/stop", {"session_id": mutate_sid})
        t1.join(120)
        check("after stopping, the same edits are accepted",
              call(port, "PUT", "/api/sessions/%s/todos" % mutate_sid,
                   {"items": [{"text": "manual tick"}]})[0] == 200
              and call(port, "PATCH", "/api/sessions/" + mutate_sid,
                       {"title": "renamed after stopping"})[0] == 200,
              types_of(running)[-2:])

        # ---- 6. the follower cap holds, the server stays responsive -------
        cap_sid = start_session(port, wid, "subscriber-cap")
        starter = []
        t1 = threading.Thread(target=stream, args=(
            port, "/api/chat", {"workspace_id": wid, "session_id": cap_sid,
                                "message": "long many watchers"}, starter))
        t1.start()
        wait_until(lambda: any(e.get("type") == "content_delta" for e in starter))
        threads = []
        for _ in range(2):
            out = []
            t = threading.Thread(target=stream,
                                 args=(port, "/api/chat/attach",
                                       {"session_id": cap_sid, "since": 0}, out))
            t.start()
            threads.append(t)
            time.sleep(0.2)
        refused = []
        for _ in range(2):
            st, br = call(port, "POST", "/api/chat/attach",
                          {"session_id": cap_sid, "since": 0}, timeout=10)
            refused.append((st, br.get("error", {}).get("code")))
        check("followers beyond the cap are refused with 429",
              all(st == 429 and code == "too_many_subscribers" for st, code in refused), refused)
        started = time.time()
        healthy = call(port, "GET", "/api/health", timeout=5)[0] == 200
        check("the server still answers while the cap is being hit",
              healthy and time.time() - started < 2.0, "%.2fs" % (time.time() - started))
        live = call(port, "GET", "/api/chat/turn?session_id=" + cap_sid)[1]
        check("the running turn reports its live subscriber count",
              live.get("subscribers") == 3, live)
        call(port, "POST", "/api/chat/stop", {"session_id": cap_sid})
        t1.join(120)
        for t in threads:
            t.join(30)

        # ---- 7. three topics, one watcher each, all live at once ----------
        trio = [start_session(port, wid, "browser-%d" % i) for i in range(3)]
        streams = {sid: [] for sid in trio}
        watchers = {sid: [] for sid in trio}
        threads = []
        for i, sid in enumerate(trio):
            t = threading.Thread(target=stream, args=(
                port, "/api/chat", {"workspace_id": wid, "session_id": sid,
                                    "message": "long%d browser" % i}, streams[sid]))
            t.start()
            threads.append(t)
        for sid in trio:
            wait_until(lambda sid=sid: any(e.get("type") == "content_delta"
                                           for e in streams[sid]))
            t = threading.Thread(target=stream, args=(
                port, "/api/chat/attach", {"session_id": sid, "since": 0}, watchers[sid]))
            t.start()
            threads.append(t)
        time.sleep(1.0)
        st, br = call(port, "GET", "/api/chat/turns", timeout=5)
        live = br.get("turns", []) if st == 200 else []
        check("all three topics are listed as running at once",
              len(live) == 3 and all(t.get("subscribers") == 2 for t in live),
              [(t.get("session_id"), t.get("subscribers")) for t in live])
        for sid in trio:
            call(port, "POST", "/api/chat/stop", {"session_id": sid})
        for t in threads:
            t.join(120)
        check("the three topics each streamed their own output",
              all(len(text_of(streams[sid])) > 0 for sid in trio)
              and len({text_of(streams[sid])[:20] for sid in trio}) == 3,
              [len(text_of(streams[sid])) for sid in trio])
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
