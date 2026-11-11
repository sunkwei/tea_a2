#!/usr/bin/env python3
"""隧道（broker + 侧载）的端到端测试。

    python3 tests/smoke_tunnel.py [build-dir]

它在一台机器上把整条链路摆出来：

    broker ──TLS──▶ 侧载(agent) ──隧道──▶ 侧载(client) ──▶ 假 a2

检查的是：

  1. broker 自己终结 TLS（不用 nginx），并打印证书指纹；
  2. 证书和指纹在重启之后不变（客户端配一次就够）；
  3. 隧道真的通：客户端能拿到假 a2 的回答；
  4. 指纹不对的客户端**进不来**（这是 pin 的意义）；
  5. broker 的地址写在配置文件里时，客户端会照着走；
  6. TURN 账号可以放在一个**人手改的 sqlite 文件**里，broker 和侧载都读它。

二进制不在就跳过（不是每个环境都编了隧道）。
"""

import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAKE_A2 = """
import http.server, socketserver, sys
port = int(sys.argv[1])
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'{"ok":true,"who":"fake-a2"}'
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *args):
        pass
socketserver.TCPServer.allow_reuse_address = True
with socketserver.TCPServer(("127.0.0.1", port), Handler) as server:
    server.serve_forever()
"""

CHECKS = []


def check(name, ok, detail=""):
    CHECKS.append((name, bool(ok), detail))
    print("%s %s%s" % ("ok  " if ok else "FAIL", name,
                       "" if ok else "  [%s]" % detail[:300]))


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
    """Where the tunnel binaries are.

    The test suite hands every test the agent binary (build/bin/tea_agent),
    so that path is turned back into the build directory. A directory is
    taken as it is, and TEA_TUNNEL_BUILD wins over both.
    """
    env = os.environ.get("TEA_TUNNEL_BUILD", "")
    if env:
        return env
    if argument:
        if os.path.isdir(argument):
            return argument
        # .../build/bin/tea_agent -> .../build
        parent = os.path.dirname(os.path.abspath(argument))
        if os.path.basename(parent) == "bin":
            return os.path.dirname(parent)
    return os.path.join(ROOT, "build")


