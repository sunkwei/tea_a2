#!/usr/bin/env python3
"""Provider quota ("usage") queries.

A provider may expose an account endpoint that says how much of the
subscription is left. Today that is MiniMax M Plan:

    GET https://www.minimax.cn/v1/token_plan/remains
    Authorization: Bearer <M Plan subscription key>

    {"model_remains":[{"model_name":"general",
                       "start_time":...,"end_time":...,"remains_time":...,
                       "current_interval_remaining_percent":71,
                       "weekly_start_time":...,"weekly_end_time":...,
                       "weekly_remains_time":...,
                       "current_weekly_remaining_percent":97}], ...}

The endpoint is not OpenAI-standard, so the provider *declares* it
(`supports_usage: true` + `usage_type: minimax_token_plan` in provider.yaml)
instead of tea_a2 probing it. This test pins:

  * the declaration is reported by /api/providers, per provider;
  * an unknown usage_type is refused with a warning, not silently ignored;
  * the query carries the provider's own key (the browser never sees it);
  * the answer is normalised: 5-hour window + weekly window, percent + reset;
  * a provider without the declaration is refused with a clear message;
  * a failing upstream (500) and a key without a subscription (empty list)
    both come back as an error, not as a crash.

Usage: python3 tests/smoke_provider_usage.py [path-to-tea_agent-binary]
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

NOW = 1792561091281          # the window start the mock reports (ms)
WINDOW_MS = 5 * 3600 * 1000  # ... 5 hours
WEEK_MS = 7 * 24 * 3600 * 1000

seen = {"auth": [], "paths": [], "headers": {}}
lock = threading.Lock()


def quota_payload(percent=71, weekly_percent=97):
    return {
        "model_remains": [
            {
                "start_time": NOW,
                "end_time": NOW + WINDOW_MS,
                "remains_time": 9_700_000,
                "current_interval_total_count": 0,
                "current_interval_usage_count": 0,
                "model_name": "general",
                "current_weekly_total_count": 0,
                "current_weekly_usage_count": 0,
                "weekly_start_time": NOW,
                "weekly_end_time": NOW + WEEK_MS,
                "weekly_remains_time": 596_000_000,
                "current_interval_remaining_percent": percent,
                "current_weekly_remaining_percent": weekly_percent,
                "current_interval_status": 1,
                "current_weekly_status": 1,
            }
        ],
        "base_resp": {"status_code": 0, "status_msg": "success"},
    }


def generic_payload():
    """A shape tea_a2 does not know: the config maps the paths itself."""
    return {
        "remaining": 42,
        "data": {"quota": {"remainingPercent": 42, "used": 58, "total": 100,
                           "resetAt": NOW + 3600 * 1000, "name": "5h"}},
    }


class Mock(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_GET(self):  # noqa: N802
        with lock:
            seen["auth"].append(self.headers.get("Authorization", ""))
            seen["paths"].append(self.path)
            seen["headers"][self.path] = {
                "authorization": self.headers.get("Authorization", ""),
                "api_key": self.headers.get("api-key", ""),
                "cookie": self.headers.get("Cookie", ""),
            }
        if self.path.startswith("/usedtotal/"):
            payload = json.dumps({"quota": {"used": 30, "total": 120}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if self.path.startswith("/generic/") or self.path.startswith("/apikey/") \
                or self.path.startswith("/cookie/"):
            payload = json.dumps(generic_payload()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if self.path.startswith("/quota/"):
            payload = json.dumps(quota_payload()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if self.path.startswith("/nosub/"):
            payload = json.dumps({"model_remains": [],
                                  "base_resp": {"status_code": 0, "status_msg": "success"}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if self.path.startswith("/boom/"):
            payload = json.dumps({"error": "nope"}).encode()
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
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

    tmp = tempfile.mkdtemp(prefix="tea_quota_")
    ws_dir = os.path.join(tmp, "ws")
    os.makedirs(ws_dir)
    config_path = os.path.join(tmp, "provider.yaml")
    with open(config_path, "w", encoding="utf-8") as f:
        f.write("""version: 1
