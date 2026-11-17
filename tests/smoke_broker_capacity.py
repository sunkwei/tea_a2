#!/usr/bin/env python3
"""a2_broker 的容量与能力上限。

    python3 tests/smoke_broker_capacity.py [build-dir]

broker 有一个"能服务多少"的上限：--max-peer-pair 说多少对 peers，中继端口池
说有多少端口（每对 peers 要两个）。这里钉住的是：

  1. 默认值：新版本默认 256 对 peers（512 个中继分配）、端口池 49160-50175；
  2. 池子必须装得下：--max-peer-pair 说 512 对而池子只有 1016 个端口时，
     **启动就报错**（不是等到第一个连接失败才发现）；
  3. 端口范围写反（begin > end）必须启动就报错：现状会静默退化成"所有分配
     抢同一个端口"，第二个分配就 500；
  4. 命令行写错不能把进程搞崩（老实现 std::stoi 抛异常 -> abort -> systemd
     变成崩溃循环）；
  5. "--flag value" 和 "--flag=value" 必须一样；
  6. 池子满了要说清楚是**谁**满了：客户端拿到 486，broker 的日志里有一句
     "the relay is full"（而不是一句含糊的 "TURN allocation failed"）；
  7. --usage 要能报出这次运行的容量（在同一个 sqlite 账本里）。

客户端是手写的最小 STUN/TURN Allocate（RFC 5766）：先拿 401 的 realm+nonce，
再带 MESSAGE-INTEGRITY 重发。不碰真网络，全在 127.0.0.1 上。
"""

import hashlib
import hmac
import os
import re
import socket
import struct
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAGIC_COOKIE = 0x2112A442

ATTR_REQUESTED_TRANSPORT = 0x0019
ATTR_USERNAME = 0x0006
ATTR_REALM = 0x0014
ATTR_NONCE = 0x0015
ATTR_MESSAGE_INTEGRITY = 0x0008
ATTR_ERROR_CODE = 0x0009

TYPE_ALLOCATE = 0x0003
TYPE_ERROR = 0x0111

# broker 内建的 TURN 账号（src/tunnel_broker.cpp）
DEFAULT_USER = "1hblsqt."
DEFAULT_PASS = "2ghlmcl,"

CHECKS = []


def check(name, ok, detail=""):
    CHECKS.append((name, bool(ok), detail))
    print("%s %s%s" % ("ok  " if ok else "FAIL", name,
                       "" if ok else "  [%s]" % detail[:300]))


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def find_binary(build, name):
    for folder in (os.path.join(build, "bin"), os.path.join(build, "tunnel", "bin")):
        for candidate in (name, name + ".exe"):
            path = os.path.join(folder, candidate)
            if os.path.exists(path):
                return path
    return ""


def find_build(argument):
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