def main():
    build = find_build(sys.argv[1] if len(sys.argv) > 1 else "")
    broker = os.path.join(build, "bin", "a2_broker")
    sidecar = os.path.join(build, "bin", "a2_tunnel")
    if not os.path.exists(broker) or not os.path.exists(sidecar):
        print("skip  (no a2_broker / a2_tunnel in %s)" % build)
        return 0

    work = tempfile.mkdtemp(prefix="smoke-tunnel-")
    home = os.path.join(work, "home")
    os.makedirs(home)
    signal_port = free_port()
    stun_port = free_port()
    a2_port = free_port()
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
        # ---- 假 a2（agent 侧载要连的那个）--------------------------------
        script = os.path.join(work, "fake_a2.py")
        with open(script, "w", encoding="utf-8") as handle:
            handle.write(FAKE_A2)
        spawn([sys.executable, script, str(a2_port)], "a2.log")
        wait_for_port(a2_port)

        # ---- broker ---------------------------------------------------
        broker_log = os.path.join(work, "broker.log")
        spawn([broker, "--stun-port", str(stun_port), "--signal-port", str(signal_port),
               "--signal-bind", "127.0.0.1", "--cert-name", "test-broker"], "broker.log")
        text = wait_for_log(broker_log, r"fingerprint \(sha256\): [0-9a-f]{64}")
        match = re.search(r"fingerprint \(sha256\): ([0-9a-f]{64})", text)
        check("the broker starts and prints a fingerprint", bool(match),
              text[-300:] if not match else "")
        if not match:
            return 1
        pin = match.group(1)
        check("the broker terminates TLS itself (no nginx in front)",
              "signalling on tcp/%d (TLS" % signal_port in text, text[-300:])
        check("the certificate is kept on disk",
              os.path.exists(os.path.join(home, ".tea_agent", "tunnel", "broker.crt")))

        # The TURN account book: a2_broker.db in the current directory. The
        # first start makes it and writes the default account into it.
        book = os.path.join(work, "a2_broker.db")
        check("the broker makes the account book on the first start",
              os.path.exists(book) and "created now" in text, text[-300:])
        import sqlite3
        db = sqlite3.connect(book)
        rows = list(db.execute("SELECT user, pass FROM turn_accounts"))
        db.close()
        check("the default account is written into it",
              rows == [("1hblsqt.", "2ghlmcl,")], str(rows))

        # ---- agent 侧载 -------------------------------------------------
        agent_log = os.path.join(work, "agent.log")
        spawn([sidecar, "--role", "agent", "--broker", "127.0.0.1:%d" % signal_port,
               "--stun", "127.0.0.1:%d" % stun_port, "--turn", "127.0.0.1:%d" % stun_port,
               "--tls-pin", pin, "--target", "127.0.0.1:%d" % a2_port,
               "--wait-timeout", "120"], "agent.log")
        text = wait_for_log(agent_log, r"a2_tunnel --role client --session [0-9a-f]+")
        match = re.search(r"a2_tunnel --role client --session ([0-9a-f]+)", text)
        check("the agent side prints a session code", bool(match),
              text[-300:] if not match else "")
        if not match:
            return 1
        session = match.group(1)
        check("the agent side hands out the fingerprint with the command",
              ("--tls-pin %s" % pin) in text, text[-300:])

        # ---- client 侧载 ------------------------------------------------
        client_log = os.path.join(work, "client.log")
        spawn([sidecar, "--role", "client", "--session", session,
               "--broker", "127.0.0.1:%d" % signal_port,
               "--stun", "127.0.0.1:%d" % stun_port, "--turn", "127.0.0.1:%d" % stun_port,
               "--tls-pin", pin, "--port", str(client_port)], "client.log")
        text = wait_for_log(client_log, r"the data channel is open", seconds=40)
        check("the client opens the data channel", "the data channel is open" in text,
              text[-400:])

        # ---- 字节真的过得去 ---------------------------------------------
        answer = ""
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                with urllib.request.urlopen("http://127.0.0.1:%d/api/health" % client_port,
                                            timeout=3) as response:
                    answer = response.read().decode("utf-8", "replace")
                break
            except Exception:
                time.sleep(0.5)
        check("the answer comes through the tunnel", "fake-a2" in answer, answer)

        # ---- 指纹不对的进不来 -------------------------------------------
        bad_log = os.path.join(work, "badpin.log")
        bad_pin = "a" * 64
        result = subprocess.run(
            [sidecar, "--role", "client", "--session", session,
             "--broker", "127.0.0.1:%d" % signal_port, "--tls-pin", bad_pin,
             "--port", str(free_port()), "--wait-timeout", "5"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, cwd=work, timeout=60)
        output = result.stdout.decode("utf-8", "replace")
        check("a client with the wrong fingerprint is refused",
              "not the one that was pinned" in output and
              "the data channel is open" not in output, output[-300:])

        # ---- 指纹在重启之后不变 -----------------------------------------
        broker_procs = [p for p, path in procs if "a2_broker" in " ".join(p.args)]
        for proc in broker_procs:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        time.sleep(0.5)
        broker_log2 = os.path.join(work, "broker2.log")
        spawn([broker, "--stun-port", str(stun_port), "--signal-port", str(signal_port),
               "--signal-bind", "127.0.0.1", "--cert-name", "test-broker"], "broker2.log")
        text = wait_for_log(broker_log2, r"fingerprint \(sha256\): [0-9a-f]{64}")
        match = re.search(r"fingerprint \(sha256\): ([0-9a-f]{64})", text)
        check("the fingerprint is the same after a restart",
              bool(match) and match.group(1) == pin,
              "old %s new %s" % (pin, match.group(1) if match else "?"))

        # ---- TURN 账号簿（sqlite，人手改）--------------------------------
        # 人手加一个账号，broker 重启后就要用它
        db = sqlite3.connect(book)
        db.execute("INSERT INTO turn_accounts (user, pass) VALUES ('alice', 's3cret')")
        db.commit()
        db.close()

        book2_log = os.path.join(work, "bookbroker2.log")
        book2_signal = free_port()
        book2_stun = free_port()
        spawn([broker, "--stun-port", str(book2_stun), "--signal-port", str(book2_signal),
               "--signal-bind", "127.0.0.1", "--turn-db", book], "bookbroker2.log")
        text = wait_for_log(book2_log, r"TURN accounts accepted", seconds=20)
        check("the broker takes the accounts from the book",
              "(2 accounts)" in text and "TURN accounts accepted: 2" in text, text[-300:])
        check("the book is not seeded again",
              "created now" not in text, text[-300:])

        # 侧载从同一本账号簿里挑一个账号用
        book_client_log = os.path.join(work, "bookclient.log")
        spawn([sidecar, "--role", "client", "--session", session,
               "--broker", "127.0.0.1:%d" % book2_signal, "--turn-db", book,
               "--port", str(free_port()), "--wait-timeout", "5"], "bookclient.log")
        text = wait_for_log(book_client_log, r"using the TURN account", seconds=20)
        check("the sidecar takes its TURN account from the same book",
              'using the TURN account "' in text, text[-300:])

        # 命令行给的账号比账号簿里的优先
        flag_client_log = os.path.join(work, "flagclient.log")
        spawn([sidecar, "--role", "client", "--session", session,
               "--broker", "127.0.0.1:%d" % book2_signal, "--turn-db", book,
               "--turn-user", "bob", "--turn-pass", "x",
               "--port", str(free_port()), "--wait-timeout", "5"], "flagclient.log")
        text = wait_for_log(flag_client_log, r"client side, room", seconds=20)
        check("an account given on the command line wins over the book",
              "using the TURN account" not in text, text[-300:])

        # ---- TURN 用量记账 ------------------------------------------------
        # broker 把中继的用量写进同一个 db（a2_broker.db），有明细表和按天的汇总表。
        import sqlite3
        db = sqlite3.connect(book)
        tables = [row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        db.close()
        check("the account book has the usage tables",
              "turn_usage" in tables and "turn_usage_daily" in tables, str(tables))

        result = subprocess.run([broker, "--usage", "--turn-db", book],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                cwd=work, timeout=20)
        text = result.stdout.decode("utf-8", "replace")
        check("the usage report can be printed",
              "TURN usage in" in text, text[-200:])

        # ---- 配置文件里的地址 -------------------------------------------
        conf_dir = os.path.join(home, ".tea_agent", "tunnel")
        os.makedirs(conf_dir, exist_ok=True)
        with open(os.path.join(conf_dir, "broker.txt"), "w", encoding="utf-8") as handle:
            handle.write("address=127.0.0.1:%d\npin=%s\n" % (signal_port, pin))
        conf_log = os.path.join(work, "conf.log")
        spawn([sidecar, "--role", "client", "--session", session,
               "--tls-pin", pin, "--port", str(free_port()), "--wait-timeout", "5"],
              "conf.log")
        text = wait_for_log(conf_log, r"client side, room", seconds=20)
        check("the broker address can live in a file",
              ("broker 127.0.0.1:%d" % signal_port) in text, text[-300:])
    finally:
        for proc, _ in procs:
            try:
                proc.send_signal(signal.SIGTERM)
            except OSError:
                pass
        for proc, _ in procs:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        shutil.rmtree(work, ignore_errors=True)

    passed = sum(1 for _, ok, _ in CHECKS if ok)
    print("\n%d/%d checks passed" % (passed, len(CHECKS)))
    return 0 if passed == len(CHECKS) else 1


if __name__ == "__main__":
    sys.exit(main())
