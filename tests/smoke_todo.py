#!/usr/bin/env python3
"""Smoke test for the todo list (task decomposition + progress).

A complex job is easier to follow - for the model and for the person watching -
when it is written down as steps. `toolkit_todo` maintains that list on the
session; it is injected into the system prompt every turn and pushed to clients
as a `todo` event on every change.

What this pins:

  * op=set writes the whole plan; op=list/add/update/clear behave as documented;
  * the list is injected into the system prompt, so a later turn (and a compacted
    one) still knows the plan;
  * a change emits a `todo` SSE event with items + counts, mid-turn;
  * state is persisted in the session file and survives a restart (the API and
    the index report 3/7 without loading every session);
  * only one step may be active at a time - marking another active demotes the
    previous one instead of leaving two "in progress";
  * a completed list says so in the tool output;
  * the list is editable from outside (PUT /api/sessions/:id/todos), which is
    what the UI checkbox uses, and a bad status is refused;
  * the exported HTML carries the list; importing it restores the progress.

Usage: python3 tests/smoke_todo.py [path-to-tea_agent-binary]
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


class MockProvider(BaseHTTPRequestHandler):
    records = []
    # The scripted plan the mock asks the agent to run. Kept in one place so the
    # assertions and the mock cannot drift apart.
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
        self._chunk({"id": "c", "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]})
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


def call(port, method, path, body=None, timeout=60, raw_body=None, content_type=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    headers = {}
    payload = None
    if raw_body is not None:
        payload = raw_body
        headers["Content-Type"] = content_type or "text/html; charset=utf-8"
    elif body is not None:
        payload = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    conn.request(method, path, body=payload, headers=headers)
    resp = conn.getresponse()
    raw = resp.read()
    try:
        return resp.status, json.loads(raw or b"null")
    except ValueError:
        return resp.status, {"raw": raw.decode("utf-8", "replace")}


def run_turn(port, workspace_id, session_id, message="go"):
    body = {"workspace_id": workspace_id, "session_id": session_id, "message": message}
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


def system_prompt(request):
    for m in request.get("messages") or []:
        if m.get("role") == "system":
            c = m.get("content")
            if isinstance(c, list):
                return "".join(p.get("text", "") for p in c if isinstance(p, dict))
            return c or ""
    return ""


def tool_output(events, name="toolkit_todo"):
    for e in events:
        if e.get("type") == "tool_result" and e.get("name") == name:
            return e.get("output", "")
    return ""


def todo_events(events):
    return [e for e in events if e.get("type") == "todo"]


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

    tmp = tempfile.mkdtemp(prefix="tea_todo_")
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
roles: {main: {provider: mock, model: m}}
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

    run_dir = os.path.join(ws_dir, ".tea_agent_run")

    try:
        if not wait_health(port):
            print("server did not become healthy")
            return 1
        _, listing = call(port, "GET", "/api/workspaces")
        wid = listing["workspaces"][0]["id"]
        _, sess = call(port, "POST", "/api/workspaces/%s/sessions" % wid, {"title": "todo"})
        sid = sess["id"]
        session_file = os.path.join(run_dir, "sessions", sid + ".json")

        # ---- the tool is advertised ----------------------------------------
        MockProvider.calls = [("c0", "toolkit_todo", {"op": "list"})]
        events = run_turn(port, wid, sid, "what is planned?")
        names = sorted(t.get("function", {}).get("name")
                       for t in (MockProvider.records[0].get("tools") or []))
        check("toolkit_todo is advertised", "toolkit_todo" in names, names)
        check("op=list on an empty list says so", "empty" in tool_output(events),
              tool_output(events)[:120])

        # ---- set the plan ---------------------------------------------------
        steps = ["读取并理解现有代码", "实现 todo 工具", "在 UI 上呈现进度", "补充测试"]
        MockProvider.calls = [("c1", "toolkit_todo", {"op": "set", "items": steps})]
        events = run_turn(port, wid, sid, "plan this")
        out = tool_output(events)
        check("op=set accepts a list of strings", "0/4 done" in out, out[:160])
        check("every step is listed", all(t in out for t in steps), out)
        evs = todo_events(events)
        check("a todo event is emitted", len(evs) == 1, len(evs))
        check("the event carries the items",
              len(evs[0].get("items") or []) == 4 if evs else False, evs)
        check("the event carries counts",
              evs and evs[0].get("total") == 4 and evs[0].get("done") == 0, evs)

        # ---- persisted, and in the prompt of the *next* turn ----------------
        saved = json.load(open(session_file, encoding="utf-8"))
        check("the list is persisted in the session file",
              [t["text"] for t in saved.get("todos") or []] == steps, saved.get("todos"))
        _, detail = call(port, "GET", "/api/sessions/%s" % sid)
        check("GET /api/sessions exposes the list",
              [t["text"] for t in detail.get("todos") or []] == steps, detail.get("todos"))
        check("every step starts pending",
              all(t["status"] == "pending" for t in detail.get("todos") or []),
              detail.get("todos"))
        _, index = call(port, "GET", "/api/workspaces/%s/sessions" % wid)
        entry = [s for s in index["sessions"] if s["id"] == sid][0]
        check("the index reports total/done", entry.get("todo_total") == 4
              and entry.get("todo_done") == 0, entry)

        MockProvider.calls = [("c2", "toolkit_todo", {"op": "list"})]
        before = len(MockProvider.records)
        events = run_turn(port, wid, sid, "remind me")
        prompt = system_prompt(MockProvider.records[before])
        check("the system prompt carries the task list", "Task list (0/4 done" in prompt,
              prompt[-400:])
        check("the prompt lists the steps", all(t in prompt for t in steps), prompt[-400:])
        check("the prompt says how to maintain it", "toolkit_todo" in prompt
              and "active" in prompt)

        # ---- progress ------------------------------------------------------
        MockProvider.calls = [
            ("c3", "toolkit_todo", {"op": "update", "index": 1, "status": "active"}),
        ]
        events = run_turn(port, wid, sid, "start")
        out = tool_output(events)
        check("op=update marks a step active", "in progress" in out and "[~]" in out, out[:200])
        evs = todo_events(events)
        check("the todo event reports the active step",
              evs and [t["status"] for t in evs[0]["items"]][0] == "active", evs)

        MockProvider.calls = [
            ("c4", "toolkit_todo", {"op": "update", "index": 1, "status": "done"}),
            ("c5", "toolkit_todo", {"op": "update", "index": 2, "status": "active"}),
        ]
        events = run_turn(port, wid, sid, "next")
        evs = todo_events(events)
        check("two updates in one iteration emit two events", len(evs) == 2, len(evs))
        statuses = [t["status"] for t in evs[-1]["items"]] if evs else []
        check("the second update is reflected",
              statuses[:2] == ["done", "active"], statuses)

        # Only one step may be in progress: promoting another demotes the first.
        MockProvider.calls = [
            ("c6", "toolkit_todo", {"op": "update", "index": 3, "status": "active"}),
        ]
        events = run_turn(port, wid, sid, "switch")
        _, detail = call(port, "GET", "/api/sessions/%s" % sid)
        st = [t["status"] for t in detail["todos"]]
        check("only one step is active at a time",
              st.count("active") == 1 and st[1] == "pending" and st[2] == "active", st)

        # ---- add / remove / complete ---------------------------------------
        MockProvider.calls = [("c7", "toolkit_todo", {"op": "add", "text": "写文档"})]
        events = run_turn(port, wid, sid, "one more")
        _, detail = call(port, "GET", "/api/sessions/%s" % sid)
        check("op=add appends a step", len(detail["todos"]) == 5
              and detail["todos"][-1]["text"] == "写文档", detail["todos"])
        check("a new step gets an id", detail["todos"][-1].get("id") == "t5",
              detail["todos"][-1])

        MockProvider.calls = [
            ("c8", "toolkit_todo", {"op": "update", "id": "t5", "status": "done"}),
        ]
        events = run_turn(port, wid, sid, "finish docs")
        out = tool_output(events)
        check("op=update accepts an id", "updated step 5" in out, out[:120])

        # ---- completing everything says so ---------------------------------
        MockProvider.calls = [("c9", "toolkit_todo", {
            "op": "set",
            "items": [{"text": t, "status": "done"} for t in steps],
        })]
        events = run_turn(port, wid, sid, "all done")
        out = tool_output(events)
        check("an all-done list is reported as complete",
              "4/4 done" in out and "All steps are done" in out, out[:240])
        _, index = call(port, "GET", "/api/workspaces/%s/sessions" % wid)
        entry = [s for s in index["sessions"] if s["id"] == sid][0]
        check("the index shows 4/4", entry.get("todo_total") == 4
              and entry.get("todo_done") == 4, entry)

        # ---- refusals -------------------------------------------------------
        MockProvider.calls = [("c10", "toolkit_todo", {
            "op": "set",
            "items": [{"text": "a", "status": "active"}, {"text": "b", "status": "active"}],
        })]
        events = run_turn(port, wid, sid, "bad plan")
        out = tool_output(events)
        check("two active steps are refused",
              "only one step may be 'active'" in out, out[:160])
        _, detail = call(port, "GET", "/api/sessions/%s" % sid)
        check("the refused plan did not replace the list", len(detail["todos"]) == 4,
              detail["todos"])

        MockProvider.calls = [("c11", "toolkit_todo", {"op": "update", "index": 99,
                                                      "status": "done"})]
        events = run_turn(port, wid, sid, "bad index")
        check("a bad index is refused", "no such step" in tool_output(events),
              tool_output(events)[:160])

        MockProvider.calls = [("c12", "toolkit_todo", {"op": "frobnicate"})]
        events = run_turn(port, wid, sid, "bad op")
        check("an unknown op is refused", "unsupported op" in tool_output(events),
              tool_output(events)[:160])

        # ---- editable from outside (the UI checkbox path) -------------------
        _, detail = call(port, "GET", "/api/sessions/%s" % sid)
        items = detail["todos"]
        items[0]["status"] = "pending"
        status, updated = call(port, "PUT", "/api/sessions/%s/todos" % sid,
                               {"items": items})
        check("PUT replaces the list", status == 200 and updated.get("ok") is True,
              (status, updated))
        check("the counts drop back to 3/4",
              updated.get("total") == 4 and updated.get("done") == 3, updated)
        status, one = call(port, "PUT", "/api/sessions/%s/todos" % sid,
                           {"index": 1, "status": "done"})
        check("PUT can tick a single item", status == 200 and one.get("done") == 4, one)
        status, err = call(port, "PUT", "/api/sessions/%s/todos" % sid,
                           {"index": 1, "status": "finished"})
        check("an invalid status is refused",
              status == 400 and "pending, active or done" in
              ((err.get("error") or {}).get("message") or ""), (status, err))
        status, err = call(port, "PUT", "/api/sessions/%s/todos" % sid,
                           {"index": 99, "status": "done"})
        check("an unknown index is 404", status == 404, status)

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
        check("the task list survived the restart",
              [t["text"] for t in detail.get("todos") or []] == steps, detail.get("todos"))
        MockProvider.calls = [("c13", "toolkit_todo", {"op": "list"})]
        before = len(MockProvider.records)
        run_turn(port, wid, sid, "where are we?")
        prompt = system_prompt(MockProvider.records[before])
        check("a fresh process still injects the plan", "Task list (4/4 done" in prompt,
              prompt[-300:])

        # ---- export / import carry the list --------------------------------
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
        conn.request("GET", "/api/sessions/%s/export" % sid)
        resp = conn.getresponse()
        html = resp.read()
        status = resp.status
        text = html.decode("utf-8")
        check("the export contains the task list", "class=\"todos\"" in text
              and all(t in text for t in steps), text.count("todos"))
        payload = json.loads(
            text.split('id="tea-session-data">', 1)[1].split("</script>", 1)[0].replace("<\\/", "</"))
        check("the embedded JSON carries the list",
              [t["text"] for t in payload.get("todos") or []] == steps, payload.get("todos"))
        status, imported = call(port, "POST", "/api/workspaces/%s/sessions/import" % wid,
                                raw_body=text.encode("utf-8"),
                                content_type="text/html; charset=utf-8")
        check("importing restores the list", status == 200, (status, imported))
        new_sid = (imported.get("session") or {}).get("id")
        _, restored = call(port, "GET", "/api/sessions/%s" % new_sid)
        check("the restored session has the same steps",
              [t["text"] for t in restored.get("todos") or []] == steps, restored.get("todos"))
        check("the restored session has the same progress",
              all(t["status"] == "done" for t in restored.get("todos") or []),
              restored.get("todos"))
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
