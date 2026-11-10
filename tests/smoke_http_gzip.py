#!/usr/bin/env python3
"""The answers may be compressed - but only when the client can take them back.

The tunnel (a2_tunnel) carries these very bytes over a link that is often a
phone or a VPN, so an answer that the client is willing to decompress travels
smaller. What is pinned here:

  * a client that does not ask for gzip gets the plain answer;
  * a client that asks gets a gzip body that unpacks to the very same bytes;
  * the `Content-Length` says how many bytes are really there, and there is
    exactly one of them (two lines with the same name make the browser wait
    for bytes that never come - it then says the data is corrupt);
  * `Vary: Accept-Encoding` says the answer depends on the coding;
  * `gzip;q=0` and `identity` mean "do not compress";
  * a live stream (the SSE frames of a running turn) is compressed too, and
    it stays a stream: the frames arrive as they come.

Usage: python3 tests/smoke_http_gzip.py [path-to-tea_agent-binary]
"""

import gzip
import http.client
import io
import json
import os
import random
import shutil
import socket
import subprocess
import sys
import tempfile
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MOCK_LLM = os.path.join(REPO_ROOT, "tools", "mock_llm_server.mjs")


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


def get(port, path, headers=None, method="GET", timeout=30):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    conn.request(method, path, headers=headers or {})
    resp = conn.getresponse()
    body = resp.read()
    # The raw header list matters: two lines with the same name are a broken
    # answer even when the dictionary looks fine.
    raw = resp.getheaders()
    conn.close()
    return resp.status, dict(raw), body, raw


def unpack(body):
    return gzip.GzipFile(fileobj=io.BytesIO(body)).read()


# The frames that carry the shape of the turn. The deltas are cut differently
# from run to run (the same text arrives in a different number of pieces), so
# they are left out of the comparison.
DELTAS = ("content_delta", "reasoning_delta", "tool_args_delta")


def shape_of(frames):
    return [f.get("type") for f in frames if f.get("type") not in DELTAS]


def frames_of(text):
    out = []
    for line in text.split("\n"):
        line = line.strip()
        if line.startswith("data:"):
            payload = line[5:].strip()
            if payload and payload != "[DONE]":
                try:
                    out.append(json.loads(payload))
                except ValueError:
                    pass
    return out


