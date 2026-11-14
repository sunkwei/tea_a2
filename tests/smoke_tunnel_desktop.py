#!/usr/bin/env python3
"""隧道桌面（--role desktop / --role control）的端到端测试。

    python3 tests/smoke_tunnel_desktop.py [build-dir]

它在一台机器上把整条链路摆出来：

    broker ──TLS──▶ 侧载(desktop，抓屏+编码) ──隧道──▶ 侧载(control，解码+出画) ──▶ 浏览器

检查的是：

  1. desktop 侧载真的起了桌面的服务（127.0.0.1 上的一个 HTTP 服务）；
  2. control 侧载起了看图的页面（本地端口，浏览器打开它）；
  3. 画面**穿过隧道**到了 control 那头（是张 JPEG，大小对得上屏幕）；
  4. 鼠标键盘事件**穿回去**到了对面（对面说它收下了几个事件）；
  5. 页面和脚本都在二进制里（设备上没有文件可读）。

二进制不在就跳过（不是每个环境都编了隧道）。
"""

import json
import os
import re
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


def find_binary(build, name):
    """The binary, with or without the .exe the Windows build gives it."""
    for folder in (os.path.join(build, "bin"), os.path.join(build, "tunnel", "bin")):
        for candidate in (name, name + ".exe"):
            path = os.path.join(folder, candidate)
            if os.path.exists(path):
                return path
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


def post_json(url, payload, timeout=20):
    data = json.dumps(payload).encode("utf-8")
    status, raw, _ = http(url, "POST", data, "application/json", timeout)
    return status, json.loads(raw.decode("utf-8"))


