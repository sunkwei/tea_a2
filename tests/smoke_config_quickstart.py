#!/usr/bin/env python3
"""Smoke test for the first-run quick setup (one API key -> provider.yaml).

A fresh install has no provider.yaml, and typing a full YAML file on a phone or
a headless board is painful. `POST /api/config/quickstart` builds the file from an
API key alone (DeepSeek, default model deepseek-flash) and applies it with the
same validate-then-swap path as an upload.

What this pins:

  * without a config, /api/health says config_loaded=false and setup is needed;
  * a POST with just `api_key` creates provider.yaml with that key, DeepSeek as
    the only provider, deepseek-flash as the default model and a `default:` block
    (never the legacy `roles:`);
  * the config is applied immediately: /api/health and /api/providers see it
    without a restart, and the model list has the documented alternatives;
  * the API key is never echoed back in any response;
  * the default is the first provider's first model (reordering decides;
    `default_model:` does not);
  * the file is owner-only on POSIX (it holds a secret);
  * a key containing quotes/backslashes survives the YAML round trip (it is a
    single-quoted scalar, so it cannot inject mapping keys);
  * bad input is refused and nothing is written: missing/blank key, control
    character in the key, unsupported provider;
  * a second call replaces the config (this is not a one-shot).
"""

import http.client
import json
import os
import random
import socket
import stat
import subprocess
import sys
import tempfile
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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


def call(port, method, path, body=None, timeout=30):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    headers = {"Content-Type": "application/json"} if body is not None else {}
    conn.request(method, path, body=json.dumps(body) if body is not None else None,
                 headers=headers)
    resp = conn.getresponse()
    raw = resp.read()
    try:
        return resp.status, json.loads(raw or b"null"), raw.decode("utf-8", "replace")
    except ValueError:
        return resp.status, {"raw": raw.decode("utf-8", "replace")}, raw.decode("utf-8", "replace")


