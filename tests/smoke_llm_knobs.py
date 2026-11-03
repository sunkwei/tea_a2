#!/usr/bin/env python3
"""The knobs of /api/llm/chat: what the caller asks for is what the provider gets.

A mechanism interface has to offer enough policy combinations: a Firefox
translation plugin wants a cheap model, no thinking and a low temperature - all
three in one request, without touching the configuration of the device it
happens to run on.

What this pins:

  * every knob (reasoning_effort, temperature, top_p, top_k,
    frequency_penalty, presence_penalty, seed, max_tokens) reaches the provider;
  * `reasoning_effort: "off"` is sent even when the model does not declare
    `supports_reasoning` - the caller knows their gateway;
  * `auto` (and null) mean "do not send the field at all";
  * a knob nobody set stays out of the request (a gateway answers 400 for a
    field it does not know);
  * the model's own configuration is the fallback when the caller says nothing;
  * `null` clears a knob - it leaves the request even when the model config
    sets one;
  * a mistyped knob is a 400, not a silent no-op;
  * the response says which knobs were actually used.

Usage: python3 tests/smoke_llm_knobs.py [path-to-tea_agent-binary]
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

SEEN = []


class MockProvider(BaseHTTPRequestHandler):
    """Records what the provider was asked for, and answers."""

    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            body = {}
        SEEN.append(body)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        out = {
            "id": "c", "object": "chat.completion",
            "choices": [{"index": 0, "message": {
                "role": "assistant", "content": "译好了。"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        }
        self.wfile.write(json.dumps(out).encode())


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


def main():
    binary = (sys.argv[1] if len(sys.argv) > 1
              else os.path.join(REPO_ROOT, "build", "bin", "tea_agent"))
    if not os.path.exists(binary):
        print("binary not found:", binary)
        return 1

    del SEEN[:]
    mock = ThreadingHTTPServer(("127.0.0.1", 0), MockProvider)
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    mock_port = mock.server_address[1]

    tmp = tempfile.mkdtemp(prefix="tea_knobs_")
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
    default_model: plain
    models:
      plain:
        max_context_tokens: 32768
        max_output_tokens: 4096
        supports_tools: false
      tuned:
        max_context_tokens: 32768
        max_output_tokens: 8192
        supports_tools: false
        temperature: 0.25
        top_p: 0.8
        top_k: 40
        frequency_penalty: 0.2
        presence_penalty: 0.1
        seed: 7
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

        # ---- 1) every knob reaches the provider ----------------------------
        del SEEN[:]
        status, resp = call(port, "POST", "/api/llm/chat", {
            "prompt": "翻译成中文",
            "model": "plain",
            "reasoning_effort": "off",
            "temperature": 0.2,
            "top_p": 0.9,
            "top_k": 40,
            "frequency_penalty": 0.5,
            "presence_penalty": 0.6,
            "seed": 1234,
            "max_tokens": 512,
        })
        check("the call succeeds", status == 200, (status, resp))
        sent = SEEN[-1] if SEEN else {}
        check("reasoning_effort goes on the wire", sent.get("reasoning_effort") == "off", sent.get("reasoning_effort"))
        check("temperature goes on the wire", sent.get("temperature") == 0.2, sent.get("temperature"))
        check("top_p goes on the wire", sent.get("top_p") == 0.9, sent.get("top_p"))
        check("top_k goes on the wire", sent.get("top_k") == 40, sent.get("top_k"))
        check("frequency_penalty goes on the wire", sent.get("frequency_penalty") == 0.5, sent.get("frequency_penalty"))
        check("presence_penalty goes on the wire", sent.get("presence_penalty") == 0.6, sent.get("presence_penalty"))
        check("seed goes on the wire", sent.get("seed") == 1234, sent.get("seed"))
        check("max_tokens goes on the wire", sent.get("max_tokens") == 512, sent.get("max_tokens"))

        # ---- 2) the response says which knobs were used --------------------
        knobs = resp.get("knobs") or {}
        check("the response reports the knobs", bool(knobs), resp)
        check("the response reports the thinking level",
              knobs.get("reasoning_effort") == "off", knobs)
        check("the response reports that the thinking level was sent",
              knobs.get("reasoning_effort_sent") is True, knobs)
        check("the response reports the temperature", knobs.get("temperature") == 0.2, knobs)

        # ---- 3) 'auto' means: do not send the field -------------------------
        del SEEN[:]
        status, resp = call(port, "POST", "/api/llm/chat", {
            "prompt": "你好", "model": "plain", "reasoning_effort": "auto"})
        sent = SEEN[-1] if SEEN else {}
        check("'auto' keeps the field out of the request",
              "reasoning_effort" not in sent, sent.get("reasoning_effort"))
        check("'auto' is reported as 'auto'",
              (resp.get("knobs") or {}).get("reasoning_effort") == "auto", resp.get("knobs"))

        # ---- 4) a knob nobody set stays out of the request -----------------
        del SEEN[:]
        status, resp = call(port, "POST", "/api/llm/chat", {
            "prompt": "你好", "model": "plain"})
        sent = SEEN[-1] if SEEN else {}
        check("an unset temperature stays out of the request",
              "temperature" not in sent, sent.get("temperature"))
        check("an unset seed stays out of the request", "seed" not in sent, sent.get("seed"))

        # ---- 5) the model's own configuration is the fallback --------------
        del SEEN[:]
        status, resp = call(port, "POST", "/api/llm/chat", {
            "prompt": "你好", "model": "tuned"})
        sent = SEEN[-1] if SEEN else {}
        check("the model's temperature is the fallback", sent.get("temperature") == 0.25, sent.get("temperature"))
        check("the model's top_p is the fallback", sent.get("top_p") == 0.8, sent.get("top_p"))
        check("the model's top_k is the fallback", sent.get("top_k") == 40, sent.get("top_k"))
        check("the model's seed is the fallback", sent.get("seed") == 7, sent.get("seed"))
        check("the model's output limit is the fallback", sent.get("max_tokens") == 8192, sent.get("max_tokens"))

        # ---- 6) null clears a knob -----------------------------------------
        del SEEN[:]
        status, resp = call(port, "POST", "/api/llm/chat", {
            "prompt": "你好", "model": "tuned", "temperature": None, "seed": None})
        sent = SEEN[-1] if SEEN else {}
        check("null clears the temperature", "temperature" not in sent, sent.get("temperature"))
        check("null clears the seed", "seed" not in sent, sent.get("seed"))
        check("the other knobs are untouched", sent.get("top_p") == 0.8, sent.get("top_p"))

        # ---- 7) a mistyped knob is a 400, not a silent no-op ----------------
        status, resp = call(port, "POST", "/api/llm/chat", {
            "prompt": "你好", "model": "plain", "temperature": "很冷"})
        check("a mistyped temperature is refused", status == 400, (status, resp))
        check("the refusal says what is wrong",
              "temperature" in json.dumps(resp, ensure_ascii=False), resp)
        status, resp = call(port, "POST", "/api/llm/chat", {
            "prompt": "你好", "model": "plain", "temperature": 9})
        check("an out-of-range temperature is refused", status == 400, (status, resp))
        status, resp = call(port, "POST", "/api/llm/chat", {
            "prompt": "你好", "model": "plain", "reasoning_effort": 3})
        check("a mistyped reasoning_effort is refused", status == 400, (status, resp))

        # ---- 8) a thinking model can be told not to think -------------------
        # 'off' is sent even though the model does not declare
        # supports_reasoning - the caller knows their gateway.
        del SEEN[:]
        status, resp = call(port, "POST", "/api/llm/chat", {
            "prompt": "你好", "model": "plain", "reasoning_effort": "off"})
        sent = SEEN[-1] if SEEN else {}
        check("'off' is sent even without supports_reasoning",
              sent.get("reasoning_effort") == "off", sent.get("reasoning_effort"))
    except Exception as e:  # noqa: BLE001 - a harness error is a failure too
        print("harness error:", e)
        failures.append("harness")
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            proc.kill()
        mock.shutdown()

    print()
    print("%d checks, %d failed" % (len(checks), len(failures)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
