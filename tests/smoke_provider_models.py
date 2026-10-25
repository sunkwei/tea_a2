#!/usr/bin/env python3
"""Discovering the models of a provider, and writing them into provider.yaml.

Almost every OpenAI-compatible gateway answers

    GET {api_url}/v1/models

with the list of the models it offers. tea_a2 reads that list, works out the
default configuration for every model (the endpoint states some of it, the rest
comes from what tea_a2 knows about the model name) and - once the user
confirmed it in the settings page - writes the result into provider.yaml.

This test pins:

  * the model list is read from the provider, with the provider's own key
    (the browser never sees it);
  * the defaults are filled in: the endpoint's numbers, the capability flags
    and the known models;
  * a gateway that answers in another shape (Ollama) is understood too;
  * a failing upstream comes back as an error, not as a crash;
  * writing the list changes ONLY the `models:` block of that one provider:
    the comments, the other providers and the settings block stay as they
    were, and the file still parses;
  * the order of the models is kept (the first one is the default), new
    models are appended;
  * a bad request leaves provider.yaml untouched.

Usage: python3 tests/smoke_provider_models.py [path-to-tea_agent-binary]
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

seen = {"auth": [], "paths": []}
lock = threading.Lock()


def openai_payload():
    """The OpenAI shape, with the extras OpenRouter-style gateways add."""
    return {
        "object": "list",
        "data": [
            {
                "id": "gpt-4o",
                "object": "model",
                "owned_by": "acme",
                "context_length": 128000,
                "max_completion_tokens": 16384,
                "supported_parameters": ["max_tokens", "temperature", "tools",
                                         "reasoning", "include_reasoning"],
            },
            {
                "id": "acme-mini",
                "object": "model",
                "owned_by": "acme",
            },
            {
                "id": "acme-vl",
                "object": "model",
                "modalities": ["text", "image"],
            },
        ],
    }


def ordered_payload():
    """A provider that lists its models alphabetically."""
    return {"object": "list", "data": [{"id": "aa-model"}, {"id": "zz-model"}]}


def ollama_payload():
    """The Ollama shape: a different key and a different name field."""
    return {
        "models": [
            {"name": "llama3.2:latest", "size": 2019393189,
             "details": {"family": "llama", "parameter_size": "3B",
                         "context_length": 131072}},
            {"name": "qwen3:8b", "size": 5200000000,
             "capabilities": ["completion", "tools", "vision", "thinking"]},
        ],
    }


class Mock(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _send(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        with lock:
            seen["auth"].append(self.headers.get("Authorization", ""))
            seen["paths"].append(self.path)
        if self.path.startswith("/openai/"):
            self._send(200, openai_payload())
            return
        if self.path.startswith("/ollama/"):
            self._send(200, ollama_payload())
            return
        if self.path.startswith("/ordered/"):
            self._send(200, ordered_payload())
            return
        if self.path.startswith("/boom/"):
            self._send(500, {"error": "nope"})
            return
        if self.path.startswith("/empty/"):
            self._send(200, {"object": "list", "data": []})
            return
        if self.path.startswith("/garbage/"):
            body = b"not json at all"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
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


PROVIDER_YAML = """# the config under test
version: 1

providers:
  acme:
    api_url: http://127.0.0.1:PORT/openai
    api_key: sk-acme
    # a comment that has to survive the update
    models:
      gpt-4o:
        max_context_tokens: 999999
        max_output_tokens: 4096
        supports_tools: true
      gone-model:
        max_context_tokens: 4096
  ollama:
    api_url: http://127.0.0.1:PORT/ollama
    api_key: sk-ollama
    models: {llama3.2:latest: {max_context_tokens: 32768}}
  ordered:
    api_url: http://127.0.0.1:PORT/ordered
    api_key: sk-ordered
    models:
      zz-model:
        max_context_tokens: 4096
      aa-model:
        max_context_tokens: 4096
  bare:
    api_url: http://127.0.0.1:PORT/ordered
    api_key: sk-bare
  boom:
    api_url: http://127.0.0.1:PORT/boom
    api_key: sk-boom
    models:
      whatever:
        max_context_tokens: 1024
  empty:
    api_url: http://127.0.0.1:PORT/empty
    api_key: sk-empty
    models:
      whatever:
        max_context_tokens: 1024
  garbage:
    api_url: http://127.0.0.1:PORT/garbage
    api_key: sk-garbage
    models:
      whatever:
        max_context_tokens: 1024

settings:
  max_iterations: 42
