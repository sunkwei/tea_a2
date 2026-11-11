#!/usr/bin/env python3
"""隧道终端（侧载 --target terminal）的端到端测试。

    python3 tests/smoke_tunnel_terminal.py [build-dir]

它在一台机器上把整条链路摆出来：

    broker ──TLS──▶ 侧载(agent, --target terminal) ──隧道──▶ 侧载(client) ──▶ 浏览器

检查的是：

  1. agent 侧载真的起了一个终端（127.0.0.1 上的一个 HTTP 服务）；
  2. 隧道是**字节透明**的：浏览器打开 client 的本地端口，拿到的是远端
     那个终端的页面（不是本地生成的）；
  3. 终端能开会话、收键盘、回显输出（真的 PTY，不是假回显）；
  4. 文件能上传、也能下载回来；
  5. client 侧知道对面是终端（它会告诉运维该打开哪个地址）。

二进制不在就跳过（不是每个环境都编了隧道）。
"""

import base64
import json
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CHECKS = []


def check(name, ok, detail=""):
    CHECKS.append((name, bool(ok), detail))
    print("%s %s%s" % ("ok  " if ok else "FAIL", name,
                       "" if ok else "  [%s]" % detail[:400]))


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for_port(port, seconds=10.0):
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def wait_for_log(path, pattern, seconds=15.0):
    deadline = time.time() + seconds
    rx = re.compile(pattern)
    while time.time() < deadline:
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                text = handle.read()
            if rx.search(text):
                return text
        except OSError:
            pass
        time.sleep(0.2)
    return ""


def find_build(argument):
    """Where the tunnel binaries are (same rule as smoke_tunnel.py)."""
    env = os.environ.get("TEA_TUNNEL_BUILD", "")
    if env:
        return env
    if argument:
        if os.path.isdir(argument):
            return argument
        parent = os.path.dirname(os.path.abspath(argument))
        if os.path.basename(parent) == "bin":
            return os.path.dirname(parent)
    return os.path.join(ROOT, "build")


def http(url, method="GET", body=None, ctype=None, timeout=10):
    request = urllib.request.Request(url, method=method, data=body)
    if ctype:
        request.add_header("Content-Type", ctype)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status, response.read(), dict(response.headers)


def post_json(url, payload, timeout=15):
    data = json.dumps(payload).encode("utf-8")
    status, raw, _ = http(url, "POST", data, "application/json", timeout)
    return status, json.loads(raw.decode("utf-8"))