def main():
    build = find_build(sys.argv[1] if len(sys.argv) > 1 else "")
    broker = find_binary(build, "a2_broker")
    sidecar = find_binary(build, "a2_tunnel")
    if not broker or not sidecar:
        print("skip  (no a2_broker / a2_tunnel in %s)" % build)
        return 0

    work = tempfile.mkdtemp(prefix="smoke-tunnel-desktop-")
    home = os.path.join(work, "home")
    os.makedirs(home)
    signal_port = free_port()
    stun_port = free_port()
    client_port = free_port()
    viewer_port = free_port()
    env = dict(os.environ)
    env["HOME"] = home
    env["TEA_AGENT_HOME"] = home   # Windows 上用的是这个（不是 HOME）

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

        # ---- desktop 侧载（设备那边）------------------------------------
        agent_log = os.path.join(work, "desktop.log")
        spawn([sidecar, "--role", "desktop", "--broker", "127.0.0.1:%d" % signal_port,
               "--stun", "127.0.0.1:%d" % stun_port, "--turn", "127.0.0.1:%d" % stun_port,
               "--tls-pin", pin, "--wait-timeout", "120"], "desktop.log")
        text = wait_for_log(agent_log, r"the desktop is listening on 127\.0\.0\.1:\d+")
        check("the desktop side starts the screen service", "the desktop is listening" in text,
              text[-400:])
        match = re.search(r"a2_tunnel --role control --session ([0-9a-f]+)", text)
        check("the desktop side prints the command for the control side", bool(match),
              text[-300:] if not match else "")
        if not match:
            return 1
        session = match.group(1)

        # ---- control 侧载（外面那边）------------------------------------
        client_log = os.path.join(work, "control.log")
        spawn([sidecar, "--role", "control", "--session", session,
               "--broker", "127.0.0.1:%d" % signal_port,
               "--stun", "127.0.0.1:%d" % stun_port, "--turn", "127.0.0.1:%d" % stun_port,
               "--tls-pin", pin, "--port", str(client_port),
               "--viewer-port", str(viewer_port)], "control.log")
        text = wait_for_log(client_log, r"the data channel is open", seconds=40)
        check("the control side opens the data channel", "the data channel is open" in text,
              text[-400:])
        text = wait_for_log(client_log, r"the viewer is on http://127\.0\.0\.1:\d+/", seconds=20)
        check("the control side starts the viewer", "the viewer is on" in text, text[-400:])
        check("the control side says which address to open",
              "open this in a browser" in text, text[-400:])
        check("the control side knows the far end is a desktop",
              "the other end is a desktop" in text, text[-400:])

        # ---- 页面 ------------------------------------------------------
        base = "http://127.0.0.1:%d" % viewer_port
        status, raw, headers = http(base + "/", timeout=10)
        page = raw.decode("utf-8", "replace")
        check("the viewer page is served", status == 200 and "远程桌面" in page, page[:200])
        check("the page pulls in its script", "tunnel_desktop.js" in page, page[:200])
        status, raw, headers = http(base + "/tunnel_desktop.js", timeout=10)
        check("the page script is served",
              status == 200 and b"api/control/frame.jpg" in raw, str(status))

        # ---- 画面穿过隧道 ------------------------------------------------
        picture = b""
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                status, raw, headers = http(base + "/api/control/frame.jpg", timeout=10)
                if status == 200 and raw:
                    picture = raw
                    break
            except Exception:
                pass
            time.sleep(0.5)
        check("a picture comes through the tunnel", bool(picture), "no picture in 30s")
        check("the picture is a JPEG", picture[:2] == b"\xff\xd8", repr(picture[:8]))
        check("the picture is not empty", len(picture) > 1000, "len=%d" % len(picture))
        print("     (the picture is %d bytes; at two a second that is %.0f KB/s)" % (
            len(picture), len(picture) * 2 / 1024.0))

        # ---- 画面会一直来 -------------------------------------------------
        # 之前有个毛病：第一张能取到，之后就卡住了（连接太多，设备端的
        # 线程池被占满）。所以这里再等几秒，看画面是不是还在来。
        status, raw, _ = http(base + "/api/control/info", timeout=10)
        first = json.loads(raw.decode("utf-8")).get("frames", 0)
        time.sleep(3)
        status, raw, _ = http(base + "/api/control/info", timeout=10)
        later = json.loads(raw.decode("utf-8")).get("frames", 0)
        check("the pictures keep coming", later > first,
              "frames went from %s to %s" % (first, later))

        # ---- 尺寸 -------------------------------------------------------
        status, raw, _ = http(base + "/api/control/info", timeout=10)
        info = json.loads(raw.decode("utf-8"))
        check("the viewer knows the size of the picture",
              info.get("frame_width", 0) > 0 and info.get("frame_height", 0) > 0,
              json.dumps(info))
        check("the viewer knows the size of the screen",
              info.get("screen_width", 0) > 0 and info.get("screen_height", 0) > 0,
              json.dumps(info))
        check("the pictures were decoded", info.get("decoded", 0) > 0, json.dumps(info))

        # ---- 鼠标键盘穿回去 ------------------------------------------------
        status, answer = post_json(base + "/api/control/input",
                                   {"events": [{"t": "move", "x": 10, "y": 20},
                                               {"t": "key", "down": True, "vk": 0,
                                                "text": "a"}]})
        check("the input reaches the other side",
              answer.get("ok") and answer.get("applied", 0) >= 2, json.dumps(answer))

        # ---- 隧道自己的端口（设备端那个）要说清楚，不该是 404 --------------
        # 外面那台如果把浏览器开在隧道端口上（横幅以前就指那个），请求会
        # 穿到设备端去 —— 那边要有一句话说明「画面在控制端」。
        desktop_page = ""
        desktop_status = 0
        match = re.search(r"the desktop is listening on 127\.0\.0\.1:(\d+)", text)
        if not match:
            with open(agent_log, encoding="utf-8", errors="replace") as handle:
                match = re.search(r"the desktop is listening on 127\.0\.0\.1:(\d+)",
                                  handle.read())
        if match:
            try:
                desktop_status, raw, _ = http("http://127.0.0.1:%s/" % match.group(1), timeout=5)
                desktop_page = raw.decode("utf-8", "replace")
            except Exception as error:
                desktop_page = str(error)
        check("the desktop side has no page of its own", "远程桌面" not in desktop_page,
              desktop_page[:120])
        check("the desktop side explains itself instead of 404",
              desktop_status == 200 and "control side" in desktop_page,
              "status=%s %s" % (desktop_status, desktop_page[:120]))
    finally:
        for proc, _ in procs:
            try:
                proc.terminate()
            except Exception:
                pass
        time.sleep(0.5)
        for proc, _ in procs:
            try:
                proc.kill()
            except Exception:
                pass

    failed = [c for c in CHECKS if not c[1]]
    print("\n%d/%d checks passed" % (len(CHECKS) - len(failed), len(CHECKS)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
