# -*- coding: utf-8 -*-
"""One device, several tunnels at the same time.

A device may run up to four tunnels; every wake makes a new one with its own
session code, and the session is the handle: `stop` ends the tunnel with that
session and leaves the others alone.

The rules this pins down:

  * two wakes give two different sessions, and both tunnels work at once;
  * the fifth wake is refused with a clear reason, and the answer says what
    is running;
  * `status` lists every tunnel of the device, or one of them by session;
  * `stop` needs a session: without one the device refuses, and a session of
    another device touches nothing;
  * the same command id twice is one tunnel (MQTT delivers at least once).

    python3 tests/smoke_mqtt_multi.py [build-dir]

Local only: a throwaway broker in a temp directory, no TURN traffic.
"""
import json
import os
import random
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "sched"))
from mqtt_cli import MqttClient, MqttError  # noqa: E402  (the CLI's own client)

results = []


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
    if os.path.isdir(argument):
        return os.path.abspath(argument)
    parent = os.path.dirname(os.path.abspath(argument))
    if os.path.basename(parent) == "bin":
        return os.path.dirname(parent)
    return os.path.join(REPO_ROOT, "build")


def free_port(low=0, high=0):
    for _ in range(200):
        if high:
            port = random.randint(low, high)
        else:
            s = socket.socket()
            try:
                s.bind(("127.0.0.1", 0))
                port = s.getsockname()[1]
            finally:
                s.close()
            return port
        s = socket.socket()
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("0.0.0.0", port))
            return port
        except OSError:
            continue
        finally:
            s.close()
    raise RuntimeError("no free port")


def add_accounts(db):
    conn = sqlite3.connect(db)
    for user, password, role in (("zonekey", "pwk", "operator"), ("dev", "pwd", "device")):
        conn.execute("INSERT OR REPLACE INTO mqtt_accounts(user, pass, role, enabled) "
                     "VALUES(?,?,?,1)", (user, password, role))
    conn.commit()
    conn.close()


def count_sidecars():
    """How many a2_tunnel processes are running on this machine."""
    if not os.path.isdir("/proc"):
        return -1
    count = 0
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open("/proc/%s/comm" % entry, "r", encoding="utf-8", errors="replace") as handle:
                if handle.read().strip() == "a2_tunnel":
                    count += 1
        except (IOError, OSError):
            continue
    return count


def wait_health(port, seconds=25.0):
    import urllib.request
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/api/health" % port, timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.3)
    return False


class Operator(object):
    """The operator side of the control channel."""

    def __init__(self, port, name):
        self.device = name
        self.client = MqttClient("127.0.0.1", port, "zonekey", "pwk",
                                 "op-%d-%04x" % (os.getpid(), random.randint(0, 0xFFFF)))
        self.client.connect()
        self.client.subscribe(["mt_control/+/resp"])
        self.counter = 0

    def close(self):
        self.client.close()

    def command(self, action, args=None, command_id=None, timeout=40):
        if command_id is None:
            self.counter += 1
            command_id = "c-%d-%04x" % (int(time.time()), self.counter)
        payload = json.dumps({"id": command_id, "action": action, "args": args or {},
                              "ts": int(time.time()), "ttl": 120})
        self.client.publish("mt_control/%s/cmd" % self.device, payload)
        deadline = time.time() + timeout
        while time.time() < deadline:
            item = self.client.wait_publish(max(0.1, deadline - time.time()))
            if not item:
                continue
            topic, body = item
            if topic != "mt_control/%s/resp" % self.device:
                continue
            try:
                data = json.loads(body)
            except ValueError:
                continue
            if data.get("id") != command_id:
                continue
            if data.get("state") in ("ready", "stopped", "failed", "rejected", "status"):
                return data
        raise RuntimeError("no answer for %s within %ds" % (command_id, timeout))


def find_device(port, seconds=25.0):
    """The name the device registered under."""
    client = MqttClient("127.0.0.1", port, "zonekey", "pwk", "probe-%d" % os.getpid())
    try:
        client.connect()
        client.subscribe(["mt_register/#"])
        deadline = time.time() + seconds
        while time.time() < deadline:
            item = client.wait_publish(max(0.1, deadline - time.time()))
            if not item:
                continue
            topic, body = item
            if not topic.startswith("mt_register/"):
                continue
            try:
                data = json.loads(body)
            except ValueError:
                continue
            if data.get("online"):
                return data.get("name")
        return ""
    finally:
        client.close()


