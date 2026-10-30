#!/usr/bin/env python3
"""The model configuration: what the settings page writes into provider.yaml.

The settings page has two doors into the `models:` block of a provider:

  * POST /api/providers/{name}/models - "更新模型": the list the provider
    offers right now REPLACES the one in the file;
  * PUT  /api/providers/{name}/models - "模型配置": the user edits the models
    that are already there. A model that is not mentioned stays, a field that
    is not mentioned keeps its value, and `null` clears a value.

This test pins the second one:

  * every knob the page offers is read back (context length, output length,
    the reasoning effort, temperature, top_p, top_k, the penalties, the seed);
  * a write touches ONLY the `models:` block of that one provider: the
    comments, the other providers and the settings block stay as they were;
  * `null` clears a value, and a field the request does not carry keeps the
    value the file had;
  * a model can be added, removed, and made the default one (the first model
    of a provider is its default);
  * a bad request changes nothing;
  * and the values really reach the provider: the request the agent sends
    carries the sampling knobs - and carries nothing at all when they were
    never set, because a gateway that does not know `top_k` answers 400.

Usage: python3 tests/smoke_model_config.py [path-to-tea_agent-binary]
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

# Every request the fake provider received: the test looks at the body.
captured = []
captured_lock = threading.Lock()


class Mock(BaseHTTPRequestHandler):
    """A fake OpenAI-compatible gateway that remembers what it was asked."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            body = {"raw": raw.decode("utf-8", "replace")}
        with captured_lock:
            captured.append(body)
        answer = {
            "id": "x", "object": "chat.completion", "created": 0,
            "model": body.get("model", "m"),
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
        self.reply(answer)

    def do_GET(self):
        self.reply({"object": "list", "data": [{"id": "acme-pro"}]})

    def reply(self, obj):
        data = json.dumps(obj).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


PROVIDER_YAML = """# the config under test
version: 1

providers:
  acme:
    api_url: http://127.0.0.1:PORT/v1
    api_key: sk-acme
    # a comment that has to survive the update
    models:
      acme-pro:
        max_context_tokens: 200000
        max_output_tokens: 32000
        supports_tools: true
        supports_reasoning: true
      acme-mini:
        max_context_tokens: 4096
        temperature: 0.9
  other:
    api_url: http://127.0.0.1:PORT/v1
    api_key: sk-other
    models:
      other-model:
        max_context_tokens: 8192

settings:
  max_tool_output: 4242
"""


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


def read(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def main():
    binary = (sys.argv[1] if len(sys.argv) > 1
              else os.path.join(REPO_ROOT, "build", "bin", "tea_agent"))
    if not os.path.exists(binary):
        print("binary not found:", binary)
        return 1

    mock = ThreadingHTTPServer(("127.0.0.1", 0), Mock)
    mock.daemon_threads = True
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    mock_port = mock.server_address[1]

    tmp = tempfile.mkdtemp(prefix="tea_modelcfg_")
    ws_dir = os.path.join(tmp, "ws")
    os.makedirs(ws_dir)
    config_path = os.path.join(tmp, "provider.yaml")
    with open(config_path, "w", encoding="utf-8") as f:
        f.write(PROVIDER_YAML.replace("PORT", str(mock_port)))

    port = free_port()
    proc = subprocess.Popen([
        binary, "--config", config_path, "--tea-home", os.path.join(tmp, "home"),
        "--host", "127.0.0.1", "--port", str(port), "--no-console",
        "--log-file", os.path.join(tmp, "tea_agent.log"), "--workspace", ws_dir,
    ])

    failures = []
    checks = []

    def check(name, ok, detail=""):
        checks.append(name)
        print(("PASS " if ok else "FAIL ") + name + ((("  [" + str(detail) + "]")) if not ok else ""))
        if not ok:
            failures.append(name)

    try:
        for _ in range(100):
            try:
                if call(port, "GET", "/api/health", timeout=2)[0] == 200:
                    break
            except OSError:
                time.sleep(0.2)

        # ---- 1. the settings page sees every knob -------------------------
        status, body = call(port, "GET", "/api/providers")
        providers = {p["name"]: p for p in body.get("providers", [])}
        info = providers.get("acme", {}).get("model_info", {}).get("acme-pro", {})
        check("the model configuration is readable",
              status == 200 and info.get("max_context_tokens") == 200000 and
              info.get("max_output_tokens") == 32000, (status, info))
        check("the capability flags are readable",
              info.get("supports_tools") is True and
              info.get("supports_reasoning") is True and
              info.get("supports_vision") is False, info)
        check("the reasoning effort is readable",
              info.get("reasoning_effort") == "auto", info)
        check("a sampling knob that was not set says so",
              info.get("has_temperature") is False and
              info.get("has_top_p") is False and
              info.get("has_top_k") is False and
              info.get("has_seed") is False, info)
        check("the reasoning levels are offered",
              body.get("reasoning_efforts") == ["auto", "off", "minimal", "low",
                                                "medium", "high", "xhigh", "max"],
              body.get("reasoning_efforts"))
        mini = providers.get("acme", {}).get("model_info", {}).get("acme-mini", {})
        check("a sampling knob that WAS set is readable",
              mini.get("has_temperature") is True and mini.get("temperature") == 0.9,
              mini)

        # ---- 2. writing the configuration ---------------------------------
        status, body = call(port, "PUT", "/api/providers/acme/models", {"models": [
            {"id": "acme-pro",
             "max_context_tokens": 1048576,
             "max_output_tokens": 65536,
             "supports_tools": True,
             "supports_reasoning": True,
             "supports_vision": True,
             "reasoning_effort": "high",
             "temperature": 0.3,
             "top_p": 0.95,
             "top_k": 40,
             "frequency_penalty": 0.1,
             "presence_penalty": 0.2,
             "seed": 42,
             "note": "主力模型"},
        ]})
        check("the write is accepted", status == 200 and body.get("ok") is True,
              (status, body))
        after = read(config_path)
        check("the context length is in the file",
              "max_context_tokens: 1048576" in after, after)
        check("the output length is in the file",
              "max_output_tokens: 65536" in after, after)
        check("the reasoning effort is in the file",
              "reasoning_effort: high" in after, after)
        check("the temperature is in the file", "temperature: 0.3" in after, after)
        check("top_p is in the file", "top_p: 0.95" in after, after)
        check("top_k is in the file", "top_k: 40" in after, after)
        check("the penalties are in the file",
              "frequency_penalty: 0.1" in after and
              "presence_penalty: 0.2" in after, after)
        check("the seed is in the file", "seed: 42" in after, after)
        check("the note is in the file", "主力模型" in after, after)
        check("only this provider's models block changed",
              "other-model:" in after and "max_context_tokens: 8192" in after, after)
        check("the comment survived", "# a comment that has to survive the update" in after,
              after)
        check("the settings block survived", "max_tool_output: 4242" in after, after)
        check("the key survived", "sk-acme" in after, after)

        # ---- 3. the file is live ------------------------------------------
        status, body = call(port, "GET", "/api/providers")
        providers = {p["name"]: p for p in body.get("providers", [])}
        info = providers.get("acme", {}).get("model_info", {}).get("acme-pro", {})
        check("the new values are live",
              info.get("max_context_tokens") == 1048576 and
              info.get("reasoning_effort") == "high" and
              info.get("has_top_k") is True and info.get("top_k") == 40, info)

        # ---- 4. `null` clears a value -------------------------------------
        status, body = call(port, "PUT", "/api/providers/acme/models", {"models": [
            {"id": "acme-pro", "temperature": None, "top_k": None, "seed": None},
        ]})
        check("clearing is accepted", status == 200, (status, body))
        after = read(config_path)
        check("the cleared temperature is gone",
              "temperature: 0.3" not in after, after)
        check("the cleared top_k is gone", "top_k: 40" not in after, after)
        check("the cleared seed is gone", "seed: 42" not in after, after)
        check("the other values are still there",
              "max_context_tokens: 1048576" in after and
              "reasoning_effort: high" in after, after)

        # ---- 5. a field the request does not carry keeps its value --------
        status, body = call(port, "PUT", "/api/providers/acme/models", {"models": [
            {"id": "acme-pro", "note": "只改备注"},
        ]})
        after = read(config_path)
        check("a partial write keeps the other values",
              "max_context_tokens: 1048576" in after and
              "reasoning_effort: high" in after and "只改备注" in after, after)

        # ---- 6. add, remove, default --------------------------------------
        status, body = call(port, "PUT", "/api/providers/acme/models", {"models": [
            {"id": "acme-new", "max_context_tokens": 65536},
        ]})
        check("a model can be added", status == 200, (status, body))
        status, body = call(port, "GET", "/api/providers")
        providers = {p["name"]: p for p in body.get("providers", [])}
        check("the new model is in the list",
              providers.get("acme", {}).get("models") ==
              ["acme-pro", "acme-mini", "acme-new"],
              providers.get("acme", {}).get("models"))

        status, body = call(port, "PUT", "/api/providers/acme/models", {"models": [
            {"id": "acme-new", "default": True},
        ]})
        check("the default model can be changed", status == 200, (status, body))
        status, body = call(port, "GET", "/api/providers")
        providers = {p["name"]: p for p in body.get("providers", [])}
        check("the default model is the first one",
              providers.get("acme", {}).get("models")[0] == "acme-new",
              providers.get("acme", {}).get("models"))

        status, body = call(port, "PUT", "/api/providers/acme/models", {"models": [
            {"id": "acme-mini", "remove": True},
        ]})
        check("a model can be removed", status == 200, (status, body))
        after = read(config_path)
        check("the removed model is gone from the file",
              "acme-mini:" not in after, after)
        check("the other models are still there",
              "acme-pro:" in after and "acme-new:" in after, after)

        # ---- 7. a bad request changes nothing ------------------------------
        before_bad = read(config_path)
        status, body = call(port, "PUT", "/api/providers/acme/models",
                            {"models": [{"id": "acme-pro", "temperature": 5}]})
        check("a temperature out of range is refused", status == 400, (status, body))
        status, body = call(port, "PUT", "/api/providers/acme/models",
                            {"models": [{"id": "acme-pro", "top_p": 3}]})
        check("a top_p out of range is refused", status == 400, (status, body))
        status, body = call(port, "PUT", "/api/providers/acme/models",
                            {"models": [{"id": "acme-pro", "top_k": -1}]})
        check("a negative top_k is refused", status == 400, (status, body))
        status, body = call(port, "PUT", "/api/providers/acme/models",
                            {"models": [{"id": "a"}, {"id": "a"}]})
        check("a duplicate model is refused", status == 400, (status, body))
        status, body = call(port, "PUT", "/api/providers/acme/models", {"models": []})
        # An empty list means "change nothing" - this is a merge, not a
        # replacement. Removing every model is what "remove" is for.
        check("an empty list changes nothing", status == 200, (status, body))
        status, body = call(port, "PUT", "/api/providers/acme/models",
                            {"models": [{"id": "acme-pro", "temperature": "warm"}]})
        check("a temperature that is not a number is refused", status == 400, (status, body))
        status, body = call(port, "PUT", "/api/providers/nope/models",
                            {"models": [{"id": "x"}]})
        check("an unknown provider is refused", status == 404, (status, body))
        check("a refused request leaves the file alone",
              before_bad == read(config_path))

        # ---- 8. the values reach the provider -----------------------------
        status, body = call(port, "PUT", "/api/providers/acme/models", {"models": [
            {"id": "acme-pro", "max_output_tokens": 65536, "reasoning_effort": "high",
             "temperature": 0.3, "top_p": 0.95, "top_k": 40, "seed": 42,
             "supports_reasoning": True},
            {"id": "acme-new", "max_output_tokens": 4096},
        ]})
        check("the sampling knobs are written", status == 200, (status, body))
        status, body = call(port, "PUT", "/api/providers/acme/models", {"models": [
            {"id": "acme-pro", "default": True},
        ]})
        del captured[:]
        # A chat turn: the request the agent sends is what the gateway sees.
        status, body = call(port, "GET", "/api/workspaces")
        ws = body.get("workspaces", [{}])[0].get("id", "")
        call(port, "POST", "/api/chat",
             {"message": "hello", "workspace_id": ws})
        for _ in range(60):
            time.sleep(0.2)
            with captured_lock:
                if captured:
                    break
        with captured_lock:
            sent = captured[-1] if captured else {}
        check("the turn reached the provider", bool(sent), sent)
        check("the reasoning effort is on the wire",
              sent.get("reasoning_effort") == "high", sent)
        check("the temperature is on the wire", sent.get("temperature") == 0.3, sent)
        check("top_p is on the wire", sent.get("top_p") == 0.95, sent)
        check("top_k is on the wire", sent.get("top_k") == 40, sent)
        check("the seed is on the wire", sent.get("seed") == 42, sent)
        check("the output limit is on the wire",
              sent.get("max_tokens") == 65536, sent)

        # ---- 9. a knob that was never set stays out of the request --------
        # A gateway that does not know `top_k` answers 400 when it sees the
        # field, so an unset knob must not be sent at all.
        status, body = call(port, "PUT", "/api/providers/acme/models", {"models": [
            {"id": "acme-pro", "temperature": None, "top_p": None, "top_k": None,
             "seed": None, "reasoning_effort": "auto"},
        ]})
        check("the knobs are cleared", status == 200, (status, body))
        del captured[:]
        call(port, "POST", "/api/chat",
             {"message": "hello again", "workspace_id": ws})
        for _ in range(60):
            time.sleep(0.2)
            with captured_lock:
                if captured:
                    break
        with captured_lock:
            sent = captured[-1] if captured else {}
        check("an unset temperature is not sent", "temperature" not in sent, sent)
        check("an unset top_p is not sent", "top_p" not in sent, sent)
        check("an unset top_k is not sent", "top_k" not in sent, sent)
        check("an unset seed is not sent", "seed" not in sent, sent)
        check("an 'auto' reasoning effort is not sent",
              "reasoning_effort" not in sent, sent)

        # ---- 10. a model without reasoning support is not asked to think ---
        status, body = call(port, "PUT", "/api/providers/acme/models", {"models": [
            {"id": "acme-pro", "reasoning_effort": "high", "supports_reasoning": False},
        ]})
        del captured[:]
        call(port, "POST", "/api/chat",
             {"message": "hello a third time", "workspace_id": ws})
        for _ in range(60):
            time.sleep(0.2)
            with captured_lock:
                if captured:
                    break
        with captured_lock:
            sent = captured[-1] if captured else {}
        check("a model that cannot think is not asked to",
              "reasoning_effort" not in sent, sent)
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
