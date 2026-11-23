#!/usr/bin/env python3
# Copyright (c) 2026 sunkwei
# SPDX-License-Identifier: GPL-3.0-or-later
"""a2 MQTT tools: list the devices that are online, wake a tunnel, read it back.

    python3 sched/mqtt_cli.py list [--all] [--json]
    python3 sched/mqtt_cli.py wake <device_name> [--wait-timeout 1800] [--timeout 60]
    python3 sched/mqtt_cli.py stop <device_name>
    python3 sched/mqtt_cli.py status <device_name>

The broker is ours by default (103.232.92.54:8382). Everything can be
overridden by flags or by the same names in the environment:

    TEA_MQTT_BROKER    host:port
    TEA_MQTT_USER      operator account
    TEA_MQTT_PASS      its password (keep it out of the command line)

One file, standard library only, Python 3.6+. It speaks the same small MQTT
3.1.1 subset the agent does: CONNECT/SUBSCRIBE/PUBLISH/PUBACK/PINGREQ.

TLS is encryption only: the broker's self-signed certificate is not verified
(no CA file, no pinning). The password is the gate. Use it on trusted
networks; a man in the middle could otherwise impersonate the broker.
"""

import argparse
import json
import os
import random
import socket
import ssl
import struct
import sys
import time

DEFAULT_BROKER = "103.232.92.54:8382"
MAX_PACKET = 300 * 1024


# --------------------------------------------------------------------- mqtt
def frame(kind, flags, body):
    out = bytearray([(kind << 4) | flags])
    value = len(body)
    while True:
        byte = value % 128
        value //= 128
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            break
    out.extend(body)
    return bytes(out)


def enc_str(text):
    data = text.encode("utf-8")
    return struct.pack("!H", len(data)) + data


class MqttError(Exception):
    pass


