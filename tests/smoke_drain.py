#!/usr/bin/env python3
"""重启不该打断正在跑的对话（优雅退场 / drain）。

    python3 tests/smoke_drain.py [path-to-tea_agent-binary]

`systemctl --user restart a2` 是「上新版本」的动作，而它通常是在**另一个窗口**
里敲的——敲的人往往正在跟这个 agent 说话。以前重启会当场掐死那一轮对话
（回答永远不来，工作白做），今天早上就是这样连着发生了三次。

这里钉住的是：

  * 收到 SIGTERM 之后，**正在跑的那一轮继续跑完**（进程不马上退出）；
  * 那一轮的回答**照常送到**（SSE 流收到结束事件）；
  * 跑完之后进程**自己退出**（systemd 的 restart 才接得上）；
  * 退场期间**新来的对话被明确拒绝**（503 + 说明），而不是假装开始了；
  * 第二个信号**不等了**（想立刻停就按两次）。

用法：python3 tests/smoke_drain.py [a2 二进制]
"""

import json
import os
import signal
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

# How long the fake provider takes to answer. Long enough that the SIGTERM
# lands in the middle of the turn, short enough for a test.
ANSWER_SECONDS = 4.0

CHECKS = []


def check(name, ok, detail=""):
    CHECKS.append((name, bool(ok), detail))
    print("%s %s%s" % ("ok  " if ok else "FAIL", name,
                       "" if ok else "  [%s]" % str(detail)[:300]))


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class MockProvider(BaseHTTPRequestHandler):
    """Answers slowly, so the turn is still running when the signal comes."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        time.sleep(ANSWER_SECONDS)
        payload = [
            {"id": "c1", "object": "chat.completion.chunk",
             "choices": [{"index": 0, "delta": {"content": "DRAIN_OK"},
                          "finish_reason": None}]},
            {"id": "c1", "object": "chat.completion.chunk",
             "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        ]
        body = "".join("data: %s\n\n" % json.dumps(p) for p in payload)
        body += "data: [DONE]\n\n"
        raw = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def get_json(port, path, timeout=10):
    import http.client
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8", "replace")
        try:
            return resp.status, json.loads(raw)
        except ValueError:
            return resp.status, {"raw": raw}
    finally:
        conn.close()


def read_sse(port, body, out):
    """POST /api/chat and collect the SSE frames."""
    import http.client
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    try:
        conn.request("POST", "/api/chat", json.dumps(body),
                     {"Content-Type": "application/json"})
        resp = conn.getresponse()
        out["status"] = resp.status
        while True:
            line = resp.readline()
            if not line:
                break
            out.setdefault("text", "")
            out["text"] += line.decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)
    finally:
        try:
            conn.close()
        except Exception:
            pass


def post_json(port, path, payload, timeout=10):
    import http.client
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        raw = json.dumps(payload).encode("utf-8")
        conn.request("POST", path, raw, {"Content-Type": "application/json"})
        resp = conn.getresponse()
        body = resp.read().decode("utf-8", "replace")
        try:
            parsed = json.loads(body)
        except ValueError:
            parsed = {"raw": body}
        return resp.status, parsed
    finally:
        conn.close()


def main():
    binary = sys.argv[1] if len(sys.argv) > 1 else os.path.join(REPO_ROOT, "build", "bin",
                                                                "tea_agent")
    if not os.path.exists(binary):
        print("binary not found:", binary)
        return 1

    mock = ThreadingHTTPServer(("127.0.0.1", 0), MockProvider)
    mock.daemon_threads = True
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    mock_port = mock.server_address[1]

    tmp = tempfile.mkdtemp(prefix="tea_drain_")
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

    port = free_port()
    proc = subprocess.Popen([
        binary, "--config", config_path, "--tea-home", os.path.join(tmp, "home"),
        "--host", "127.0.0.1", "--port", str(port), "--no-console",
        "--log-file", os.path.join(tmp, "tea_agent.log"), "--workspace", ws_dir,
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    try:
        # wait for the agent
        deadline = time.time() + 20
        up = False
        while time.time() < deadline:
            try:
                import http.client
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
                conn.request("GET", "/api/health")
                conn.getresponse().read()
                conn.close()
                up = True
                break
            except Exception:
                time.sleep(0.2)
        if not up:
            check("the agent starts", False, "it never answered /api/health")
            return 1
        check("the agent starts", True)

        # ---- a turn that takes a while ---------------------------------
        status, listing = get_json(port, "/api/workspaces")
        workspaces = listing.get("workspaces") or []
        if not workspaces:
            check("the workspace is registered", False, json.dumps(listing)[:200])
            return 1
        wid = workspaces[0]["id"]
        out = {}
        thread = threading.Thread(target=read_sse, args=(port, {
            "workspace_id": wid, "message": "hello"}, out))
        thread.daemon = True
        thread.start()
        time.sleep(1.0)

        # ---- the restart signal ----------------------------------------
        proc.send_signal(signal.SIGTERM)
        time.sleep(1.0)
        check("the process is still alive after the signal (it is draining)",
              proc.poll() is None, "exit code %s" % proc.poll())

        # A new turn must be refused with a reason, not silently accepted.
        status, body = post_json(port, "/api/chat", {"workspace_id": wid,
                                                      "message": "second"})
        check("a new turn is refused while draining", status == 503,
              "%s %s" % (status, body))
        check("the refusal says why", "restart" in json.dumps(body).lower(),
              json.dumps(body)[:200])

        # ---- the running turn finishes ---------------------------------
        thread.join(timeout=40)
        text = out.get("text", "")
        check("the running turn gets its answer", "DRAIN_OK" in text,
              (out.get("error") or text[-200:]))
        check("the turn is closed properly", "[DONE]" in text or "done" in text.lower(),
              text[-200:])

        # ---- and then the process goes away ----------------------------
        deadline = time.time() + 20
        while time.time() < deadline and proc.poll() is None:
            time.sleep(0.2)
        check("the process exits after the turn is done", proc.poll() is not None,
              "still running")
        if proc.poll() is not None:
            check("it exits cleanly", proc.returncode == 0, proc.returncode)

        return 0 if all(ok for _, ok, _ in CHECKS) else 1
    finally:
        try:
            if proc.poll() is None:
                proc.kill()
        except Exception:
            pass
        mock.shutdown()


if __name__ == "__main__":
    sys.exit(main())
