# -*- coding: utf-8 -*-
"""A silent sidecar is dropped, a quiet-but-alive one is not.

The broker cannot see a half-open link: nobody sends anything, so nothing times
out (the signalling read blocks for ever and nothing sets SO_KEEPALIVE). What it
can see is silence, because the sidecars ping:

    -> {"type":"ping"}
    <- {"type":"pong"}

and a peer that has said nothing for `--peer-idle-timeout` seconds is dropped
(600 = ten minutes by default; 0 = never). Ten minutes and not one: a link that
comes back inside that window is still the same TCP connection, and dropping it
would only renegotiate a tunnel that works.

    python3 tests/smoke_broker_idle.py [build-dir]

Local only: a throwaway broker in a temp directory, no TURN traffic.
"""
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
results = []


def check(name, ok, detail=""):
    results.append((name, ok))
    print(("PASS " if ok else "FAIL ") + name + (("  " + str(detail)) if (detail and not ok) else ""))


def find_binary(build, name):
    for candidate in (os.path.join(build, "bin", name + ".exe"), os.path.join(build, "bin", name),
                      os.path.join(build, name + ".exe"), os.path.join(build, name)):
        if os.path.isfile(candidate):
            return candidate
    return ""


def free_port():
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


class Line(object):
    """A signalling connection: newline-delimited JSON over TCP."""

    def __init__(self, port):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        self.sock.settimeout(10)
        self.buf = b""

    def send(self, obj):
        self.sock.sendall((json.dumps(obj) + "\n").encode("utf-8"))

    def recv_line(self, timeout=5.0):
        """The next line, or None on EOF/timeout."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            pos = self.buf.find(b"\n")
            if pos >= 0:
                line = self.buf[:pos]
                self.buf = self.buf[pos + 1:]
                return json.loads(line.decode("utf-8"))
            self.sock.settimeout(max(0.1, deadline - time.time()))
            try:
                chunk = self.sock.recv(4096)
            except socket.timeout:
                return None
            if not chunk:
                return None
            self.buf += chunk
        return None

    def closed(self, timeout=5.0):
        """Wait for the peer to hang up."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.sock.settimeout(max(0.1, deadline - time.time()))
            try:
                chunk = self.sock.recv(4096)
            except socket.timeout:
                return False
            if not chunk:
                return True
        return False

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def wait_log(path, pattern, timeout=20.0):
    deadline = time.time() + timeout
    text = ""
    while time.time() < deadline:
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                text = handle.read()
            if re.search(pattern, text):
                return text
        time.sleep(0.2)
    return text


def main():
    build = os.path.join(REPO_ROOT, "build-mingw")
    if len(sys.argv) > 1:
        build = sys.argv[1]
    broker = find_binary(build, "a2_broker")
    if not broker:
        print("skip  (no a2_broker in %s)" % build)
        return 0

    work = tempfile.mkdtemp(prefix="smoke-broker-idle-")
    home = os.path.join(work, "home")
    os.makedirs(home)
    env = dict(os.environ)
    env["HOME"] = home
    env["TEA_AGENT_HOME"] = home

    stun_port = free_port()
    signal_port = free_port()
    log = os.path.join(work, "broker.log")
    handle = open(log, "wb")
    proc = subprocess.Popen(
        [broker, "--no-tls", "--stun-port", str(stun_port),
         "--signal-port", str(signal_port), "--signal-bind", "127.0.0.1",
         "--max-peer-pair", "2", "--relay-begin", "49160", "--relay-end", "49180",
         "--turn-db", os.path.join(work, "a2_broker.db"),
         "--peer-idle-timeout", "3"],
        stdout=handle, stderr=subprocess.STDOUT, env=env, cwd=work)
    a = b = c = None
    try:
        wait_log(log, r"signalling on tcp/%d" % signal_port)

        # A joins and pings once - that is what marks it as a peer the sweep may
        # drop - and then goes silent.
        a = Line(signal_port)
        a.send({"room": "r1", "token": "t1", "role": "agent", "register": True})
        joined = a.recv_line()
        check("the first side joins an empty room",
              joined and joined.get("type") == "joined" and joined.get("peer") is False, joined)
        a.send({"type": "ping"})
        pong = a.recv_line()
        check("a ping is answered", pong and pong.get("type") == "pong", pong)

        # B joins, sees A, and keeps pinging.
        b = Line(signal_port)
        b.send({"room": "r1", "token": "t1", "role": "client"})
        joined_b = b.recv_line()
        check("the second side sees the first one",
              joined_b and joined_b.get("peer") is True, joined_b)
        notice = a.recv_line()
        check("the first side is told a peer arrived",
              notice and notice.get("type") == "peer_joined", notice)

        # A stays silent (the half-open case); B keeps saying something.
        dropped = False
        pings = 0
        deadline = time.time() + 20
        while time.time() < deadline:
            b.send({"type": "ping"})
            reply = b.recv_line(timeout=2.0)
            if not (reply and reply.get("type") == "pong"):
                break
            pings += 1
            if a.closed(timeout=0.3):
                dropped = True
                break
        check("the silent peer is dropped after the idle timeout", dropped or a.closed(timeout=2.0))
        check("the peer that keeps pinging is kept", pings > 0, pings)

        text = wait_log(log, r"has been silent for", timeout=5)
        check("the broker says why it dropped it", "has been silent for" in text, text[-300:])

        # The room is usable again: a fresh agent side sees the remaining peer.
        b.send({"type": "ping"})
        check("the surviving peer still talks", (b.recv_line() or {}).get("type") == "pong")
        c = Line(signal_port)
        c.send({"room": "r1", "token": "t1", "role": "agent", "register": True})
        joined_c = c.recv_line()
        check("a new side finds the room in a sane state",
              joined_c and joined_c.get("type") == "joined" and joined_c.get("peer") is True,
              joined_c)
    finally:
        for conn in (a, b, c):
            if conn is not None:
                conn.close()
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        handle.close()

    failed = [name for name, ok in results if not ok]
    print("")
    print("%d/%d checks passed" % (len(results) - len(failed), len(results)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
