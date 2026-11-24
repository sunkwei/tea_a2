# -*- coding: utf-8 -*-
"""A device may only be commanded by the operator account it names.

The agent registers with ``--mqtt_oper_name`` (default ``zonekey``). The
broker resolves that name against its own ``mqtt_accounts``: a name that is not
an operator account there falls back to ``zonekey``. The operator account that
matches may list the device and command it; another operator account may not
see it at all.

This pins down the rule that the account alone does not say who may touch
which device: one device account is shared by many devices (one per customer
site), so the zone is what keeps the sites apart.

    python3 tests/smoke_mqtt_zone.py [build-dir]

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
import threading
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "sched"))
from mqtt_cli import MqttClient, MqttError  # noqa: E402  (the CLI's own client)

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


def free_port(low=0, high=0):
    """A free TCP port.

    With a range given, the port comes from it: the broker refuses to start
    when its relay pool is outside 49152-65535, so the pool this test hands
    it has to come from there. Ports are checked by binding them - a port
    that is free now can be taken by the next process that asks for one.
    """
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
            # The broker binds INADDR_ANY with SO_REUSEADDR, so the check has
            # to do the same: a port that is free on 127.0.0.1 can still be
            # taken on another address.
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("0.0.0.0", port))
            return port
        except OSError:
            continue
        finally:
            s.close()
    raise RuntimeError("no free port in %d-%d" % (low, high))


class BrokerLog(object):
    """The broker's output, read in the background.

    Two reasons: a pipe that nobody reads fills up and blocks the broker,
    and "the port accepts a connection" is not a readiness signal here - the
    ephemeral range is full of listeners of other processes, so a connect can
    succeed against something that is not our broker. The line the broker
    prints when it starts listening is the honest signal.
    """

    def __init__(self, proc):
        self.lines = []
        self._proc = proc
        self._thread = threading.Thread(target=self._pump)
        self._thread.daemon = True
        self._thread.start()

    def _pump(self):
        for raw in iter(self._proc.stdout.readline, b""):
            self.lines.append(raw.decode("utf-8", "replace"))

    def wait_for(self, needle, seconds=20):
        deadline = time.time() + seconds
        while time.time() < deadline:
            for line in list(self.lines):
                if needle in line:
                    return True
            if self._proc.poll() is not None:
                return False
            time.sleep(0.1)
        return False

    def text(self):
        return "".join(self.lines)


def add_accounts(db):
    """The accounts live in the broker's database; it reads it on every CONNECT."""
    conn = sqlite3.connect(db)
    for user, password, role in (("zoneA", "pwA", "operator"),
                                  ("zoneB", "pwB", "operator"),
                                  ("zonekey", "pwk", "operator"),
                                  ("dev", "pwd", "device")):
        conn.execute("INSERT OR REPLACE INTO mqtt_accounts(user, pass, role, enabled) "
                     "VALUES(?,?,?,1)", (user, password, role))
    conn.commit()
    conn.close()


def registration(name, oper_name):
    return json.dumps({
        "name": name,
        "version": "1.8.3",
        "ip": "10.0.0.1",
        "mac": "aabbccddeeff",
        "port": 8383,
        "oper_name": oper_name,
        "actions": ["tunnel.start", "tunnel.stop", "status"],
        "online": True,
        "ts": int(time.time()),
    })


def connect(port, user, password, client_id):
    client = MqttClient("127.0.0.1", port, user, password, client_id)
    client.connect()
    return client


def sees_registration(port, user, password, wanted, seconds=3.0):
    """Does this operator account get the registration of that device?"""
    client = connect(port, user, password, "probe-%s-%d" % (user, os.getpid()))
    try:
        client.subscribe(["mt_register/#"])
        deadline = time.time() + seconds
        while time.time() < deadline:
            item = client.wait_publish(max(0.1, deadline - time.time()))
            if not item:
                continue
            topic, payload = item
            if not topic.startswith("mt_register/"):
                continue
            try:
                data = json.loads(payload)
            except ValueError:
                continue
            if data.get("name") == wanted:
                return True
        return False
    finally:
        client.close()


def command_accepted(port, user, password, device, seconds=3.0):
    """May this operator account command that device? The broker closes the
    connection when it may not."""
    client = connect(port, user, password, "probe-cmd-%s-%d" % (user, os.getpid()))
    try:
        client.subscribe(["mt_control/+/resp"])
        client.publish("mt_control/%s/cmd" % device,
                       json.dumps({"id": "probe-1", "action": "status", "ts": int(time.time())}))
        deadline = time.time() + seconds
        while time.time() < deadline:
            try:
                client.wait_publish(max(0.2, deadline - time.time()))
            except MqttError:
                return False  # the broker hung up: the command was refused
        return True
    finally:
        client.close()


def register_device(port, client_id, oper_name, seconds=3.0):
    """A device (re)connects and reports the operator that manages it."""
    conn = connect(port, "dev", "pwd", client_id)
    conn.publish("mt_register/" + client_id, registration(client_id, oper_name),
                 retain=True)
    time.sleep(0.4)
    conn.close()
    time.sleep(0.4)