def read_log(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return ""


def wait_for_log(path, pattern, seconds=15.0):
    deadline = time.time() + seconds
    rx = re.compile(pattern)
    while time.time() < deadline:
        text = read_log(path)
        if rx.search(text):
            return text
        time.sleep(0.1)
    return read_log(path)


# ---- the smallest TURN client there is ------------------------------------


def stun_attr(kind, value):
    padding = (4 - len(value) % 4) % 4
    return struct.pack(">HH", kind, len(value)) + value + b"\x00" * padding


def parse_attrs(body):
    attrs = {}
    pos = 0
    while pos + 4 <= len(body):
        kind, length = struct.unpack(">HH", body[pos:pos + 4])
        value = body[pos + 4:pos + 4 + length]
        attrs.setdefault(kind, value)
        pos += 4 + length + ((4 - length % 4) % 4)
    return attrs


def parse_response(data):
    kind, length = struct.unpack(">HH", data[:4])
    attrs = parse_attrs(data[20:20 + length])
    code = None
    if ATTR_ERROR_CODE in attrs and len(attrs[ATTR_ERROR_CODE]) >= 4:
        raw = attrs[ATTR_ERROR_CODE]
        code = (raw[2] & 0x07) * 100 + raw[3]
    return kind, code, attrs


def allocate_message(username, realm, nonce, password, transaction_id):
    """An Allocate request. With a username it carries the long-term
    credentials (MESSAGE-INTEGRITY over the message, RFC 5389)."""
    body = stun_attr(ATTR_REQUESTED_TRANSPORT, bytes([17, 0, 0, 0]))  # UDP
    if username:
        body += stun_attr(ATTR_USERNAME, username.encode())
        body += stun_attr(ATTR_REALM, realm.encode())
        body += stun_attr(ATTR_NONCE, nonce.encode())
    length = len(body) + (24 if username else 0)
    message = struct.pack(">HHI", TYPE_ALLOCATE, length, MAGIC_COOKIE) + transaction_id + body
    if username:
        key = hashlib.md5(("%s:%s:%s" % (username, realm, password)).encode()).digest()
        digest = hmac.new(key, message, hashlib.sha1).digest()
        message += struct.pack(">HH", ATTR_MESSAGE_INTEGRITY, len(digest)) + digest
    return message


def binding_request(host, port, timeout=2.0):
    """A plain STUN Binding request. It says whether the broker's UDP port
    answers on this address at all."""
    sock = socket.socket(socket.AF_INET if ":" not in host else socket.AF_INET6,
                         socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        body = stun_attr(0x0020, b"\x21\x12\xa4\x42")   # XOR-MAPPED-ADDRESS
        message = struct.pack(">HHI", 0x0001, len(body), MAGIC_COOKIE) + os.urandom(12) + body
        sock.sendto(message, (host, port))
        data, _ = sock.recvfrom(2048)
        kind, code, _ = parse_response(data)
        return kind == 0x0101
    except OSError:
        return False
    finally:
        sock.close()


def reachable_host(port):
    """The address the broker's UDP port answers on.

    On most machines that is 127.0.0.1. On a WSL2 machine it is ::1: the
    STUN server binds an IPv6 socket (libjuice prefers IPv6) and WSL2 does
    not deliver IPv4 loopback packets to it.
    """
    for host in ("127.0.0.1", "::1"):
        if binding_request(host, port):
            return host
    return "127.0.0.1"


def allocate(host, port, user=DEFAULT_USER, password=DEFAULT_PASS, timeout=5.0):
    """Ask for a relay. Returns the STUN error code (None when it worked)."""
    sock = socket.socket(socket.AF_INET if ":" not in host else socket.AF_INET6,
                         socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        tid = os.urandom(12)
        sock.sendto(allocate_message(None, "", "", "", tid), (host, port))
        data, _ = sock.recvfrom(2048)
        kind, code, attrs = parse_response(data)
        if code == 401:
            realm = attrs[ATTR_REALM].decode()
            nonce = attrs[ATTR_NONCE].decode()
            tid = os.urandom(12)
            sock.sendto(allocate_message(user, realm, nonce, password, tid), (host, port))
            data, _ = sock.recvfrom(2048)
            kind, code, attrs = parse_response(data)
        return code
    finally:
        sock.close()


def main():
    build = find_build(sys.argv[1] if len(sys.argv) > 1 else "")
    broker = find_binary(build, "a2_broker")
    if not broker:
        print("skip  (no a2_broker in %s)" % build)
        return 0

    work = tempfile.mkdtemp(prefix="smoke-broker-capacity-")
    home = os.path.join(work, "home")
    os.makedirs(home)
    env = dict(os.environ)
    env["HOME"] = home
    env["TEA_AGENT_HOME"] = home

    def run_broker(args, timeout=20):
        """Run the broker and wait for it to stop. Returns (code, output)."""
        proc = subprocess.Popen([broker] + args, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, env=env, cwd=work, text=True)
        try:
            output, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            output, _ = proc.communicate()
        return proc.returncode, output

    try:
        # ---- 1. the defaults ------------------------------------------------
        code, output = run_broker(["--help"])
        check("--help shows the new defaults",
              "--max-peer-pair 256" in output and "--relay-begin 49160" in output and
              "--relay-end 50175" in output, output)

        # ---- 2. a small capacity starts and says what it can do ----------
        stun_port = free_port()
        signal_port = free_port()
        book = os.path.join(work, "a2_broker.db")
        log = os.path.join(work, "broker.log")
        handle = open(log, "wb")
        proc = subprocess.Popen([broker, "--no-tls", "--stun-port", str(stun_port),
                                 "--signal-port", str(signal_port), "--signal-bind", "127.0.0.1",
                                 "--max-peer-pair", "2", "--relay-begin", "49160",
                                 "--relay-end", "49180", "--turn-db", book],
                                stdout=handle, stderr=subprocess.STDOUT, env=env, cwd=work)
        try:
            text = wait_for_log(log, r"signalling on tcp/%d" % signal_port)
            check("the broker starts with a small capacity",
                  "capacity: 2 peer pairs (4 allocations max" in text, text[-300:])
            check("the relay port pool is printed with its size",
                  "relay 49160-49180, 21 ports" in text, text[-300:])

            # ---- 3. the pool has to be big enough ------------------------
            code, output = run_broker(["--max-peer-pair", "512"])
            check("a pool that is too small is refused at startup",
                  code == 2 and "too small" in output, "code=%d %s" % (code, output))

            # ---- 4. the pool must not be empty --------------------------
            code, output = run_broker(["--relay-begin", "49672", "--relay-end", "49160"])
            check("a reversed port pool is refused at startup",
                  code == 2 and "empty" in output, "code=%d %s" % (code, output))

            # ---- 5. a wrong number must not crash the broker ------------
            code, output = run_broker(["--max-peer-pair", "abc"])
            check("a wrong number is an error, not a crash",
                  code == 2 and "needs a number" in output, "code=%d %s" % (code, output))

            # ---- 6. "--flag value" and "--flag=value" are the same -------
            code, output = run_broker(["--max-peer-pair=512"])
            check("--flag=value works like --flag value",
                  code == 2 and "too small" in output, "code=%d %s" % (code, output))

            # ---- 7. the relay says who is full --------------------------
            host = reachable_host(stun_port)
            check("the broker answers the STUN port on %s" % host, host != "127.0.0.1" or
                  binding_request(host, stun_port))
            codes = []
            for _ in range(5):
                codes.append(allocate(host, stun_port))
            check("four allocations fit in the capacity of two pairs",
                  codes[:4] == [None, None, None, None], str(codes))
            check("the fifth allocation is refused with 486",
                  codes[4] == 486, str(codes))
            text = wait_for_log(log, r"the relay is full", seconds=5.0)
            check("the broker says the relay is full (and how many are in use)",
                  "the relay is full" in text and "4 allocations are in use" in text,
                  text[-300:])

            # ---- 8. --usage knows the capacity --------------------------
            usage = ""
            for _ in range(20):
                code, usage = run_broker(["--usage", "--turn-db", book])
                if "capacity:" in usage:
                    break
                time.sleep(0.2)
            check("--usage prints the capacity of this run",
                  "capacity: 2 peer pairs (4 allocations max" in usage, usage[-300:])
            check("--usage prints how many allocations are in use",
                  "now:" in usage and "allocations in use" in usage, usage[-300:])
        finally:
            proc.kill()
            proc.wait()
            handle.close()
    finally:
        pass

    failed = [name for name, ok, _ in CHECKS if not ok]
    print("\n%d checks, %d failed" % (len(CHECKS), len(failed)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