providers:
  mini:
    api_url: http://127.0.0.1:PORT/chat/v1
    api_key: sk-quota-test
    supports_usage: true
    usage_type: minimax_token_plan
    usage_url: http://127.0.0.1:PORT/quota/v1/token_plan/remains
    models: {m: {max_context_tokens: 32768, max_output_tokens: 256}}
  plain:
    api_url: http://127.0.0.1:PORT/chat/v1
    api_key: sk-plain
    models: {m: {max_context_tokens: 32768, max_output_tokens: 256}}
  nosub:
    api_url: http://127.0.0.1:PORT/chat/v1
    api_key: sk-nosub
    supports_usage: true
    usage_type: minimax_token_plan
    usage_url: http://127.0.0.1:PORT/nosub/v1/token_plan/remains
    models: {m: {max_context_tokens: 32768, max_output_tokens: 256}}
  boom:
    api_url: http://127.0.0.1:PORT/chat/v1
    api_key: sk-boom
    supports_usage: true
    usage_type: minimax_token_plan
    usage_url: http://127.0.0.1:PORT/boom/v1/token_plan/remains
    models: {m: {max_context_tokens: 32768, max_output_tokens: 256}}
  weird:
    api_url: http://127.0.0.1:PORT/chat/v1
    api_key: sk-weird
    supports_usage: true
    usage_type: not_a_known_type
    models: {m: {max_context_tokens: 32768, max_output_tokens: 256}}
  generic:
    api_url: http://127.0.0.1:PORT/chat/v1
    api_key: sk-generic
    supports_usage: true
    usage_type: json_fields
    usage_url: http://127.0.0.1:PORT/generic/quota
    usage_fields:
      remaining_percent: data.quota.remainingPercent
      used: data.quota.used
      total: data.quota.total
      reset_at_ms: data.quota.resetAt
      label: data.quota.name
    models: {m: {max_context_tokens: 32768, max_output_tokens: 256}}
  apikey:
    api_url: http://127.0.0.1:PORT/chat/v1
    api_key: sk-apikey
    supports_usage: true
    usage_type: json_fields
    usage_auth: api-key
    usage_url: http://127.0.0.1:PORT/apikey/quota
    usage_fields:
      remaining_percent: remaining
    models: {m: {max_context_tokens: 32768, max_output_tokens: 256}}
  linky:
    api_url: http://127.0.0.1:PORT/chat/v1
    api_key: sk-linky
    usage_console_url: https://example.com/console/usage
    models: {m: {max_context_tokens: 32768, max_output_tokens: 256}}
  badlink:
    api_url: http://127.0.0.1:PORT/chat/v1
    api_key: sk-badlink
    usage_console_url: javascript:alert(1)
    models: {m: {max_context_tokens: 32768, max_output_tokens: 256}}
  usedtotal:
    api_url: http://127.0.0.1:PORT/chat/v1
    api_key: sk-usedtotal
    supports_usage: true
    usage_type: json_fields
    usage_url: http://127.0.0.1:PORT/usedtotal/quota
    usage_fields:
      used: quota.used
      total: quota.total
    models: {m: {max_context_tokens: 32768, max_output_tokens: 256}}
  cookie:
    api_url: http://127.0.0.1:PORT/chat/v1
    api_key: sk-cookie
    supports_usage: true
    usage_type: json_fields
    usage_auth: none
    usage_url: http://127.0.0.1:PORT/cookie/quota
    usage_headers:
      Cookie: session=abc123
    usage_fields:
      remaining_percent: remaining
    models: {m: {max_context_tokens: 32768, max_output_tokens: 256}}