class MqttClient(object):
    def __init__(self, host, port, user, password, client_id, connect_timeout=10):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.client_id = client_id
        self.connect_timeout = connect_timeout
        self.sock = None
        self.buffer = b""
        self.packet_id = 0
        self.next_ping = time.time() + 10

    # -- plumbing
    def _next_id(self):
        self.packet_id = (self.packet_id % 65535) + 1
        return self.packet_id

    def _send(self, data):
        self.sock.sendall(data)

    def connect(self):
        try:
            raw = socket.create_connection((self.host, self.port), timeout=self.connect_timeout)
        except socket.timeout:
            raise MqttError("cannot reach %s:%d (timed out)" % (self.host, self.port))
        except OSError as exc:
            raise MqttError("cannot reach %s:%d (%s)" % (self.host, self.port, exc))
        # Encryption only, no certificate check: the broker is ours and the
        # password is what gates it. Keep this tool on trusted networks.
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        try:
            self.sock = context.wrap_socket(raw, server_hostname=self.host)
        except ssl.SSLError as exc:
            raw.close()
            raise MqttError("TLS failed: %s" % exc)
        self.sock.settimeout(1.0)

        flags = 0x02
        if self.user:
            flags |= 0x80
        if self.password:
            flags |= 0x40
        body = enc_str("MQTT") + bytes([4, flags]) + struct.pack("!H", 30)
        body += enc_str(self.client_id)
        if self.user:
            body += enc_str(self.user)
        if self.password:
            body += enc_str(self.password)
        self._send(frame(1, 0, body))
        packet = self._read_packet(10)
        if packet is None or packet[0] != 2:
            raise MqttError("the broker did not answer CONNECT")
        code = packet[2][1] if len(packet[2]) > 1 else 255
        if code != 0:
            if code == 4:
                raise MqttError("the broker refused the credentials (wrong user or password)")
            if code == 5:
                raise MqttError("the broker refused the connection (not authorized)")
            raise MqttError("the broker refused the connection (CONNACK %d)" % code)

    def subscribe(self, filters):
        body = struct.pack("!H", self._next_id())
        for item in filters:
            body += enc_str(item) + bytes([1])
        self._send(frame(8, 2, body))
        packet = self._read_packet(5)
        if packet is None or packet[0] != 9:
            raise MqttError("the broker did not acknowledge the subscription")
        if len(packet[2]) > 2 and packet[2][2] == 0x80:
            raise MqttError("the broker refused the subscription to %s" % ", ".join(filters))

    def publish(self, topic, payload, qos=1):
        body = enc_str(topic)
        if qos:
            body += struct.pack("!H", self._next_id())
        body += payload.encode("utf-8")
        self._send(frame(3, qos << 1, body))

    def close(self):
        try:
            if self.sock:
                self._send(frame(14, 0, b""))
        except OSError:
            pass
        try:
            if self.sock:
                self.sock.close()
        except OSError:
            pass

    # -- reading
    def _take(self):
        if len(self.buffer) < 2:
            return None
        value = 0
        multiplier = 1
        index = 1
        while True:
            if index >= len(self.buffer):
                return None
            byte = self.buffer[index]
            index += 1
            value += (byte & 0x7F) * multiplier
            if not (byte & 0x80):
                break
            multiplier *= 128
            if index > 4:
                raise MqttError("malformed packet from the broker")
        if value > MAX_PACKET:
            raise MqttError("the broker sent a packet that is too large")
        if len(self.buffer) < index + value:
            return None
        kind = self.buffer[0] >> 4
        flags = self.buffer[0] & 0x0F
        body = self.buffer[index:index + value]
        self.buffer = self.buffer[index + value:]
        return kind, flags, body

    def _read_packet(self, timeout):
        deadline = time.time() + timeout
        while True:
            packet = self._take()
            if packet is not None:
                return packet
            if time.time() >= deadline:
                return None
            self._keepalive()
            try:
                chunk = self.sock.recv(4096)
            except socket.timeout:
                continue
            except OSError as exc:
                raise MqttError("connection lost (%s)" % exc)
            if not chunk:
                raise MqttError("the broker closed the connection")
            self.buffer += chunk

    def _keepalive(self):
        # The broker drops a silent client after 1.5 x keepalive (30 s).
        if time.time() >= self.next_ping:
            try:
                self._send(frame(12, 0, b""))
            except OSError:
                pass
            self.next_ping = time.time() + 10

    def wait_publish(self, timeout):
        """One (topic, payload) from the broker, or None."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            packet = self._read_packet(max(0.1, deadline - time.time()))
            if packet is None:
                return None
            kind, flags, body = packet
            if kind != 3:
                continue
            if len(body) < 2:
                continue
            length = struct.unpack("!H", body[:2])[0]
            if len(body) < 2 + length:
                continue
            topic = body[2:2 + length].decode("utf-8", "replace")
            index = 2 + length
            qos = (flags >> 1) & 3
            if qos:
                index += 2
            return topic, body[index:].decode("utf-8", "replace")
        return None


# ------------------------------------------------------------------- config
def split_endpoint(text):
    if ":" not in text:
        raise SystemExit("error: --broker needs HOST:PORT (got %r)" % text)
    host, _, port = text.rpartition(":")
    try:
        return host, int(port)
    except ValueError:
        raise SystemExit("error: --broker needs HOST:PORT (got %r)" % text)


def build_client(args):
    endpoint = args.broker or os.environ.get("TEA_MQTT_BROKER") or DEFAULT_BROKER
    host, port = split_endpoint(endpoint)
    user = args.user or os.environ.get("TEA_MQTT_USER") or ""
    password = args.password or os.environ.get("TEA_MQTT_PASS") or ""
    if not user or not password:
        # No account is built into this tool on purpose: a helper that ships a
        # password is a password that leaks the moment it is copied around.
        raise SystemExit("error: --user and --pass are required (this tool has no built-in "
                         "account); or set TEA_MQTT_USER / TEA_MQTT_PASS")
    # Only an explicitly given CA is used: probing ~/.tea_agent would pick up
    # a stale certificate from an older broker and break the pinned default.
    client = MqttClient(host, port, user, password,
                        "mqtt-cli-%d-%04x" % (os.getpid(), random.randint(0, 0xFFFF)))
    return client


def now_iso(seconds):
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(seconds))
    except (ValueError, OSError):
        return "-"


def relative_age(seconds):
    if not seconds:
        return "-"
    age = max(0, int(time.time() - seconds))
    if age < 60:
        return "%ds" % age
    if age < 3600:
        return "%dm" % (age // 60)
    if age < 86400:
        return "%dh" % (age // 3600)
    return "%dd" % (age // 86400)


def collect_devices(client, window=2.0):
    """The retained registrations the broker hands over after SUBSCRIBE."""
    client.subscribe(["mt_register/#"])
    devices = {}
    deadline = time.time() + window
    while time.time() < deadline:
        item = client.wait_publish(max(0.05, deadline - time.time()))
        if not item:
            continue
        topic, payload = item
        if not topic.startswith("mt_register/"):
            continue
        name = topic[len("mt_register/"):]
        try:
            data = json.loads(payload)
        except ValueError:
            continue
        data["name"] = data.get("name") or name
        devices[data["name"]] = data
    return devices


# ------------------------------------------------------------------ actions
def cmd_list(args):
    client = build_client(args)
    try:
        client.connect()
        devices = collect_devices(client)
    finally:
        client.close()

    online = [d for d in devices.values() if d.get("online")]
    shown = devices.values() if args.all else online
    shown = sorted(shown, key=lambda d: d.get("name", ""))
    if args.json:
        print(json.dumps(shown, ensure_ascii=False, indent=2))
        return 0
    if not shown:
        print("no device is online" if not args.all else "no registration at all")
        return 0
    print("%-46s %-8s %-15s %-13s %-5s %s" %
          ("DEVICE", "VERSION", "IP", "MAC", "PORT", "SEEN"))
    for device in shown:
        print("%-46s %-8s %-15s %-13s %-5s %s%s" % (
            device.get("name", "?"),
            device.get("version", "?"),
            device.get("ip", "?"),
            device.get("mac", "?"),
            device.get("port", "?"),
            relative_age(device.get("ts", 0)),
            "" if device.get("online") else "  (offline)",
        ))
    if len(online) != len(devices) and not args.all:
        print("\n%d offline registration(s) hidden; --all shows them" %
              (len(devices) - len(online)))
    return 0


def find_online(client, name):
    devices = collect_devices(client)
    device = devices.get(name)
    if device is None:
        raise SystemExit('error: device "%s" is not online (no retained registration); '
                         'check the name with "list"' % name)
    if not device.get("online"):
        raise SystemExit('error: device "%s" is offline (last seen %s)' %
                         (name, now_iso(device.get("ts", 0))))
    return device


def wait_answer(client, name, command_id, timeout):
    """Follow the responses until a terminal state, printing the progress."""
    terminal = ("ready", "stopped", "failed", "rejected", "status")
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        item = client.wait_publish(max(0.05, deadline - time.time()))
        if not item:
            continue
        topic, payload = item
        if topic != "mt_control/%s/resp" % name:
            continue
        try:
            data = json.loads(payload)
        except ValueError:
            continue
        if data.get("id") != command_id:
            continue
        state = data.get("state", "?")
        if state != last:
            print("state   : %s" % state)
            last = state
        if state in terminal:
            return data
    raise SystemExit("error: no answer from %s within %ds (the device may have lost the "
                     "broker; check both ends)" % (name, timeout))


def print_tunnel(data):
    session = data.get("session")
    if session:
        print("session : %s" % session)
    if data.get("broker"):
        print("broker  : %s" % data["broker"])
    if data.get("listen"):
        print("listen  : %s" % data["listen"])
    if data.get("expires_in"):
        print("expires : %ss" % data["expires_in"])
    if session:
        print("next    : a2_tunnel --role client --session %s" % session)


def cmd_wake(args):
    client = build_client(args)
    try:
        client.connect()
        device = find_online(client, args.device)
        print("device  : %s (%s, %s)" %
              (device["name"], device.get("version", "?"), now_iso(device.get("ts", 0))))
        # The operator account may subscribe "mt_control/+/resp" (not one
        # concrete topic); the name filter is done locally.
        client.subscribe(["mt_control/+/resp"])
        command_id = "wake-%d-%04x" % (int(time.time()), random.randint(0, 0xFFFF))
        payload = json.dumps({
            "id": command_id,
            "action": "tunnel.start",
            "args": {"wait_timeout": args.wait_timeout},
            "ts": int(time.time()),
            "ttl": 120,
        })
        client.publish("mt_control/%s/cmd" % args.device, payload)
        data = wait_answer(client, args.device, command_id, args.timeout)
        if args.json:
            print(json.dumps(data, ensure_ascii=False, indent=2))
        if data.get("state") != "ready":
            print("error   : %s" % data.get("error", data.get("state")))
            return 1
        if not args.json:
            print_tunnel(data)
        return 0
    finally:
        client.close()


def simple_command(args, action):
    client = build_client(args)
    try:
        client.connect()
        find_online(client, args.device)
        # The operator account may subscribe "mt_control/+/resp" (not one
        # concrete topic); the name filter is done locally.
        client.subscribe(["mt_control/+/resp"])
        command_id = "%s-%d-%04x" % (action, int(time.time()), random.randint(0, 0xFFFF))
        payload = json.dumps({"id": command_id, "action": action,
                              "ts": int(time.time()), "ttl": 120})
        client.publish("mt_control/%s/cmd" % args.device, payload)
        data = wait_answer(client, args.device, command_id, args.timeout)
        if args.json:
            print(json.dumps(data, ensure_ascii=False, indent=2))
        if data.get("state") in ("failed", "rejected"):
            print("error   : %s" % data.get("error", data.get("state")))
            return 1
        if not args.json and action == "tunnel.stop":
            print_tunnel(data)
        return 0
    finally:
        client.close()


# --------------------------------------------------------------------- main
# Options that take a value, so the scan below does not mistake their value
# for the subcommand.
VALUE_OPTIONS = ("--broker", "--user", "--pass", "--fingerprint", "--ca")


def _normalize_command(argv):
    """Accept `lIst` as `list`. Only the subcommand is case-folded: device
    names are case-sensitive, and an option's value must not be touched."""
    out = list(argv)
    skip_next = False
    for index, token in enumerate(out):
        if skip_next:
            skip_next = False
            continue
        if token in VALUE_OPTIONS:
            skip_next = True
            continue
        if token.startswith("-"):
            continue
        out[index] = token.lower()
        break
    return out


