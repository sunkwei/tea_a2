#!/usr/bin/env python3
"""The queue's event stream must not go silent - and the bots must survive it.

Two things this pins down (both found on the VPS on 2026-11-24):

  1. **The event stream sends a keepalive.** While nothing happens the
     stream said nothing at all, so the **client's** read timeout (an hour,
     by default) fired first: the watcher on the other end logged
     "task watcher: timed out" every single hour and reconnected. A comment
     line (": keepalive") is invisible to the SSE parsers - they only read
     the "data:" ones - and keeps the connection alive through the proxies.

  2. **A read that times out is a ChannelError, not a crash.** The Telegram
     long poll reads the answer *after* the request succeeded, so urllib
     does not wrap a read timeout in URLError: the raw TimeoutError escaped
     the bot's run loop (which only knows ChannelError), the process died and
     systemd started it again - the log showed the bot restarting every few
     minutes for a whole afternoon.

Usage: python3 tests/smoke_sse_keepalive.py
"""

import http.client
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler

try:
    # Python 3.7+
    from http.server import ThreadingHTTPServer
except ImportError:  # Python 3.6
    from http.server import HTTPServer
    from socketserver import ThreadingMixIn

    class ThreadingHTTPServer(HTTPServer, ThreadingMixIn):
        """The 3.6 spelling of http.server.ThreadingHTTPServer."""
        daemon_threads = True

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(REPO_ROOT, "sched", "task_server.py")
sys.path.insert(0, os.path.join(REPO_ROOT, "sched"))

import channel  # noqa: E402
import telegram_bot  # noqa: E402
from a2_ext_py import Queue  # noqa: E402


def free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockaddr()[1] if False else sock.getsockname()[1]
    sock.close()
    return port


class MockAgent(BaseHTTPRequestHandler):
    """Just enough agent for the queue: it wants to know it is alive."""

    def do_GET(self):  # noqa: N802 - the name is the stdlib's
        body = json.dumps({"ok": True, "version": "1.8.3",
                           "workspaces": [{"id": "ws_1", "path": "/tmp"}]}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def wait_health(port, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            conn.request("GET", "/api/health")
            conn.getresponse().read()
            conn.close()
            return True
        except Exception:  # noqa: BLE001
            time.sleep(0.2)
    return False


def main():
    failures = []
    checks = []

    def check(name, ok, detail=""):
        checks.append(name)
        print(("PASS " if ok else "FAIL ") + name + (("  " + str(detail)) if not ok else ""))
        if not ok:
            failures.append(name)

    tmp = tempfile.mkdtemp(prefix="tea_sse_")
    port = free_port()
    agent = ThreadingHTTPServer(("127.0.0.1", 0), MockAgent)
    agent.daemon_threads = True
    threading.Thread(target=agent.serve_forever, daemon=True).start()
    agent_port = agent.server_address[1]
    log_path = os.path.join(tmp, "queue.log")
    with open(log_path, "w", encoding="utf-8") as log:
        server = subprocess.Popen([
            sys.executable, SERVER, "--agent", "http://127.0.0.1:%d" % agent_port,
            "--host", "127.0.0.1", "--port", str(port),
            "--data-dir", os.path.join(tmp, "tasks"),
            "--workspace-path", os.path.join(tmp, "task-workspace"),
            "--interval", "2",
            # a keepalive every second: the test must not wait a whole minute
            "--sse-keepalive", "1",
        ], stdout=log, stderr=subprocess.STDOUT, universal_newlines=True)

    try:
        if not wait_health(port):
            print("the queue did not become healthy")
            try:
                with open(log_path, encoding="utf-8") as handle:
                    print(handle.read()[-2000:])
            except OSError:
                pass
            return 1

        # ---- 1. the server sends a keepalive ------------------------------
        comments = []
        frames = []
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("GET", "/api/events")
        resp = conn.getresponse()
        deadline = time.time() + 8
        try:
            while time.time() < deadline:
                line = resp.fp.readline()
                if not line:
                    break
                text = line.decode("utf-8", "replace").strip()
                if text.startswith(":"):
                    comments.append(text)
                elif text.startswith("data:"):
                    frames.append(text)
                if comments:
                    break
        finally:
            conn.close()
        check("the event stream sends a keepalive comment", bool(comments), comments[:2])
        check("the keepalive is not a data frame", not frames, frames[:2])

        # ---- 2. the client ignores the comment ----------------------------
        # The client's read timeout is 4 s: without the keepalive the read
        # blocks for the whole 4 s and then raises - which is exactly what
        # the watcher on the other end was doing, once an hour.
        queue = Queue("http://127.0.0.1:%d" % port)
        state = {"frames": [], "error": ""}

        def listen():
            try:
                for frame in queue.events(timeout=4):
                    state["frames"].append(frame)
            except Exception as exc:  # noqa: BLE001
                state["error"] = "%s: %s" % (type(exc).__name__, exc)

        watcher = threading.Thread(target=listen, daemon=True)
        watcher.start()
        time.sleep(6)
        check("the client does not time out while the stream is kept alive",
              not state["error"], state["error"])
        check("the client does not read the keepalive as an event",
              not state["frames"], state["frames"][:2])

        # ---- 3. a read timeout is a ChannelError --------------------------
        api = telegram_bot.Telegram("123456:ABCDEF", "http://127.0.0.1:1")
        original = urllib.request.urlopen

        def timeout_urlopen(*args, **kwargs):
            raise socket.timeout("the read operation timed out")

        urllib.request.urlopen = timeout_urlopen
        try:
            api.call("getMe")
            check("a read timeout becomes a ChannelError", False, "no error at all")
        except channel.ChannelError as exc:
            check("a read timeout becomes a ChannelError", True, str(exc))
        except Exception as exc:  # noqa: BLE001
            check("a read timeout becomes a ChannelError", False,
                  "%s: %s" % (type(exc).__name__, exc))
        finally:
            urllib.request.urlopen = original

        # ---- 4. a reset connection is an ApiError --------------------------
        reset = Queue("http://127.0.0.1:%d" % free_port())
        died = ""
        try:
            reset.health()
        except Exception as exc:  # noqa: BLE001
            died = "%s: %s" % (type(exc).__name__, exc)
        check("a reset connection is an ApiError, not a crash",
              died.startswith("ApiError"), died)

        # a token that Telegram rejects is still an exit (not a retry loop)
        def unauthorized(*args, **kwargs):
            raise urllib.error.HTTPError("http://x", 401, "Unauthorized", None,
                                        io.BytesIO(b'{"error":"unauthorized"}'))

        urllib.request.urlopen = unauthorized
        try:
            api.call("getMe")
            check("a rejected token is a ChannelError with a status", False, "no error")
        except channel.ChannelError as exc:
            check("a rejected token is a ChannelError with a status",
                  getattr(exc, "status", 0) == 401, str(exc))
        except Exception as exc:  # noqa: BLE001
            check("a rejected token is a ChannelError with a status", False,
                  "%s: %s" % (type(exc).__name__, exc))
        finally:
            urllib.request.urlopen = original
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except Exception:  # noqa: BLE001
            server.kill()

    print("\n%d/%d checks passed" % (len(checks) - len(failures), len(checks)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
