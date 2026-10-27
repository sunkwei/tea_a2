#!/usr/bin/env python3
"""Smoke test for speech input (the `asr:` section and the ASR endpoints).

Voice input is a three-part chain, and every link can break on its own:

  browser (records, encodes 16 kHz mono WAV)
      -> POST /api/asr/transcribe        (this server)
      -> POST <asr.url>/v1/audio/transcriptions   (the transcription service)
      -> {"text": "..."}                 (back up the chain)

The transcription service is a separate program (sched/asr_server.py runs
SenseVoiceSmall locally, but any OpenAI-compatible endpoint works), so this
test fakes one and pins the contract between the two halves:

  * without an `asr:` section the endpoints say so instead of guessing;
  * the `asr:` section is read from provider.yaml, and a broken one is a
    warning, not a crash;
  * the audio is forwarded as multipart/form-data with the file and the model
    name - the shape the OpenAI transcription API uses;
  * the answer's `text` field comes back to the caller;
  * a service that is down, refuses, or answers nonsense is a 502 with a
    message that says what went wrong (never a silent empty transcript);
  * a request that is not a WAV file is refused before it reaches the service;
  * the endpoint works on a plain http:// service (no TLS needed on a device
    that only talks to itself).

Usage:  python3 tests/smoke_asr.py [path-to-tea_agent-binary]
"""

import http.client
import http.server
import json
import os
import random
import socket
import struct
import sys
import tempfile
import threading
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


def call(port, method, path, body=None, content_type=None, timeout=30):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    headers = {}
    if content_type:
        headers["Content-Type"] = content_type
    if body is not None:
        headers.setdefault("Content-Type", "application/json")
        if isinstance(body, (dict, list)):
            body = json.dumps(body)
        if isinstance(body, str):
            body = body.encode("utf-8")
    conn.request(method, path, body=body, headers=headers)
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()
    try:
        return resp.status, json.loads(raw or b"null"), raw
    except ValueError:
        return resp.status, {"raw": raw.decode("utf-8", "replace")}, raw


def make_wav(seconds=0.25, rate=16000):
    """A tiny 16 kHz mono 16-bit PCM WAV file."""
    n = int(seconds * rate)
    samples = b"".join(struct.pack("<h", 0) for _ in range(n))
    data = samples
    header = b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE"
    header += b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
    header += b"data" + struct.pack("<I", len(data))
    return header + data


class FakeAsr(http.server.BaseHTTPRequestHandler):
    """A transcription service that records what it was sent."""

    protocol_version = "HTTP/1.1"
    last_request = {}
    reply = {"text": "这是一句测试语音"}
    status = 200
    fail = False

    def log_message(self, fmt, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        type(self).last_request = {
            "path": self.path,
            "content_type": self.headers.get("Content-Type") or "",
            "authorization": self.headers.get("Authorization") or "",
            "body": body,
            "length": length,
        }
        if type(self).fail:
            payload = json.dumps({"error": {"message": "model not loaded"}}).encode("utf-8")
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        payload = json.dumps(type(self).reply).encode("utf-8")
        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def start_fake_asr():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeAsr)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