def main():
    binary = (sys.argv[1] if len(sys.argv) > 1
              else os.path.join(REPO_ROOT, "build", "bin", "tea_agent"))

    failures = []
    checks = []

    def check(name, ok, detail=""):
        checks.append(name)
        print(("PASS " if ok else "FAIL ") + name + (("  " + str(detail)) if not ok else ""))
        if not ok:
            failures.append(name)

    if not os.path.exists(binary):
        print("binary not found:", binary)
        return 1

    tmp = tempfile.mkdtemp(prefix="tea_gzip_")
    home = os.path.join(tmp, "home")
    work = os.path.join(tmp, "ws")
    os.makedirs(work)
    mock_port = free_port()
    port = free_port()
    config = os.path.join(tmp, "provider.yaml")
    with io.open(config, "w", encoding="utf-8") as fh:
        fh.write("""version: 1
providers:
  mock:
    api_url: http://127.0.0.1:%d/v1
    api_key: mock-key-not-used
    default_model: mock-model
    models:
      mock-model:
        max_context_tokens: 128000
        max_output_tokens: 8192
        supports_tools: true
        supports_reasoning: true
""" % mock_port)

    env = dict(os.environ)
    mock = subprocess.Popen(["node", MOCK_LLM, "--port", str(mock_port)], env=env,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    proc = subprocess.Popen([
        binary, "--config", config, "--tea-home", home, "--host", "127.0.0.1",
        "--port", str(port), "--no-console", "--log-file", os.path.join(tmp, "tea.log"),
        "--workspace", work,
    ], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if not wait_health(port):
            print("server did not become healthy")
            return 1

        # ---- 1. the plain answer -------------------------------------------
        status, headers, body, _ = get(port, "/")
        check("GET / answers", status == 200, status)
        check("a client that does not ask gets the plain page",
              "Content-Encoding" not in headers, headers.get("Content-Encoding"))
        plain = body

        # ---- 2. the same answer, compressed --------------------------------
        status, headers, body, raw = get(port, "/", {"Accept-Encoding": "gzip, deflate, br"})
        check("the compressed answer is 200", status == 200, status)
        check("it says it is gzip", headers.get("Content-Encoding") == "gzip",
              headers.get("Content-Encoding"))
        check("it depends on the coding",
              "Accept-Encoding" in headers.get("Vary", ""), headers.get("Vary"))
        lengths = [v for k, v in raw if k.lower() == "content-length"]
        check("there is exactly one Content-Length", len(lengths) == 1, lengths)
        check("the Content-Length is the size of the body",
              lengths and lengths[0] == str(len(body)),
              "%s vs %d" % (lengths, len(body)))
        try:
            unpacked = unpack(body)
            check("the gzip body unpacks", True)
            check("it unpacks to the very same page", unpacked == plain,
                  "%d vs %d bytes" % (len(unpacked), len(plain)))
        except Exception as exc:  # noqa: BLE001
            check("the gzip body unpacks", False, exc)

        compressed_len = len(body)

        # ---- 3. the weights and the wildcards ------------------------------
        for header, want_gzip, why in (
                ("gzip;q=0", False, "a weight of zero means no"),
                ("identity", False, "identity means no"),
                ("gzip;q=0.5", True, "a weight below one is still a yes"),
                ("*", True, "the wildcard covers gzip"),
                ("gzip;q=0, *", False, "the named coding wins over the wildcard"),
                ("br, zstd", False, "a coding we do not have is no coding")):
            status, headers, body, _ = get(port, "/", {"Accept-Encoding": header})
            coded = headers.get("Content-Encoding") == "gzip"
            check("Accept-Encoding: %s => %s" % (header, "gzip" if want_gzip else "plain"),
                  coded == want_gzip, headers.get("Content-Encoding"))
            if not want_gzip:
                check("Accept-Encoding: %s leaves the page alone" % header, body == plain)

        # ---- 4. the head of the answer -------------------------------------
        status, headers, body, raw = get(port, "/", {"Accept-Encoding": "gzip"}, method="HEAD")
        check("HEAD answers 200", status == 200, status)
        check("HEAD has no body", body == b"", len(body))
        check("HEAD says the compressed size",
              headers.get("Content-Length") == str(compressed_len),
              "%s vs %d" % (headers.get("Content-Length"), compressed_len))

        # ---- 5. a json answer ----------------------------------------------
        status, headers, body, _ = get(port, "/api/capabilities", {"Accept-Encoding": "gzip"})
        check("the json answer is compressed", headers.get("Content-Encoding") == "gzip",
              headers.get("Content-Encoding"))
        try:
            payload = json.loads(unpack(body))
            check("the json answer unpacks into json", isinstance(payload, dict),
                  type(payload).__name__)
        except Exception as exc:  # noqa: BLE001
            check("the json answer unpacks into json", False, exc)

        # ---- 6. the live stream --------------------------------------------
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        conn.request("GET", "/api/workspaces")
        workspaces = json.loads(conn.getresponse().read().decode("utf-8"))
        conn.close()
        workspace_id = workspaces["workspaces"][0]["id"]

        def chat(headers):
            body = json.dumps({"workspace_id": workspace_id,
                               "message": "please run the mock tool sequence"})
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=180)
            conn.request("POST", "/api/chat", body=body,
                         headers=dict(headers, **{"Content-Type": "application/json"}))
            resp = conn.getresponse()
            raw = resp.read()
            head = dict(resp.getheaders())
            conn.close()
            return resp.status, head, raw

        status, headers, raw = chat({})
        check("the plain stream answers 200", status == 200, status)
        check("the plain stream is not coded", "Content-Encoding" not in headers,
              headers.get("Content-Encoding"))
        plain_shape = shape_of(frames_of(raw.decode("utf-8", "replace")))

        status, headers, raw = chat({"Accept-Encoding": "gzip"})
        check("the compressed stream answers 200", status == 200, status)
        check("the compressed stream says it is gzip",
              headers.get("Content-Encoding") == "gzip", headers.get("Content-Encoding"))
        check("the compressed stream is still chunked",
              headers.get("Transfer-Encoding") == "chunked", headers.get("Transfer-Encoding"))
        try:
            unpacked = unpack(raw)
            check("the compressed stream unpacks", True)
            shape = shape_of(frames_of(unpacked.decode("utf-8", "replace")))
            # Two runs are two turns: the timings and the cut of the deltas
            # differ, and the second turn finds the note the first one wrote -
            # so the frames are compared one by one, not as a sequence.
            for wanted in ("start", "tool_call", "tool_result", "done"):
                check("the compressed stream carries the %s frame" % wanted,
                      wanted in shape, shape)
            check("the plain stream carried the same frames",
                  all(wanted in plain_shape
                      for wanted in ("start", "tool_call", "tool_result", "done")),
                  plain_shape)
        except Exception as exc:  # noqa: BLE001
            check("the compressed stream unpacks", False, exc)
    finally:
        for running in (proc, mock):
            try:
                running.terminate()
                running.wait(timeout=10)
            except Exception:  # noqa: BLE001
                running.kill()
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    print("%d checks, %d failed" % (len(checks), len(failures)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