def zone_of(db, client_id):
    conn = sqlite3.connect(db)
    try:
        row = conn.execute("SELECT oper_name FROM mqtt_devices WHERE client_id = ?",
                           (client_id,)).fetchone()
        return row[0] if row and row[0] else ""
    finally:
        conn.close()


def main():
    build = sys.argv[1] if len(sys.argv) > 1 else os.path.join(REPO_ROOT, "build-mingw")
    exe = find_binary(build, "a2_broker")
    if not exe:
        print("no a2_broker in " + build)
        return 1

    work = tempfile.mkdtemp(prefix="a2-zone-")
    db = os.path.join(work, "a2_broker.db")
    # The signal and STUN ports come from below the ephemeral range
    # (32768-60999 by default): inside it, other processes on this machine
    # hold listeners on ports that look free, and the broker then cannot
    # bind them.
    port = free_port(20000, 32000)
    stun = free_port(20000, 32000)
    # The relay pool has to sit inside 49152-65535 or the broker refuses to
    # start ("the relay port pool must be inside ..."). The pool is never
    # bound here: this test relays nothing.
    relay = free_port(49200, 65400)
    env = dict(os.environ)
    env["TEA_AGENT_HOME"] = work
    cmd = [exe, "--mqtt", "--signal-port", str(port), "--stun-port", str(stun),
           "--max-peer-pair", "2",
           "--relay-begin", str(relay), "--relay-end", str(relay + 4),
           "--turn-db", db]
    proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    log = BrokerLog(proc)

    try:
        if not log.wait_for("signalling on tcp/%d" % port):
            print("the broker did not come up on port %d" % port)
            print(log.text())
            return 1
        add_accounts(db)

        # A device that names an operator account that exists.
        device_a = "devA@10.0.0.1-aabbccddeeff"
        client = connect(port, "dev", "pwd", device_a)
        try:
            client.publish("mt_register/" + device_a, registration(device_a, "zoneA"), retain=True)
            time.sleep(0.6)
        finally:
            client.close()
        check("the device of zone A is stored under zone A",
              zone_of(db, device_a) == "zoneA", zone_of(db, device_a))

        # A device that names an operator account that does not exist.
        device_b = "devB@10.0.0.2-bbccddeeff00"
        client = connect(port, "dev", "pwd", device_b)
        try:
            client.publish("mt_register/" + device_b, registration(device_b, "nosuchoperator"),
                           retain=True)
            time.sleep(0.6)
        finally:
            client.close()
        check("an unknown operator name falls back to zonekey",
              zone_of(db, device_b) == "zonekey", zone_of(db, device_b))

        # An agent older than 1.8.4 does not report an operator at all: it
        # registers without the field and stays in the default zone.
        device_old = "devOld@10.0.0.3-ccddeeff0011"
        client = connect(port, "dev", "pwd", device_old)
        try:
            payload = json.loads(registration(device_old, ""))
            del payload["oper_name"]
            client.publish("mt_register/" + device_old, json.dumps(payload), retain=True)
            time.sleep(0.6)
        finally:
            client.close()
        check("a device that reports no operator (an older a2) stays in zonekey",
              zone_of(db, device_old) == "zonekey", zone_of(db, device_old))
        check("the zonekey operator sees the device that reported no operator",
              sees_registration(port, "zonekey", "pwk", device_old))

        check("the operator of zone A sees the device of zone A",
              sees_registration(port, "zoneA", "pwA", device_a))
        check("the operator of zone B does not see the device of zone A",
              not sees_registration(port, "zoneB", "pwB", device_a))
        check("the operator of zonekey sees the device that fell back to it",
              sees_registration(port, "zonekey", "pwk", device_b))
        check("the operator of zone A does not see the device of zonekey",
              not sees_registration(port, "zoneA", "pwA", device_b))

        check("the operator of zone A may command the device of zone A",
              command_accepted(port, "zoneA", "pwA", device_a))
        check("the operator of zone B may not command the device of zone A",
              not command_accepted(port, "zoneB", "pwB", device_a))
        check("the operator of zonekey may command the device that fell back to it",
              command_accepted(port, "zonekey", "pwk", device_b))

        # The group belongs to the device: it reports the operator. When it
        # comes back under another name, the broker moves it.
        register_device(port, device_a, "zoneB")
        check("a device that reports another operator moves to that group",
              sees_registration(port, "zoneB", "pwB", device_a) and
              not sees_registration(port, "zoneA", "pwA", device_a))
        check("the moved device is stored under the new operator",
              zone_of(db, device_a) == "zoneB",
              "stored as %s" % zone_of(db, device_a))
        check("the old operator may not command the moved device",
              not command_accepted(port, "zoneA", "pwA", device_a))
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()

    failed = [name for name, ok in results if not ok]
    print("")
    print("%d/%d checks passed" % (len(results) - len(failed), len(results)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