def main():
    binary = (sys.argv[1] if len(sys.argv) > 1
              else os.path.join(REPO_ROOT, "build", "bin", "tea_agent"))
    if not os.path.exists(binary):
        print("binary not found:", binary)
        return 1

    tmp = tempfile.mkdtemp(prefix="tea_quickstart_")
    port = free_port()
    home = os.path.join(tmp, "home")
    os.makedirs(home)
    config_path = os.path.join(home, "provider.yaml")

    proc = subprocess.Popen([
        binary, "--tea-home", home, "--host", "127.0.0.1", "--port", str(port),
        "--no-console", "--log-file", os.path.join(home, "logs", "tea_agent.log"),
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

        # ---- the starting point -------------------------------------------
        _, health, _ = call(port, "GET", "/api/health")
        check("a fresh install has no config", health.get("config_loaded") is False, health)
        check("the tea home is reported for the setup gate",
              health.get("tea_home") == home, health.get("tea_home"))
        _, cfg, _ = call(port, "GET", "/api/config/status")
        check("setup is needed", cfg.get("needs_setup") is True, cfg)
        check("nothing was written yet", not os.path.exists(config_path), config_path)

        # ---- bad input changes nothing ------------------------------------
        status, err, _ = call(port, "POST", "/api/config/quickstart", {})
        check("a missing key is refused",
              status == 400 and (err.get("error") or {}).get("code") == "missing_key", (status, err))
        status, err, _ = call(port, "POST", "/api/config/quickstart", {"api_key": "   "})
        check("a blank key is refused",
              status == 400 and (err.get("error") or {}).get("code") == "missing_key", (status, err))
        status, err, _ = call(port, "POST", "/api/config/quickstart",
                              {"provider": "openai", "api_key": "sk-x"})
        check("an unsupported provider is refused",
              status == 400
              and (err.get("error") or {}).get("code") == "unsupported_provider", (status, err))
        status, err, _ = call(port, "POST", "/api/config/quickstart",
                              {"api_key": "sk-abc\ndef"})
        check("a key with a control character is refused",
              status == 400 and (err.get("error") or {}).get("code") == "bad_key", (status, err))
        check("none of the refusals wrote a file", not os.path.exists(config_path), config_path)

        # ---- the one-key path ---------------------------------------------
        key = "sk-quickstart-1234567890"
        status, done, body_text = call(port, "POST", "/api/config/quickstart", {"api_key": key})
        check("quickstart succeeds", status == 200 and done.get("ok") is True, (status, done))
        check("it reports the generated provider", done.get("provider") == "DeepSeek", done)
        check("the default model is deepseek-flash", done.get("model") == "deepseek-flash", done)
        check("the alternatives are offered too",
              set(done.get("models") or []) == {"deepseek-flash", "deepseek-chat",
                                                "deepseek-reasoner"}, done.get("models"))
        check("provider.yaml exists", os.path.isfile(config_path), config_path)
        text = open(config_path, encoding="utf-8").read()
        check("the key is in the file", key in text, text[:200])
        check("the provider is DeepSeek", "DeepSeek:" in text and "https://api.deepseek.com" in text,
              text[:300])
        # The first model in the list is the default; there is no default_model:
        # key to disagree with it.
        check("deepseek-flash is the first model in the file",
              text.index("models:") < text.index("deepseek-flash:"), text)
        # Line-anchored: the trailing comment may mention the key by name.
        check("the file needs no default_model: key",
              not any(line.strip().startswith("default_model:") for line in text.splitlines()),
              text)
        check("DeepSeek is the first provider, so it is the default",
              text.index("providers:") < text.index("DeepSeek:"), text[:400])
        check("the generated file uses neither roles: nor default:",
              "roles:" not in text and "\ndefault:" not in text, text[-200:])
        check("the secret is not echoed back by the API", key not in body_text,
              body_text[:200])
        if os.name == "posix":
            mode = stat.S_IMODE(os.stat(config_path).st_mode)
            check("the file is owner-only", mode == 0o600, oct(mode))

        # ---- the default is the first model of the first provider ----------
        # `default_model:` is deliberately NOT the switch: it used to disagree
        # with the visible order, which made "why is it using that model?"
        # unanswerable. Reordering the models list must move the default.
        os.makedirs(os.path.join(tmp, "order_ws"), exist_ok=True)
        order_cfg = os.path.join(tmp, "order.yaml")
        with open(order_cfg, "w", encoding="utf-8") as f:
            f.write('''version: 1
providers:
  first:
    api_url: http://127.0.0.1:9/v1
    api_key: sk-a
    default_model: second-model
    models:
      first-model: {}
      second-model: {}
  later:
    api_url: http://127.0.0.1:9/v1
    api_key: sk-b
    models:
      later-model: {}
''')
        order_port = free_port()
        order_proc = subprocess.Popen([
            binary, "--config", order_cfg, "--tea-home", os.path.join(tmp, "order_home"),
            "--host", "127.0.0.1", "--port", str(order_port), "--no-console",
            "--log-file", os.path.join(tmp, "order.log"),
            "--workspace", os.path.join(tmp, "order_ws"),
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            check("the ordering fixture comes up", wait_health(order_port), order_port)
            _, order_health, _ = call(order_port, "GET", "/api/health")
            check("the first provider wins over later ones",
                  order_health.get("provider") == "first", order_health.get("provider"))
            check("the first model wins over default_model",
                  order_health.get("model") == "first-model", order_health.get("model"))
        finally:
            try:
                order_proc.terminate()
                order_proc.wait(timeout=10)
            except Exception:
                order_proc.kill()

        # ---- applied immediately ------------------------------------------
        _, health, _ = call(port, "GET", "/api/health")
        check("the config is live without a restart",
              health.get("config_loaded") is True and health.get("provider") == "DeepSeek"
              and health.get("model") == "deepseek-flash", health)
        _, cfg, _ = call(port, "GET", "/api/config/status")
        check("setup mode is over", cfg.get("loaded") is True, cfg)
        _, prov, _ = call(port, "GET", "/api/providers")
        names = [p.get("name") for p in prov.get("providers") or []]
        check("the provider list has exactly DeepSeek", names == ["DeepSeek"], names)
        models = (prov.get("providers") or [{}])[0].get("models") or []
        check("the model catalogue is exposed", "deepseek-flash" in models, models)
        info = (prov.get("providers") or [{}])[0].get("model_info") or {}
        check("the model carries a context window",
              (info.get("deepseek-flash") or {}).get("max_context_tokens", 0) > 0, info)
        check("the API key is never returned by /api/providers", key not in json.dumps(prov),
              "leaked")

        # ---- an awkward key survives the YAML round trip -------------------
        tricky = "sk-a'b\\c\"d"
        status, done, _ = call(port, "POST", "/api/config/quickstart", {"api_key": tricky})
        check("a key with quotes/backslashes is accepted", status == 200 and done.get("ok"), done)
        text = open(config_path, encoding="utf-8").read()
        check("the key is emitted as a single-quoted scalar",
              "api_key: 'sk-a''b\\c\"d'" in text, text)
        _, health, _ = call(port, "GET", "/api/health")
        check("the awkward key still parses into a working config",
              health.get("config_loaded") is True, health)
        check("the file is still owner-only",
              os.name != "posix" or stat.S_IMODE(os.stat(config_path).st_mode) == 0o600,
              oct(stat.S_IMODE(os.stat(config_path).st_mode)))

        # ---- it replaces, it is not one-shot ------------------------------
        status, done, _ = call(port, "POST", "/api/config/quickstart",
                               {"api_key": "sk-second", "api_url": "https://example.test/v1"})
        check("a second call replaces the config", status == 200, (status, done))
        text = open(config_path, encoding="utf-8").read()
        check("the new key and url are in the file",
              "sk-second" in text and "https://example.test/v1" in text, text[:300])
        check("the old key is gone", "sk-quickstart-1234567890" not in text, text[:300])
        _, health, _ = call(port, "GET", "/api/health")
        check("the replacement is live", health.get("config_loaded") is True, health)
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