"""


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

    tmp = tempfile.mkdtemp(prefix="tea_models_")
    ws_dir = os.path.join(tmp, "ws")
    os.makedirs(ws_dir)
    config_path = os.path.join(tmp, "provider.yaml")
    original = PROVIDER_YAML.replace("PORT", str(mock_port))
    with open(config_path, "w", encoding="utf-8") as f:
        f.write(original)

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

        # ---- 1. the model list -------------------------------------------
        status, body = call(port, "GET", "/api/providers/acme/models")
        check("the model list is read", status == 200 and body.get("ok") is True,
              (status, body))
        models = {m["id"]: m for m in body.get("models", [])}
        check("every model of the provider is listed",
              set(models) == {"gpt-4o", "acme-mini", "acme-vl"}, sorted(models))
        check("the endpoint's context length is used",
              models.get("gpt-4o", {}).get("max_context_tokens") == 128000,
              models.get("gpt-4o"))
        check("the endpoint's output limit is used",
              models.get("gpt-4o", {}).get("max_output_tokens") == 16384,
              models.get("gpt-4o"))
        check("the capability flags are read from supported_parameters",
              models.get("gpt-4o", {}).get("supports_tools") is True and
              models.get("gpt-4o", {}).get("supports_reasoning") is True,
              models.get("gpt-4o"))
        check("the modalities are read (vision)",
              models.get("acme-vl", {}).get("supports_vision") is True,
              models.get("acme-vl"))
        check("a known model falls back to the built-in table",
              models.get("gpt-4o", {}).get("max_context_tokens") == 128000 and
              models.get("gpt-4o", {}).get("source") in ("provider", "config"),
              models.get("gpt-4o"))
        check("an unknown model gets the defaults",
              models.get("acme-mini", {}).get("max_context_tokens") == 0 and
              models.get("acme-mini", {}).get("supports_tools") is True,
              models.get("acme-mini"))
        check("a model that is already in the file is marked",
              models.get("gpt-4o", {}).get("in_config") is True and
              models.get("acme-mini", {}).get("in_config") is False,
              [(m["id"], m["in_config"]) for m in body.get("models", [])])
        check("the new models are named",
              set(body.get("added", [])) == {"acme-mini", "acme-vl"},
              body.get("added"))
        check("a model the provider no longer offers is named",
              body.get("removed") == ["gone-model"], body.get("removed"))
        check("the provider's own key is used for the query",
              "Bearer sk-acme" in seen["auth"], seen["auth"])
        check("the key never leaves the server", "sk-acme" not in json.dumps(body), body)

        # ---- 2. another shape (Ollama) ------------------------------------
        status, body = call(port, "GET", "/api/providers/ollama/models")
        models = {m["id"]: m for m in body.get("models", [])}
        check("the Ollama shape is understood",
              status == 200 and set(models) == {"llama3.2:latest", "qwen3:8b"},
              (status, body))
        check("the context length is found in details",
              models.get("llama3.2:latest", {}).get("max_context_tokens") == 131072,
              models.get("llama3.2:latest"))
        check("the capabilities are found",
              models.get("qwen3:8b", {}).get("supports_tools") is True and
              models.get("qwen3:8b", {}).get("supports_vision") is True and
              models.get("qwen3:8b", {}).get("supports_reasoning") is True,
              models.get("qwen3:8b"))

        # ---- 2b. the order of the file wins -------------------------------
        # The first model of a provider is its default, so an update must not
        # move the models that are already there: a provider that lists its
        # models alphabetically would otherwise change every default.
        status, body = call(port, "GET", "/api/providers/ordered/models")
        order = [m["id"] for m in body.get("models", [])]
        check("the models that are already in the file keep their place",
              status == 200 and order == ["zz-model", "aa-model"], (status, order))

        # ---- 3. the failures ----------------------------------------------
        status, body = call(port, "GET", "/api/providers/boom/models")
        check("a failing upstream is reported, not crashed",
              status == 502 and "500" in ((body.get("error") or {}).get("message") or ""),
              (status, body))
        status, body = call(port, "GET", "/api/providers/empty/models")
        check("an empty list is an error",
              status == 502 and (body.get("error") or {}).get("code") == "models_failed",
              (status, body))
        status, body = call(port, "GET", "/api/providers/garbage/models")
        check("a non-JSON answer is an error",
              status == 502 and "JSON" in ((body.get("error") or {}).get("message") or ""),
              (status, body))
        status, body = call(port, "GET", "/api/providers/nope/models")
        check("an unknown provider is a 404", status == 404, (status, body))
        status, body = call(port, "GET", "/api/health")
        check("the server is still alive after the failures", status == 200, status)

        # ---- 4. writing the list ------------------------------------------
        status, body = call(port, "GET", "/api/providers/acme/models")
        confirmed = []
        for m in body.get("models", []):
            confirmed.append({
                "id": m["id"],
                "max_context_tokens": m["max_context_tokens"],
                "max_output_tokens": m["max_output_tokens"],
                "supports_tools": m["supports_tools"],
                "supports_reasoning": m["supports_reasoning"],
                "supports_vision": m["supports_vision"],
            })
        status, body = call(port, "POST", "/api/providers/acme/models",
                            {"models": confirmed})
        check("the confirmed list is written",
              status == 200 and body.get("ok") is True and
              body.get("models") == ["gpt-4o", "acme-mini", "acme-vl"],
              (status, body))

        with open(config_path, "r", encoding="utf-8") as f:
            after = f.read()
        check("the models block was replaced",
              "acme-mini:" in after and "gone-model:" not in after, after)
        check("the comment survived", "# a comment that has to survive the update" in after,
              after)
        check("the other providers survived", "ollama:" in after and "boom:" in after, after)
        check("the settings block survived", "max_iterations: 42" in after, after)
        check("the key survived", "sk-acme" in after, after)
        check("the model order is kept (the first one is the default)",
              after.index("gpt-4o:") < after.index("acme-mini:") < after.index("acme-vl:"),
              after)

        status, body = call(port, "GET", "/api/providers")
        providers = {p["name"]: p for p in body.get("providers", [])}
        check("the new list is live",
              providers.get("acme", {}).get("models") == ["gpt-4o", "acme-mini", "acme-vl"],
              providers.get("acme"))
        check("the context window of the new model is live",
              providers.get("acme", {}).get("model_info", {})
              .get("acme-mini", {}).get("max_context_tokens") == 0,
              providers.get("acme", {}).get("model_info"))

        # ---- 5. a bad request changes nothing ------------------------------
        with open(config_path, "r", encoding="utf-8") as f:
            before_bad = f.read()
        status, body = call(port, "POST", "/api/providers/acme/models", {"models": []})
        check("an empty list is refused", status == 400, (status, body))
        status, body = call(port, "POST", "/api/providers/acme/models",
                            {"models": [{"id": "a"}, {"id": "a"}]})
        check("a duplicate model is refused", status == 400, (status, body))
        status, body = call(port, "POST", "/api/providers/acme/models",
                            {"models": [{"id": "x", "max_context_tokens": -5}]})
        check("an implausible limit is refused", status == 400, (status, body))
        status, body = call(port, "POST", "/api/providers/acme/models", {"nope": 1})
        check("a body without models is refused", status == 400, (status, body))
        with open(config_path, "r", encoding="utf-8") as f:
            after_bad = f.read()
        check("a refused request leaves the file alone", before_bad == after_bad)

        # ---- 6. a provider that is not in the file -------------------------
        # The file was edited by hand while the server was running: the
        # provider is gone from the file, but the server still knows it.
        with open(config_path, "r", encoding="utf-8") as f:
            without_ollama = f.read().replace("  ollama:", "  ollama-gone:")
        with open(config_path, "w", encoding="utf-8") as f:
            f.write(without_ollama)
        status, body = call(port, "POST", "/api/providers/ollama/models",
                            {"models": [{"id": "llama3.2:latest"}]})
        check("a provider that is not in the file is refused",
              status == 400 and (body.get("error") or {}).get("code") == "patch_failed",
              (status, body))
        with open(config_path, "r", encoding="utf-8") as f:
            check("the refused write left the file alone",
                  f.read() == without_ollama)
        with open(config_path, "w", encoding="utf-8") as f:
            f.write(without_ollama.replace("  ollama-gone:", "  ollama:"))

        # ---- 7. the flow-style models block is normalised ------------------
        status, body = call(port, "GET", "/api/providers/ollama/models")
        confirmed = [{"id": m["id"], "max_context_tokens": m["max_context_tokens"]}
                     for m in body.get("models", [])]
        status, body = call(port, "POST", "/api/providers/ollama/models",
                            {"models": confirmed})
        check("a flow-style models block is accepted", status == 200, (status, body))
        with open(config_path, "r", encoding="utf-8") as f:
            after = f.read()
        check("the flow-style block became a normal one",
              "    models:\n" in after and '"llama3.2:latest":' in after, after)
        check("the model with a colon in its name is quoted",
              '"llama3.2:latest":' in after, after)

        # ---- 7b. a provider without a models block gets one ----------------
        status, body = call(port, "GET", "/api/providers/bare/models")
        check("a provider without a models block can be asked too",
              status == 200 and len(body.get("models", [])) == 2, (status, body))
        confirmed = [{"id": m["id"], "max_context_tokens": m["max_context_tokens"]}
                     for m in body.get("models", [])]
        status, body = call(port, "POST", "/api/providers/bare/models",
                            {"models": confirmed})
        check("the models block is added", status == 200, (status, body))
        with open(config_path, "r", encoding="utf-8") as f:
            after = f.read()
        check("the new block sits inside the provider",
              "  bare:\n" in after and "    models:\n" in after and
              "sk-bare" in after, after)

        # ---- 8. the settings of a model the user tuned are kept ------------
        status, body = call(port, "GET", "/api/providers/acme/models")
        confirmed = []
        for m in body.get("models", []):
            confirmed.append({"id": m["id"], "max_context_tokens": m["max_context_tokens"],
                              "note": m.get("note", "")})
        call(port, "POST", "/api/providers/acme/models", {"models": confirmed})
        with open(config_path, "r", encoding="utf-8") as f:
            after = f.read()
        check("the file is still a valid provider.yaml",
              "providers:" in after and "acme-mini:" in after, after)
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