def main():
    build = find_build(sys.argv[1] if len(sys.argv) > 1 else "")
    agent = find_binary(build, "tea_agent")
    broker = find_binary(build, "a2_broker")
    if not agent or not broker:
        print("skip  (no tea_agent / a2_broker in %s)" % build)
        return 0

    work = tempfile.mkdtemp(prefix="smoke-mqtt-multi-")
    home = os.path.join(work, "home")
    os.makedirs(home)
    broker_port = free_port(20000, 32000)
    stun_port = free_port(20000, 32000)
    relay = free_port(49200, 65400)
    a2_port = free_port()
    db = os.path.join(work, "a2_broker.db")
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

    sidecars_before = count_sidecars()
    try:
        # ---- broker ------------------------------------------------------
        broker_log = os.path.join(work, "broker.log")
        spawn([broker, "--mqtt", "--signal-port", str(broker_port), "--stun-port", str(stun_port),
               "--max-peer-pair", "8", "--relay-begin", str(relay),
               "--relay-end", str(relay + 16),
               "--turn-db", db], "broker.log")
        deadline = time.time() + 25
        while time.time() < deadline:
            try:
                with open(broker_log, "r", encoding="utf-8", errors="replace") as handle:
                    if "signalling on tcp/%d" % broker_port in handle.read():
                        break
            except (IOError, OSError):
                pass
            time.sleep(0.3)
        add_accounts(db)

        # ---- the device ---------------------------------------------------
        config = os.path.join(work, "provider.yaml")
        with open(config, "w", encoding="utf-8") as handle:
            handle.write("""version: 1
providers:
  mock:
    api_url: http://127.0.0.1:1
    api_key: sk-test
    default_model: m
    models:
      m:
        max_context_tokens: 32768
        max_output_tokens: 4096
""")
        agent_log = os.path.join(work, "a2.log")
        spawn([agent, "--config", config, "--tea-home", home, "--host", "127.0.0.1",
               "--port", str(a2_port), "--no-console", "--log-file", agent_log,
               "--mqtt", "127.0.0.1:%d" % broker_port, "--mqtt_user", "dev",
               "--mqtt_pass", "pwd", "--mqtt_name", "testdev",
               "--mqtt_oper_name", "zonekey",
               "--tunnel-broker", "127.0.0.1:%d" % broker_port,
               "--mqtt_idle_timeout", "30"], "a2.log")
        if not wait_health(a2_port):
            print("the agent did not come up")
            with open(agent_log, "r", encoding="utf-8", errors="replace") as handle:
                print(handle.read()[-2000:])
            return 1

        device = find_device(broker_port)
        check("the device registers itself", bool(device), device)
        if not device:
            return 1

        operator = Operator(broker_port, device)
        try:
            # ---- two tunnels at the same time ----------------------------
            first = operator.command("tunnel.start", {"wait_timeout": 600})
            second = operator.command("tunnel.start", {"wait_timeout": 600})
            check("the first wake is ready", first.get("state") == "ready", first)
            check("the second wake is ready", second.get("state") == "ready", second)
            check("every wake makes a new session",
                  bool(first.get("session")) and first.get("session") != second.get("session"),
                  "%s vs %s" % (first.get("session"), second.get("session")))

            # ---- the same command id twice is one tunnel -------------------
            # MQTT delivers at least once: a redelivered wake must not make
            # a second tunnel.
            once = operator.command("tunnel.start", {"wait_timeout": 600},
                                    command_id="same-id")
            twice = operator.command("tunnel.start", {"wait_timeout": 600},
                                     command_id="same-id")
            check("a command id is answered once",
                  once.get("state") == "ready" and once.get("session"), once)
            check("the redelivered command gives the same session",
                  twice.get("session") == once.get("session"), "%s / %s" % (once, twice))
            counted = operator.command("status")
            check("the redelivered command made no second tunnel",
                  len(counted.get("tunnels") or []) == 3, counted)

            # ---- up to four ------------------------------------------------
            third = operator.command("tunnel.start", {"wait_timeout": 600})
            fourth = operator.command("tunnel.start", {"wait_timeout": 600})
            check("the device holds four tunnels",
                  third.get("state") == "ready" and fourth.get("state") == "rejected",
                  "%s / %s" % (third.get("state"), fourth.get("state")))

            fifth = fourth
            check("the fifth wake is refused", fifth.get("state") == "rejected", fifth)
            check("the refusal says why", fifth.get("error") == "too_many_tunnels", fifth)
            check("the refusal lists the tunnels", len(fifth.get("tunnels") or []) == 4, fifth)

            # ---- status ----------------------------------------------------
            listing = operator.command("status")
            check("status lists every tunnel", len(listing.get("tunnels") or []) == 4, listing)
            one = operator.command("status", {"session": first.get("session")})
            check("status answers about one tunnel",
                  one.get("session") == first.get("session") and one.get("tunnel") == "ready", one)
            unknown = operator.command("status", {"session": "0" * 32})
            check("status says when it does not know a session",
                  unknown.get("error") == "unknown_session", unknown)

            # ---- stop ------------------------------------------------------
            without = operator.command("tunnel.stop")
            check("stop without a session is refused",
                  without.get("state") == "rejected" and without.get("error") == "session_required",
                  without)
            foreign = operator.command("tunnel.stop", {"session": "f" * 32})
            check("stop with an unknown session is refused",
                  foreign.get("state") == "rejected" and foreign.get("error") == "unknown_session",
                  foreign)

            stopped = operator.command("tunnel.stop", {"session": first.get("session")})
            check("stop ends the tunnel with that session",
                  stopped.get("state") == "stopped", stopped)
            after = operator.command("status")
            check("the other tunnels are still there", len(after.get("tunnels") or []) == 3, after)

            # ---- what the device tells its own api ------------------------
            snapshot = {}
            try:
                with urllib.request.urlopen("http://127.0.0.1:%d/api/status" % a2_port,
                                            timeout=5) as response:
                    snapshot = json.loads(response.read().decode("utf-8", "replace"))
            except Exception as exc:
                snapshot = {"error": str(exc)}
            mqtt_status = snapshot.get("mqtt") or {}
            check("the api lists the tunnels", len(mqtt_status.get("tunnels") or []) == 3, mqtt_status)
            check("the api keeps the old single field", mqtt_status.get("tunnel") == "ready", mqtt_status)
            check("the api says why the last tunnel ended",
                  mqtt_status.get("last_exit_reason") == "stopped", mqtt_status)
            still = operator.command("status", {"session": second.get("session")})
            check("the other tunnel is untouched", still.get("tunnel") == "ready", still)

            # ---- reuse -----------------------------------------------------
            reused = operator.command("tunnel.start", {"reuse": True})
            check("reuse hands out a tunnel that is already running",
                  reused.get("state") == "ready" and reused.get("session") == second.get("session"),
                  reused)
        finally:
            operator.close()

        # ---- the command line tool -------------------------------------------
        cli = [sys.executable, os.path.join(REPO_ROOT, "sched", "mqtt_cli.py"),
               "--broker", "127.0.0.1:%d" % broker_port, "--user", "zonekey", "--pass", "pwk"]

        def run_cli(args):
            proc = subprocess.Popen(cli + args, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, env=env)
            out, _ = proc.communicate(timeout=90)
            return proc.returncode, out.decode("utf-8", "replace")

        code, out = run_cli(["status", device])
        check("the cli lists the tunnels", code == 0 and "tunnels :" in out, out)
        code, out = run_cli(["stop", device])
        check("the cli refuses to stop without a session",
              code == 1 and "--session is required" in out, out)
        code, out = run_cli(["stop", device, "--session", "0" * 32])
        check("the cli says when the session is unknown",
              code == 1 and "does not know that session" in out, out)
        code, out = run_cli(["wake", device, "--wait-timeout", "600"])
        check("the cli wakes a tunnel and prints the session",
              code == 0 and "session :" in out and "--port" in out, out)
    finally:
        for proc, _ in procs:
            try:
                proc.terminate()
            except OSError:
                pass
        for proc, _ in procs:
            try:
                proc.wait(timeout=8)
            except Exception:
                try:
                    proc.kill()
                except OSError:
                    pass

    # The sidecars the agent started die with it: none may be left behind.
    if sidecars_before >= 0:
        deadline = time.time() + 20
        left = count_sidecars()
        while left > sidecars_before and time.time() < deadline:
            time.sleep(0.5)
            left = count_sidecars()
        check("no sidecar is left behind when the agent stops", left <= sidecars_before,
              "%d before, %d after" % (sidecars_before, left))

    failed = [name for name, ok in results if not ok]
    print("\n%d checks, %d failed" % (len(results), len(failed)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