def main():
    build = find_build(sys.argv[1] if len(sys.argv) > 1 else "")
    broker = os.path.join(build, "bin", "a2_broker")
    sidecar = os.path.join(build, "bin", "a2_tunnel")
    if not os.path.exists(broker) or not os.path.exists(sidecar):
        print("skip  (no a2_broker / a2_tunnel in %s)" % build)
        return 0

    work = tempfile.mkdtemp(prefix="smoke-tunnel-term-")
    home = os.path.join(work, "home")
    os.makedirs(home)
    signal_port = free_port()
    stun_port = free_port()
    client_port = free_port()
    env = dict(os.environ)
    env["HOME"] = home

    procs = []

    def spawn(args, log_name):
        path = os.path.join(work, log_name)
        handle = open(path, "wb")
        proc = subprocess.Popen(args, stdout=handle, stderr=subprocess.STDOUT, env=env,
                                cwd=work)
        procs.append((proc, path))
        return proc, path

    try:
        # ---- broker ---------------------------------------------------
        broker_log = os.path.join(work, "broker.log")
        spawn([broker, "--stun-port", str(stun_port), "--signal-port", str(signal_port),
               "--signal-bind", "127.0.0.1", "--cert-name", "test-broker"], "broker.log")
        text = wait_for_log(broker_log, r"fingerprint \(sha256\): [0-9a-f]{64}")
        match = re.search(r"fingerprint \(sha256\): ([0-9a-f]{64})", text)
        check("the broker starts", bool(match), text[-300:] if not match else "")
        if not match:
            return 1
        pin = match.group(1)

        # ---- agent 侧载（带终端）---------------------------------------
        agent_log = os.path.join(work, "agent.log")
        spawn([sidecar, "--role", "agent", "--broker", "127.0.0.1:%d" % signal_port,
               "--stun", "127.0.0.1:%d" % stun_port, "--turn", "127.0.0.1:%d" % stun_port,
               "--tls-pin", pin, "--target", "terminal",
               "--wait-timeout", "120"], "agent.log")
        text = wait_for_log(agent_log, r"the terminal is listening on 127\.0\.0\.1:\d+")
        check("the agent side starts a terminal", "the terminal is listening" in text,
              text[-400:])
        match = re.search(r"a2_tunnel --role client --session ([0-9a-f]+)", text)
        check("the agent side prints a session code", bool(match),
              text[-300:] if not match else "")
        if not match:
            return 1
        session = match.group(1)
        check("the agent side says the target is a shell",
              "a real shell on this machine" in text, text[-400:])

        # ---- client 侧载 ------------------------------------------------
        client_log = os.path.join(work, "client.log")
        spawn([sidecar, "--role", "client", "--session", session,
               "--broker", "127.0.0.1:%d" % signal_port,
               "--stun", "127.0.0.1:%d" % stun_port, "--turn", "127.0.0.1:%d" % stun_port,
               "--tls-pin", pin, "--port", str(client_port)], "client.log")
        text = wait_for_log(client_log, r"the data channel is open", seconds=40)
        check("the client opens the data channel", "the data channel is open" in text,
              text[-400:])
        text = wait_for_log(client_log, r"the other end is a terminal", seconds=20)
        check("the client is told the far end is a terminal",
              "the other end is a terminal" in text, text[-400:])
        check("the client tells the operator which address to open",
              "The far end is a terminal" in text, text[-400:])

        # ---- 页面真的穿过隧道 ------------------------------------------
        base = "http://127.0.0.1:%d" % client_port
        page = ""
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                status, raw, headers = http(base + "/", timeout=5)
                page = raw.decode("utf-8", "replace")
                break
            except Exception:
                time.sleep(0.5)
        check("the page comes through the tunnel", "隧道终端" in page, page[:200])
        check("the page pulls in the shared VT engine", "term.js" in page, page[:200])

        # 脚本也要穿过隧道
        status, raw, headers = http(base + "/term.js", timeout=10)
        check("the VT engine comes through the tunnel",
              status == 200 and b"TeaTerm" in raw, str(status))
        status, raw, headers = http(base + "/tunnel_terminal.js", timeout=10)
        check("the page script comes through the tunnel",
              status == 200 and b"api/term" in raw, str(status))

        # ---- 远端的机器信息 ------------------------------------------------
        status, raw, _ = http(base + "/api/term", timeout=10)
        cap = json.loads(raw.decode("utf-8"))
        check("the far end says what it can do",
              cap.get("available") and cap.get("platform"), raw.decode("utf-8", "replace"))

        # ---- 开一个会话 ----------------------------------------------------
        status, answer = post_json(base + "/api/term/session", {"cols": 80, "rows": 24})
        check("a session can be opened", answer.get("ok") and answer.get("id"),
              json.dumps(answer))
        session_id = answer.get("id", "")
        if not session_id:
            return 1

        # ---- 打字，看它回显 ----------------------------------------------
        marker = "TEA_TERMINAL_%d" % int(time.time())
        post_json(base + "/api/term/session/%s/input" % session_id,
                  {"data": "echo %s\n" % marker})

        text = ""
        deadline = time.time() + 25
        while time.time() < deadline:
            status, raw, _ = http(
                base + "/api/term/session/%s/output?cursor=0&wait=5000" % session_id,
                timeout=15)
            answer = json.loads(raw.decode("utf-8"))
            chunk = base64.b64decode(answer.get("data_b64") or "")
            text += chunk.decode("utf-8", "replace")
            if marker in text:
                break
            time.sleep(0.3)
        check("the shell answers what was typed", marker in text, text[-300:])

        # 真的 shell（不是回显）：让远端算一个数
        post_json(base + "/api/term/session/%s/input" % session_id,
                  {"data": "echo $((6*7))\n"})
        deadline = time.time() + 25
        while time.time() < deadline:
            status, raw, _ = http(
                base + "/api/term/session/%s/output?cursor=0&wait=3000" % session_id,
                timeout=15)
            answer = json.loads(raw.decode("utf-8"))
            text += base64.b64decode(answer.get("data_b64") or "").decode("utf-8", "replace")
            if "42" in text:
                break
            time.sleep(0.3)
        check("the far end really runs a shell", "42" in text, text[-300:])

        # ---- 文件上传 / 下载 -------------------------------------------------
        payload = b"tea-tunnel-upload-check\n"
        status, raw, _ = http(
            base + "/api/term/session/%s/upload?name=tunnel-check.txt" % session_id,
            "POST", payload, "application/octet-stream", timeout=20)
        answer = json.loads(raw.decode("utf-8"))
        check("a file can be uploaded through the tunnel",
              answer.get("ok") and answer.get("bytes") == len(payload),
              raw.decode("utf-8", "replace"))

        status, raw, _ = http(
            base + "/api/term/session/%s/file?path=tunnel-check.txt" % session_id,
            timeout=20)
        check("the file comes back", raw == payload, repr(raw[:80]))

        # ---- 结束会话 --------------------------------------------------------
        status, raw, _ = http(base + "/api/term/session/%s" % session_id, "DELETE",
                              timeout=10)
        check("a session can be closed", status == 200, str(status))

        return 0 if all(ok for _, ok, _ in CHECKS) else 1
    finally:
        for proc, _ in procs:
            try:
                proc.send_signal(signal.SIGTERM)
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass


if __name__ == "__main__":
    sys.exit(main())
