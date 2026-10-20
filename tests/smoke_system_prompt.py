#!/usr/bin/env python3
"""Smoke test for the per-workspace system prompt (settings page).

The prompt lives in `<workspace>/.tea_agent_run/system_prompt.md` and is read
fresh for every request, so the web UI can edit it without restarting the agent:

  * with no file the workspace falls back to the built-in default (or to
    `settings.yaml: system_prompt` when that is set);
  * GET /api/workspaces/{id}/system-prompt reports the override, the fallback and
    the cost of the prompt the next turn will actually send; `current_prompt` is
    the persona really in effect (the default when there is no override), so the
    UI can show it instead of an empty box;
  * PUT saves the override and the *next* model request starts with it, while the
    environment block, workspace instructions and agent.md are still appended;
  * an empty/whitespace-only PUT clears the override and brings the default back;
  * the 64 KB cap is enforced instead of silently bloating every request.

Usage: python3 tests/smoke_system_prompt.py [path-to-tea_agent-binary]
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

    def log_message(self, *args):
        pass

    def _read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length) or b"{}")

    def _chunk(self, payload):
        try:
            self.wfile.write(("data: " + json.dumps(payload) + "\n\n").encode())
            self.wfile.flush()
        except OSError:
            pass

    def _content(self, text):
        self._chunk({"id": "c1", "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]})
        self._chunk({"id": "c1", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
        try:
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except OSError:
            pass

    def do_POST(self):
        if self.path != "/v1/chat/completions":
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        MockProvider.records.append(self._read_json())
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self._content("ok")


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


def call(port, method, path, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    headers = {"Content-Type": "application/json"} if body is not None else {}
    conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
    resp = conn.getresponse()
    raw = resp.read()
    try:
        return resp.status, json.loads(raw or b"null")
    except ValueError:
        return resp.status, {"raw": raw.decode("utf-8", "replace")}


def run_turn(port, workspace_id):
    """One full SSE turn; returns the events."""
    body = {"workspace_id": workspace_id, "message": "hello"}
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

    tmp = tempfile.mkdtemp(prefix="tea_sysprompt_")
    agent_port = free_port()
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
""" % mock_port)

    proc = subprocess.Popen([
        binary, "--config", config_path, "--tea-home", os.path.join(tmp, "home"),
        "--host", "127.0.0.1", "--port", str(agent_port), "--no-console",
        "--log-file", os.path.join(tmp, "tea_agent.log"), "--workspace", ws_dir,
    ])
    failures = []
    checks = []

    def check(name, ok, detail=""):
        checks.append(name)
        print(("PASS " if ok else "FAIL ") + name + (("  " + str(detail)) if not ok else ""))
        if not ok:
            failures.append(name)

    run_dir = os.path.join(ws_dir, ".tea_agent_run")
    prompt_path = os.path.join(run_dir, "system_prompt.md")

    try:
        if not wait_health(agent_port):
            print("server did not become healthy")
            return 1
        _, listing = call(agent_port, "GET", "/api/workspaces")
        wid = listing["workspaces"][0]["id"]

        # ---- default state --------------------------------------------------
        status, info = call(agent_port, "GET", "/api/workspaces/%s/system-prompt" % wid)
        check("GET system-prompt succeeds", status == 200, (status, info))
        check("no override by default",
              info.get("content") == "" and info.get("custom") is False, info)
        check("the fallback is the built-in prompt",
              info.get("default_source") == "builtin"
              and "tea_agent, a pragmatic software-engineering agent" in info.get("default_prompt", ""),
              info.get("default_source"))
        check("current_prompt is the persona actually in effect",
              info.get("current_prompt", "").startswith("You are tea_agent")
              and info.get("current_prompt") == info.get("default_prompt", "").strip(),
              info.get("current_prompt", "")[:80])
        check("the source names where the persona comes from",
              info.get("source") == "builtin", info.get("source"))
        check("the path is inside .tea_agent_run",
              info.get("relative_path", "").replace("\\", "/").endswith(".tea_agent_run/system_prompt.md"),
              info.get("relative_path"))
        check("the response reports the effective prompt size",
              info.get("effective_bytes", 0) > 0 and info.get("estimated_tokens", 0) > 0, info)

        before = len(MockProvider.records)
        run_turn(agent_port, wid)
        prompt = system_prompt(MockProvider.records[before])
        check("a turn without an override uses the built-in prompt",
              prompt.startswith("You are tea_agent"), prompt[:120])
        check("the environment block follows the persona", "Environment (probed" in prompt)
        check("agent.md is still appended", "Workspace experience" in prompt)

        # ---- save an override, next turn uses it ---------------------------
        marker = "You are a test persona for workspace system prompts."
        status, saved = call(agent_port, "PUT", "/api/workspaces/%s/system-prompt" % wid,
                             {"content": marker})
        check("PUT saves the override", status == 200 and saved.get("ok"), (status, saved))
        check("PUT reports it as custom",
              saved.get("custom") is True and marker in saved.get("content", ""), saved)
        check("PUT reports the new persona as current",
              saved.get("current_prompt") == marker and saved.get("source") == "workspace",
              (saved.get("current_prompt"), saved.get("source")))
        check("the override is on disk",
              os.path.isfile(prompt_path) and marker in open(prompt_path, encoding="utf-8").read(),
              prompt_path)

        before = len(MockProvider.records)
        run_turn(agent_port, wid)
        prompt = system_prompt(MockProvider.records[before])
        check("the next turn starts with the saved prompt", prompt.startswith(marker), prompt[:160])
        check("the built-in persona is replaced, not appended",
              "pragmatic software-engineering agent" not in prompt, prompt[:400])
        check("the environment block is still appended", "Environment (probed" in prompt)
        check("agent.md is still appended", "Workspace experience" in prompt)

        status, preview = call(agent_port, "GET", "/api/workspaces/%s/prompt" % wid)
        check("GET /prompt reflects the override",
              status == 200 and preview.get("has_system_prompt") is True
              and preview.get("system_prompt", "").startswith(marker), preview.get("has_system_prompt"))

        # ---- a second edit applies to the very next turn -------------------
        marker2 = "Persona v2: answer in haiku."
        call(agent_port, "PUT", "/api/workspaces/%s/system-prompt" % wid, {"content": marker2})
        before = len(MockProvider.records)
        run_turn(agent_port, wid)
        prompt = system_prompt(MockProvider.records[before])
        check("a later edit also reaches the next turn", prompt.startswith(marker2), prompt[:120])

        # ---- clearing restores the default --------------------------------
        status, cleared = call(agent_port, "PUT", "/api/workspaces/%s/system-prompt" % wid,
                               {"content": "   \n\n"})
        check("a blank PUT clears the override",
              status == 200 and cleared.get("custom") is False
              and cleared.get("content") == "", cleared)
        check("the file is empty on disk",
              os.path.getsize(prompt_path) == 0, os.path.getsize(prompt_path))
        before = len(MockProvider.records)
        run_turn(agent_port, wid)
        prompt = system_prompt(MockProvider.records[before])
        check("the default prompt comes back", prompt.startswith("You are tea_agent"), prompt[:120])
        check("the old marker is gone", marker2 not in prompt)

        # ---- settings.yaml fallback, then the file wins --------------------
        with open(os.path.join(run_dir, "settings.yaml"), "w", encoding="utf-8") as f:
            f.write("settings:\n  system_prompt: |\n    You are configured in settings.yaml.\n")
        status, info = call(agent_port, "GET", "/api/workspaces/%s/system-prompt" % wid)
        check("settings.yaml is reported as the fallback source",
              status == 200 and info.get("default_source") == "settings.yaml"
              and "configured in settings.yaml" in info.get("default_prompt", ""), info)
        before = len(MockProvider.records)
        run_turn(agent_port, wid)
        check("a turn uses settings.yaml's prompt when the file is empty",
              system_prompt(MockProvider.records[before]).startswith("You are configured in settings.yaml."),
              system_prompt(MockProvider.records[before])[:120])
        call(agent_port, "PUT", "/api/workspaces/%s/system-prompt" % wid, {"content": marker})
        before = len(MockProvider.records)
        run_turn(agent_port, wid)
        check("the workspace file outranks settings.yaml",
              system_prompt(MockProvider.records[before]).startswith(marker))

        # ---- saving an unmodified copy of the default creates no override ----
        _, info = call(agent_port, "GET", "/api/workspaces/%s/system-prompt" % wid)
        status, same = call(agent_port, "PUT", "/api/workspaces/%s/system-prompt" % wid,
                            {"content": info.get("default_prompt", "")})
        check("saving the default text back does not pin an override",
              status == 200 and same.get("custom") is False
              and same.get("content") == "" and same.get("source") == "settings.yaml",
              (status, same.get("custom"), same.get("content"), same.get("source")))
        check("the file is empty after that save",
              os.path.getsize(prompt_path) == 0, os.path.getsize(prompt_path))
        check("the persona in effect is still settings.yaml's",
              same.get("current_prompt", "").startswith("You are configured in settings.yaml."),
              same.get("current_prompt", "")[:80])

        # ---- size cap -------------------------------------------------------
        call(agent_port, "PUT", "/api/workspaces/%s/system-prompt" % wid, {"content": marker})
        status, err = call(agent_port, "PUT", "/api/workspaces/%s/system-prompt" % wid,
                           {"content": "Z" * (65 * 1024)})
        check("PUT refuses a prompt past the cap",
              status == 400 and "limit" in ((err.get("error") or {}).get("message") or ""),
              (status, err))
        check("the rejected prompt did not touch the file",
              marker in open(prompt_path, encoding="utf-8").read())
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