""".replace("PORT", str(mock_port)))

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

        # ---- 1. the declaration is visible ---------------------------------
        status, body = call(port, "GET", "/api/providers")
        plist = body
        providers = {p["name"]: p for p in body.get("providers", [])}
        check("the provider list carries the usage capability",
              providers.get("mini", {}).get("supports_usage") is True and
              providers.get("mini", {}).get("usage_type") == "minimax_token_plan",
              providers.get("mini"))
        check("a provider without the declaration says so",
              providers.get("plain", {}).get("supports_usage") is False,
              providers.get("plain"))
        check("an unknown usage_type is refused with a warning",
              providers.get("weird", {}).get("supports_usage") is False and
              any("not_a_known_type" in w for w in body.get("warnings", [])),
              body.get("warnings"))

        # ---- 2. the query --------------------------------------------------
        status, body = call(port, "GET", "/api/providers/mini/usage")
        check("the quota query succeeds", status == 200 and body.get("ok") is True,
              (status, body))
        window = body.get("window") or {}
        check("the 5-hour window is normalised",
              window.get("present") is True and window.get("name") == "5h" and
              window.get("remaining_percent") == 71 and
              window.get("end_ms") == NOW + WINDOW_MS and window.get("remains_ms") == 9_700_000,
              window)
        weekly = body.get("weekly") or {}
        check("the weekly window is normalised",
              weekly.get("present") is True and weekly.get("remaining_percent") == 97 and
              weekly.get("end_ms") == NOW + WEEK_MS,
              weekly)
        check("the provider's own key is used for the query",
              "Bearer sk-quota-test" in seen["auth"], seen["auth"])
        check("the key never leaves the server", "sk-quota-test" not in json.dumps(body),
              body)

        # ---- 3. the refusals -----------------------------------------------
        status, body = call(port, "GET", "/api/providers/plain/usage")
        check("a provider without the declaration is refused",
              status == 400 and (body.get("error") or {}).get("code") == "usage_not_supported",
              (status, body))
        status, body = call(port, "GET", "/api/providers/nope/usage")
        check("an unknown provider is a 404", status == 404, (status, body))

        # ---- 4. the upstream failures --------------------------------------
        status, body = call(port, "GET", "/api/providers/nosub/usage")
        check("a key without a subscription says so",
              status == 502 and "订阅" in ((body.get("error") or {}).get("message") or ""),
              (status, body))
        status, body = call(port, "GET", "/api/providers/boom/usage")
        check("a failing upstream is reported, not crashed",
              status == 502 and "500" in ((body.get("error") or {}).get("message") or ""),
              (status, body))
        status, body = call(port, "GET", "/api/health")
        check("the server is still alive after the failures", status == 200, status)

        # ---- 4b. a provider that only offers a console link ----------------
        check("the console link is reported",
              providers.get("linky", {}).get("usage_console_url") ==
              "https://example.com/console/usage",
              providers.get("linky"))
        check("a console link does not pretend there is an API",
              providers.get("linky", {}).get("supports_usage") is False,
              providers.get("linky"))
        status, body = call(port, "GET", "/api/providers/linky/usage")
        check("querying a provider without an API is still refused",
              status == 400 and (body.get("error") or {}).get("code") == "usage_not_supported",
              (status, body))
        check("a non-http console link is dropped with a warning",
              not providers.get("badlink", {}).get("usage_console_url") and
              any("usage_console_url" in w for w in plist.get("warnings", [])),
              (providers.get("badlink"), plist.get("warnings")))

        # ---- 5. a provider whose JSON shape we do not know -----------------
        status, body = call(port, "GET", "/api/providers/generic/usage")
        window = body.get("window") or {}
        check("json_fields reads the configured paths",
              status == 200 and window.get("remaining_percent") == 42 and
              window.get("end_ms") == NOW + 3600 * 1000 and window.get("name") == "5h",
              (status, body))
        status, body = call(port, "GET", "/api/providers/usedtotal/usage")
        window = body.get("window") or {}
        check("json_fields computes the percentage from used/total",
              status == 200 and abs(float(window.get("remaining_percent", -1)) - 75.0) < 0.01,
              (status, body))

        # ---- 6. the key can be presented in another header -----------------
        status, body = call(port, "GET", "/api/providers/apikey/usage")
        seen_apikey = seen["headers"].get("/apikey/quota", {})
        check("usage_auth: api-key puts the key in the api-key header",
              seen_apikey.get("api_key") == "sk-apikey", seen_apikey)
        check("usage_auth: api-key sends no Authorization header",
              not seen_apikey.get("authorization"), seen_apikey)

        # ---- 7. an endpoint behind a session cookie ------------------------
        status, body = call(port, "GET", "/api/providers/cookie/usage")
        seen_cookie = seen["headers"].get("/cookie/quota", {})
        check("usage_headers carries the cookie",
              seen_cookie.get("cookie") == "session=abc123", seen_cookie)
        check("usage_auth: none sends no key at all",
              not seen_cookie.get("authorization") and not seen_cookie.get("api_key"),
              seen_cookie)
        check("the cookie endpoint still parses",
              status == 200 and (body.get("window") or {}).get("remaining_percent") == 42,
              (status, body))
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