def main():
    binary = (sys.argv[1] if len(sys.argv) > 1
              else os.path.join(REPO_ROOT, "build", "bin", "tea_agent"))
    if not os.path.exists(binary):
        print("binary not found:", binary)
        return 1

    tmp = tempfile.mkdtemp(prefix="tea_asr_")
    home = os.path.join(tmp, "home")
    workspace = os.path.join(tmp, "ws")
    os.makedirs(home)
    os.makedirs(workspace)

    fake, fake_port = start_fake_asr()
    FakeAsr.reply = {"text": "这是一句测试语音"}
    FakeAsr.status = 200
    FakeAsr.fail = False

    failures = []
    checks = []

    def check(name, ok, detail=""):
        checks.append(name)
        print(("PASS " if ok else "FAIL ") + name + (("  " + str(detail)) if not ok else ""))
        if not ok:
            failures.append(name)

    def write_config(text):
        path = os.path.join(home, "provider.yaml")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    base_provider = """version: 1
providers:
  mock:
    api_url: http://127.0.0.1:9/v1
    api_key: sk-test
    default_model: see
    models:
      see: {}
"""

    # First run: no asr: section at all.
    write_config(base_provider)
    port = free_port()
    import subprocess
    proc = subprocess.Popen([
        binary, "--tea-home", home, "--host", "127.0.0.1", "--port", str(port),
        "--no-console", "--log-file", os.path.join(home, "logs", "tea_agent.log"),
        "--workspace", workspace,
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if not wait_health(port):
            print("server did not become healthy")
            return 1

        # ---- no asr: section ----------------------------------------------
        status, body, _ = call(port, "GET", "/api/asr/status")
        check("without an asr: section the status says it is not configured",
              status == 200 and body.get("configured") is False, (status, body))
        status, body, _ = call(port, "GET", "/api/providers")
        check("the providers answer carries the asr section",
              status == 200 and body.get("asr", {}).get("configured") is False, (status, body))
        status, body, _ = call(port, "POST", "/api/asr/transcribe", make_wav(),
                               content_type="audio/wav")
        check("transcribing without a service is refused with a clear code",
              status == 400 and (body.get("error") or {}).get("code") == "asr_not_configured",
              (status, body))
        check("the refusal explains what to do",
              "asr:" in ((body.get("error") or {}).get("message") or ""), body)

        # ---- an asr: section ----------------------------------------------
        write_config(base_provider + """
asr:
  url: http://127.0.0.1:%d
  model: sense-voice
""" % fake_port)
        status, body, _ = call(port, "POST", "/api/config/reload")
        check("the config reloads", status == 200, (status, body))

        status, body, _ = call(port, "GET", "/api/asr/status")
        check("the asr: section is picked up",
              status == 200 and body.get("configured") is True, (status, body))
        check("the endpoint is the standard transcription path",
              body.get("url", "").endswith("/v1/audio/transcriptions"), body.get("url"))
        check("the model name is reported", body.get("model") == "sense-voice", body)

        status, body, _ = call(port, "GET", "/api/providers")
        check("the providers answer now says asr is configured",
              body.get("asr", {}).get("configured") is True, body.get("asr"))


        # ---- the happy path -----------------------------------------------
        wav = make_wav()

        # ---- the url may be written out in full --------------------------
        # A base url gets the standard path appended; a url that already names
        # the endpoint must be left alone (appending it twice would send the
        # request to a path that does not exist).
        write_config(base_provider + """
asr:
  url: http://127.0.0.1:%d/v1/audio/transcriptions
  model: sense-voice
""" % fake_port)
        call(port, "POST", "/api/config/reload")
        status, body, _ = call(port, "GET", "/api/asr/status")
        check("a url that already names the endpoint is left alone",
              body.get("url") == "http://127.0.0.1:%d/v1/audio/transcriptions" % fake_port,
              body.get("url"))
        FakeAsr.last_request = {}
        status, body, _ = call(port, "POST", "/api/asr/transcribe", wav,
                               content_type="audio/wav")
        check("the audio is transcribed with the explicit endpoint",
              status == 200 and body.get("text") == "这是一句测试语音", (status, body))
        check("the request went to the path that was written",
              FakeAsr.last_request.get("path") == "/v1/audio/transcriptions",
              FakeAsr.last_request.get("path"))

        # Put the plain base url back for the rest of the test.
        write_config(base_provider + """
asr:
  url: http://127.0.0.1:%d
  model: sense-voice
""" % fake_port)
        call(port, "POST", "/api/config/reload")
        FakeAsr.last_request = {}
        status, body, _ = call(port, "POST", "/api/asr/transcribe", wav,
                               content_type="audio/wav")
        check("the audio is transcribed",
              status == 200 and body.get("ok") is True, (status, body))
        check("the text comes back", body.get("text") == "这是一句测试语音", body)
        check("the elapsed time is reported", isinstance(body.get("elapsed_ms"), int), body)

        # What did the service actually receive? The OpenAI transcription API
        # takes multipart/form-data, and the file part has to carry the audio.
        sent = FakeAsr.last_request
        check("the service is called at the transcription path",
              sent.get("path") == "/v1/audio/transcriptions", sent.get("path"))
        check("the request is multipart/form-data",
              (sent.get("content_type") or "").startswith("multipart/form-data; boundary="),
              sent.get("content_type"))
        check("the audio is in the body", wav in sent.get("body", b""),
              len(sent.get("body", b"")))
        check("the model name is sent along",
              b"sense-voice" in sent.get("body", b""), len(sent.get("body", b"")))
        check("no api key is sent when none is configured",
              not sent.get("authorization"), sent.get("authorization"))

        # ---- an api key is sent when one is configured --------------------
        write_config(base_provider + """
asr:
  url: http://127.0.0.1:%d
  api_key: sk-asr-secret
  model: sense-voice
""" % fake_port)
        call(port, "POST", "/api/config/reload")
        FakeAsr.last_request = {}
        status, body, _ = call(port, "POST", "/api/asr/transcribe", wav,
                               content_type="audio/wav")
        check("the audio is transcribed with a key configured", status == 200, (status, body))
        check("the key is sent as a bearer token",
              FakeAsr.last_request.get("authorization") == "Bearer sk-asr-secret",
              FakeAsr.last_request.get("authorization"))

        # ---- bad input is refused before the service is bothered ---------
        FakeAsr.last_request = {}
        status, body, _ = call(port, "POST", "/api/asr/transcribe", b"not a wav file",
                               content_type="audio/wav")
        check("a body that is not a WAV file is refused",
              status == 400 and (body.get("error") or {}).get("code") == "bad_request",
              (status, body))
        check("the refused request never reached the service",
              FakeAsr.last_request == {}, FakeAsr.last_request)

        status, body, _ = call(port, "POST", "/api/asr/transcribe", b"",
                               content_type="audio/wav")
        check("an empty body is refused",
              status == 400 and (body.get("error") or {}).get("code") == "bad_request",
              (status, body))

        # ---- the service is down ------------------------------------------
        write_config(base_provider + """
asr:
  url: http://127.0.0.1:9
  model: sense-voice
""")
        call(port, "POST", "/api/config/reload")
        status, body, _ = call(port, "POST", "/api/asr/transcribe", wav,
                               content_type="audio/wav", timeout=60)
        check("a service that is not there is a 502",
              status == 502 and (body.get("error") or {}).get("code") == "asr_failed",
              (status, body))
        check("the failure says the service did not answer",
              "did not answer" in ((body.get("error") or {}).get("message") or "")
              or "connect" in ((body.get("error") or {}).get("message") or "").lower(),
              body)

        # ---- the service refuses ------------------------------------------
        write_config(base_provider + """
asr:
  url: http://127.0.0.1:%d
  model: sense-voice
""" % fake_port)
        call(port, "POST", "/api/config/reload")
        FakeAsr.fail = True
        status, body, _ = call(port, "POST", "/api/asr/transcribe", wav,
                               content_type="audio/wav")
        check("a refusing service is a 502",
              status == 502 and (body.get("error") or {}).get("code") == "asr_failed",
              (status, body))
        check("the service's own message is passed through",
              "model not loaded" in ((body.get("error") or {}).get("message") or ""), body)
        FakeAsr.fail = False

        # ---- the service answers something else ---------------------------
        FakeAsr.reply = {"unexpected": "shape"}
        status, body, _ = call(port, "POST", "/api/asr/transcribe", wav,
                               content_type="audio/wav")
        check("an answer without a text field is an error, not an empty transcript",
              status == 502 and (body.get("error") or {}).get("code") == "asr_failed",
              (status, body))
        FakeAsr.reply = {"text": "这是一句测试语音"}

        # ---- a broken asr: section is a warning, not a crash --------------
        write_config(base_provider + """
asr:
  model: sense-voice
""")
        status, body, _ = call(port, "POST", "/api/config/reload")
        check("a config without an asr url still loads", status == 200, (status, body))
        status, body, _ = call(port, "GET", "/api/asr/status")
        check("the broken asr: section is not configured",
              body.get("configured") is False, body)
        check("the reason is reported",
              "url" in (body.get("error") or ""), body.get("error"))
        status, body, _ = call(port, "GET", "/api/providers")
        check("the warning is in the providers answer",
              any("asr" in w for w in (body.get("warnings") or [])), body.get("warnings"))

        write_config(base_provider + """
asr:
  url: ftp://127.0.0.1/%d
""" % fake_port)
        call(port, "POST", "/api/config/reload")
        status, body, _ = call(port, "GET", "/api/asr/status")
        check("a non-http asr url is refused",
              body.get("configured") is False, body)

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        fake.shutdown()

    print("\n%d/%d checks passed" % (len(checks) - len(failures), len(checks)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
