# -*- coding: utf-8 -*-
"""A tunnel nobody uses gives itself up.

A tunnel that carries no data holds a process, a broker room, a TURN
allocation and a relay port. The sidecar therefore counts the seconds since
the last byte and leaves when they run out:

    a2_tunnel --idle-timeout 1800     # the default: 30 minutes
    a2_tunnel --idle-timeout 0        # never

The window slides: every byte that goes through the tunnel starts it over.
The sidecar that leaves this way exits with the code 3, so the other end can
tell "the operator walked away" from "the sidecar crashed".

    python3 tests/smoke_tunnel_idle.py [build-dir]

Local only: a throwaway broker in a temp directory, no TURN traffic.
"""
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
results = []

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


def check(name, ok, detail=""):
    results.append((name, bool(ok)))
    print(("PASS " if ok else "FAIL ") + name + (("  " + str(detail)) if (detail and not ok) else ""))


def find_binary(build, name):
    for candidate in (os.path.join(build, "bin", name + ".exe"), os.path.join(build, "bin", name),
                      os.path.join(build, name + ".exe"), os.path.join(build, name)):
        if os.path.isfile(candidate):
            return candidate
    return ""


def find_build(argument):
    """Where the tunnel binaries are.

    The suite hands every test the agent binary; the build directory is what
    holds a2_broker and a2_tunnel next to it. The path is absolute: the test
    runs its children in a temp directory.
    """
    if os.path.isdir(argument):
        return os.path.abspath(argument)
    parent = os.path.dirname(os.path.abspath(argument))
    if os.path.basename(parent) == "bin":
        return os.path.dirname(parent)
    return os.path.join(REPO_ROOT, "build")


def free_port():
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def wait_for_log(path, pattern, seconds=20.0):
    deadline = time.time() + seconds
    rx = re.compile(pattern)
    while time.time() < deadline:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                text = handle.read()
        except (IOError, OSError):
            text = ""
        match = rx.search(text)
        if match:
            return match
        time.sleep(0.2)
    return None


def log_text(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except (IOError, OSError):
        return ""


def main():
    build = find_build(sys.argv[1] if len(sys.argv) > 1 else "")
    broker = find_binary(build, "a2_broker")
    sidecar = find_binary(build, "a2_tunnel")
    if not broker or not sidecar:
        print("skip  (no a2_broker / a2_tunnel in %s)" % build)
        return 0

    work = tempfile.mkdtemp(prefix="smoke-tunnel-idle-")
    home = os.path.join(work, "home")
    os.makedirs(home)
    signal_port = free_port()
    stun_port = free_port()
    a2_port = free_port()
    client_port = free_port()
    env = dict(os.environ)
    env["HOME"] = home
    env["TEA_AGENT_HOME"] = home

    procs = []

    def spawn(args, log_name):
        path = os.path.join(work, log_name)
        handle = open(path, "wb")
        proc = subprocess.Popen(args, stdout=handle, stderr=subprocess.STDOUT, env=env, cwd=work)
        procs.append((proc, path))
        return proc, path

    try:
        script = os.path.join(work, "fake_a2.py")
        with open(script, "w", encoding="utf-8") as handle:
            handle.write(FAKE_A2)
        spawn([sys.executable, script, str(a2_port)], "a2.log")
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", a2_port), timeout=1):
                    break
            except OSError:
                time.sleep(0.2)

        broker_log = os.path.join(work, "broker.log")
        spawn([broker, "--stun-port", str(stun_port), "--signal-port", str(signal_port),
               "--signal-bind", "127.0.0.1", "--cert-name", "test-broker"], "broker.log")
        if not wait_for_log(broker_log, r"signalling on tcp/%d" % signal_port):
            print("the broker did not come up")
            print(log_text(broker_log))
            return 1

        common = [sidecar, "--broker", "127.0.0.1:%d" % signal_port,
                  "--stun", "127.0.0.1:%d" % stun_port, "--turn", "127.0.0.1:%d" % stun_port,
                  "--target", "127.0.0.1:%d" % a2_port]

        # ---- an idle tunnel gives itself up --------------------------------
        agent_log = os.path.join(work, "agent-idle.log")
        agent, agent_path = spawn(common + ["--role", "agent", "--wait-timeout", "0",
                                            "--idle-timeout", "5"], "agent-idle.log")
        deadline = time.time() + 40
        while time.time() < deadline and agent.poll() is None:
            time.sleep(0.3)
        check("a tunnel without traffic gives itself up", agent.poll() is not None,
              "the sidecar is still running")
        check("it leaves with the code that says 'idle'", agent.poll() == 3,
              "exit code %s" % agent.poll())
        check("it says why it leaves", "no data for" in log_text(agent_path),
              log_text(agent_path)[-300:])

        # ---- --idle-timeout 0 means never ----------------------------------
        keep_log = os.path.join(work, "agent-keep.log")
        keep, keep_path = spawn(common + ["--role", "agent", "--wait-timeout", "0",
                                          "--idle-timeout", "0"], "agent-keep.log")
        time.sleep(10)
        check("--idle-timeout 0 keeps the tunnel", keep.poll() is None,
              "exit code %s" % keep.poll())
        keep.terminate()

        # ---- traffic keeps the tunnel alive --------------------------------
        agent2_log = os.path.join(work, "agent-busy.log")
        agent2, agent2_path = spawn(common + ["--role", "agent", "--wait-timeout", "0",
                                              "--idle-timeout", "6"], "agent-busy.log")
        match = wait_for_log(agent2_path, r"a2_tunnel --role client --session ([0-9a-f]+)")
        if not match:
            check("the agent side prints a session code", False, log_text(agent2_path)[-300:])
            return 1
        session = match.group(1)
        client_log = os.path.join(work, "client.log")
        spawn([sidecar, "--role", "client", "--session", session,
               "--broker", "127.0.0.1:%d" % signal_port,
               "--stun", "127.0.0.1:%d" % stun_port, "--turn", "127.0.0.1:%d" % stun_port,
               "--port", str(client_port)], "client.log")

        answered = 0
        deadline = time.time() + 30
        while time.time() < deadline and answered == 0:
            try:
                with urllib.request.urlopen("http://127.0.0.1:%d/api/health" % client_port,
                                            timeout=3) as response:
                    body = response.read().decode("utf-8", "replace")
                if "fake-a2" in body:
                    answered = 1
            except Exception:
                time.sleep(0.5)
        check("the tunnel carries bytes", answered == 1)

        # Keep it busy for longer than the idle timeout.
        end = time.time() + 12
        while time.time() < end:
            try:
                with urllib.request.urlopen("http://127.0.0.1:%d/api/health" % client_port,
                                            timeout=3) as response:
                    response.read()
            except Exception:
                pass
            time.sleep(1.5)
        check("a tunnel with traffic stays", agent2.poll() is None,
              "exit code %s" % agent2.poll())
    finally:
        for proc, _ in procs:
            try:
                proc.terminate()
            except OSError:
                pass
        for proc, _ in procs:
            try:
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except OSError:
                    pass

    failed = [name for name, ok in results if not ok]
    print("\n%d checks, %d failed" % (len(results), len(failed)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