def main(argv=None):
    argv = _normalize_command(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(
        prog="mqtt_cli.py",
        description="list the devices an a2_broker knows, and wake a tunnel on one of them")
    parser.add_argument("--broker", help="host:port (default %s, or TEA_MQTT_BROKER)"
                        % DEFAULT_BROKER)
    parser.add_argument("--user", help="operator account (or TEA_MQTT_USER)")
    parser.add_argument("--pass", dest="password",
                        help="password (or TEA_MQTT_PASS; prefer the environment)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")

    # --json is accepted both before and after the subcommand; SUPPRESS keeps
    # the second one from overwriting a value given before it.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="machine-readable output")

    sub = parser.add_subparsers(dest="command")

    list_parser = sub.add_parser("list", help="devices that are online", parents=[common])
    list_parser.add_argument("--all", action="store_true", help="include offline registrations")

    wake = sub.add_parser("wake", help="start a tunnel and wait for the session code",
                           parents=[common])
    wake.add_argument("device")
    wake.add_argument("--wait-timeout", type=int, default=1800,
                      help="how long the sidecar waits for the client side (default 1800)")
    wake.add_argument("--timeout", type=int, default=60,
                      help="how long to wait for the device to answer (default 60)")

    stop = sub.add_parser("stop", help="stop the tunnel on a device", parents=[common])
    stop.add_argument("device")
    stop.add_argument("--timeout", type=int, default=30)

    status = sub.add_parser("status", help="ask a device what its tunnel is doing",
                            parents=[common])
    status.add_argument("device")
    status.add_argument("--timeout", type=int, default=30)

    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 2

    try:
        if args.command == "list":
            return cmd_list(args)
        if args.command == "wake":
            return cmd_wake(args)
        if args.command == "stop":
            return simple_command(args, "tunnel.stop")
        if args.command == "status":
            return simple_command(args, "status")
    except MqttError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    except ssl.SSLError as exc:
        print("error: TLS failed: %s" % exc, file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
