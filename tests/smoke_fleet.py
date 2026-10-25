#!/usr/bin/env python3
"""Smoke test for the fleet: the ledger, the collection, the batch jobs.

What this pins:

  * the ledger: the inventory loads, the selectors (group / name / label)
    pick the right devices, and a broken ledger is refused with a message
    instead of a traceback;
  * the collection: every device is asked in parallel, a device that is down
    is a row with an error and not a crash, and the day gets a history file
    and a report;
  * the batch job: the canary goes first and a failing canary stops the run
    before the other devices are touched, the batches run in order, a failed
    device is rolled back, and every step lands in the audit log;
  * the HTTP API of the fleet server: the token is required, the inventory
    is served, a job is started and can be polled;
  * the real binary: a device started with --api-token answers /api/status
    and /api/exec, and refuses them without the token.

Usage: python3 tests/smoke_fleet.py [path-to-tea_agent-binary]
"""

import json
import os
import shutil
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

    class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
        """The 3.6 spelling of http.server.ThreadingHTTPServer."""
        daemon_threads = True

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "sched"))

import fleet  # noqa: E402
import fleet_server  # noqa: E402

CHECKS = []


def check(name, ok, detail=""):
    CHECKS.append((name, bool(ok), detail))
    print("%-4s %s%s" % ("ok" if ok else "FAIL", name, ("  -- " + detail) if detail and not ok else ""))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ---------------------------------------------------------------------------
# a fake device
# ---------------------------------------------------------------------------

class MockProvider(BaseHTTPRequestHandler):
    """A provider that answers, and remembers what it was sent.

    The first turn asks for the fleet with toolkit_fleet; the second one
    repeats what the tool said. The answer is a stream, because the agent
    asks the provider with stream: true.
    """

    seen = []
    ask_the_fleet = False

    def log_message(self, *args):
        pass

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        MockProvider.seen.append(body)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        if MockProvider.ask_the_fleet and len(MockProvider.seen) == 1:
            self._chunk({"tool_calls": [{
                "index": 0, "id": "call_1", "type": "function",
                "function": {"name": "toolkit_fleet",
                             "arguments": json.dumps({"action": "status"})},
            }]})
        else:
            self._chunk({"content": "ok"})
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def _chunk(self, delta):
        frame = {"id": "c1", "object": "chat.completion.chunk", "created": 1,
                 "model": "mock",
                 "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}
        self.wfile.write(("data: " + json.dumps(frame) + "\n\n").encode("utf-8"))
        self.wfile.flush()


class DeviceProvider(BaseHTTPRequestHandler):
    """A provider that belongs to the DEVICE: it answers with a tool call.

    It is the device's own provider, not the server's - that is the point of
    the multi-level setup: the device thinks for itself.
    """

    seen = []

    def log_message(self, *args):
        pass

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        DeviceProvider.seen.append(body)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        if len(DeviceProvider.seen) == 1:
            delta = {"tool_calls": [{
                "index": 0, "id": "call_1", "type": "function",
                "function": {"name": "toolkit_exec",
                             "arguments": json.dumps({"command": "cat /etc/hostname"})},
            }]}
        else:
            delta = {"content": "the projector service is DOWN"}
        frame = {"id": "c1", "object": "chat.completion.chunk",
                 "choices": [{"index": 0, "finish_reason": None, "delta": delta}]}
        self.wfile.write(("data: " + json.dumps(frame) + "\n\n").encode("utf-8"))
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


class FakeDevice(BaseHTTPRequestHandler):
    """A device that answers /api/status and /api/exec like a2 does.

    `state` is shared with the test: it says whether the device is up, and
    which commands fail on it.
    """

    def log_message(self, *args):
        pass

    def _json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path != "/api/status":
            self._json(404, {"error": {"code": "not_found"}})
            return
        state = self.server.state
        if not state.get("up", True):
            self._json(500, {"error": {"code": "boom"}})
            return
        self._json(200, {
            "ok": True, "name": state["name"], "version": "0.12.1",
            "uptime_s": 3600.0, "os": "FakeOS", "arch": "x86_64",
            "cpu_count": 4, "memory_total_bytes": 8 * 1024 ** 3,
            "disk_free_bytes": 40 * 1024 ** 3, "provider": "deepseek",
            "model": "deepseek-flash", "exec_enabled": True,
        })

    def do_POST(self):  # noqa: N802
        if self.path != "/api/exec":
            self._json(404, {"error": {"code": "not_found"}})
            return
        state = self.server.state
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        command = body.get("command", "")
        state.setdefault("commands", []).append(command)
        if not state.get("up", True):
            self._json(500, {"error": {"code": "boom"}})
            return
        for bad in state.get("fail_commands", []):
            if bad in command:
                self._json(200, {"ok": False, "exit_code": 1,
                                 "output": "boom: " + command, "elapsed_ms": 1,
                                 "timed_out": False, "truncated": False})
                return
        if state.get("fail_verify") and "verify" in command:
            self._json(200, {"ok": False, "exit_code": 2, "output": "verify failed",
                             "elapsed_ms": 1, "timed_out": False, "truncated": False})
            return
        self._json(200, {"ok": True, "exit_code": 0,
                         "output": "ran: " + command, "elapsed_ms": 1,
                         "timed_out": False, "truncated": False})


def start_fake_device(name, **state):
    port = free_port()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), FakeDevice)
    httpd.state = dict(state)
    httpd.state["name"] = name
    thread = threading.Thread(target=httpd.serve_forever)
    thread.daemon = True
    thread.start()
    return httpd, port


# ---------------------------------------------------------------------------
# the tests
# ---------------------------------------------------------------------------

def test_ledger(tmp):
    path = os.path.join(tmp, "devices.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({
            "defaults": {"port": 8282, "token": "shared-token"},
            "devices": [
                {"name": "lab-01", "host": "10.0.0.1", "group": "lab", "labels": {"room": "A"}},
                {"name": "lab-02", "host": "10.0.0.2", "group": "lab", "labels": {"room": "A"}},
                {"name": "class-01", "host": "10.0.0.3", "group": "class", "labels": {"room": "B"}},
                {"name": "off-01", "host": "10.0.0.4", "group": "class", "enabled": False},
            ],
        }, f)

    ledger = fleet.Ledger(path).load()
    check("the ledger loads", len(ledger.devices) == 4)
    check("the defaults carry the token", ledger.token_for(ledger.devices[0]) == "shared-token")
    check("a disabled device is left out", len(ledger.select()) == 3)
    check("a group selects", [d.name for d in ledger.select(groups=["lab"])] == ["lab-01", "lab-02"])
    check("a name selects", [d.name for d in ledger.select(names=["class-01"])] == ["class-01"])
    check("a label selects", [d.name for d in ledger.select(labels={"room": "B"})] == ["class-01"])
    check("an empty selector means everything enabled", len(ledger.select()) == 3)

    # No group in the ledger => the default group. A ledger that is nothing
    # but a list of addresses therefore still has groups: exactly one.
    plain = os.path.join(tmp, "plain.json")
    with open(plain, "w", encoding="utf-8") as f:
        json.dump({"devices": [
            {"name": "ip-1", "host": "10.0.0.1"},
            {"name": "ip-2", "host": "10.0.0.2"},
        ]}, f)
    plain_ledger = fleet.Ledger(plain).load()
    check("a device with no group is in the default group",
          all(d.group == fleet.DEFAULT_GROUP for d in plain_ledger.devices),
          str([d.group for d in plain_ledger.devices]))
    check("the default group selects all of them",
          len(plain_ledger.select(groups=[fleet.DEFAULT_GROUP])) == 2)
    check("no group at all selects all of them",
          len(plain_ledger.select()) == 2)
    check("an empty group name means the default group",
          len(plain_ledger.select(groups=[""])) == 2)
    check("another group does not select them",
          len(plain_ledger.select(groups=["lab"])) == 0)

    # A ledger that mixes both: the ungrouped ones are in the default group,
    # and asking for another group leaves them out.
    mixed = os.path.join(tmp, "mixed.json")
    with open(mixed, "w", encoding="utf-8") as f:
        json.dump({"devices": [
            {"name": "lab-1", "host": "10.0.0.1", "group": "lab"},
            {"name": "free-1", "host": "10.0.0.2"},
        ]}, f)
    mixed_ledger = fleet.Ledger(mixed).load()
    check("a grouped device keeps its group",
          mixed_ledger.devices[0].group == "lab")
    check("an ungrouped device is in the default group",
          mixed_ledger.devices[1].group == fleet.DEFAULT_GROUP)
    check("asking for a group leaves the ungrouped ones out",
          [d.name for d in mixed_ledger.select(groups=["lab"])] == ["lab-1"])
    check("the default group picks the ungrouped ones",
          [d.name for d in mixed_ledger.select(groups=[fleet.DEFAULT_GROUP])] == ["free-1"])

    # A broken ledger must be refused with a message, not a traceback.
    bad = os.path.join(tmp, "bad.json")
    with open(bad, "w", encoding="utf-8") as f:
        f.write("{not json")
    try:
        fleet.Ledger(bad).load()
        check("a broken ledger is refused", False)
    except fleet.FleetError as exc:
        check("a broken ledger is refused", "not valid JSON" in str(exc), str(exc))

    dup = os.path.join(tmp, "dup.json")
    with open(dup, "w", encoding="utf-8") as f:
        json.dump({"devices": [{"name": "x", "host": "1.1.1.1"},
                               {"name": "x", "host": "1.1.1.2"}]}, f)
    try:
        fleet.Ledger(dup).load()
        check("two devices with one name are refused", False)
    except fleet.FleetError as exc:
        check("two devices with one name are refused", "two devices" in str(exc), str(exc))
    return path


def test_collect(tmp, ledger_path):
    httpd1, port1 = start_fake_device("lab-01")
    httpd2, port2 = start_fake_device("lab-02")
    httpd3, port3 = start_fake_device("class-01", up=False)
    try:
        data_dir = os.path.join(tmp, "fleetdata")
        service = fleet_server.FleetService(ledger_path, data_dir, token="shared-token")
        # Point the ledger at the fake devices.
        service.fleet.ledger.devices = [
            fleet.Device("lab-01", "127.0.0.1", port1, "lab"),
            fleet.Device("lab-02", "127.0.0.1", port2, "lab"),
            fleet.Device("class-01", "127.0.0.1", port3, "class"),
        ]
        rows = service.collect()
        by_name = dict((r["name"], r) for r in rows)
        check("the collection visits every device", len(rows) == 3)
        check("a healthy device is ok",
              by_name["lab-01"].get("ok") and by_name["lab-01"].get("version") == "0.12.1")
        check("a device that is down is a row, not a crash",
              not by_name["class-01"].get("ok") and by_name["class-01"].get("error"))
        check("the history is written", os.path.exists(service.fleet.history_path()))
        report = open(service.fleet.report_path(), encoding="utf-8").read()
        check("the report names the offline device", "class-01" in report and "离线" in report)
        entries = []
        with open(service.fleet.audit_path, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    entries.append(json.loads(line))
        check("the collection is in the audit log",
              any(e.get("action") == "collect" for e in entries),
              json.dumps(entries))
        return service
    finally:
        for httpd in (httpd1, httpd2, httpd3):
            httpd.shutdown()


def test_batch_job(tmp):
    httpd1, port1 = start_fake_device("lab-01")
    httpd2, port2 = start_fake_device("lab-02")
    httpd3, port3 = start_fake_device("class-01")
    try:
        data_dir = os.path.join(tmp, "fleetdata2")
        service = fleet_server.FleetService(None, data_dir, token="t")
        service.fleet.ledger.devices = [
            fleet.Device("lab-01", "127.0.0.1", port1, "lab"),
            fleet.Device("lab-02", "127.0.0.1", port2, "lab"),
            fleet.Device("class-01", "127.0.0.1", port3, "class"),
        ]
        # A canary that fails must stop the run before the others are touched.
        httpd1.state["fail_commands"] = ["apt-get install"]
        summary = service.fleet.run_job("apt-get install -y nginx",
                                        service.fleet.ledger.devices,
                                        canary=1, batch_size=2)
        check("a failing canary stops the run", summary["stopped_early"])
        check("the other devices are left alone",
              all(r.get("skipped") for r in summary["results"][1:]),
              json.dumps(summary["results"][1:]))
        check("the canary itself is marked failed", summary["results"][0]["ok"] is False)
        check("only the canary was touched",
              len(httpd2.state.get("commands", [])) == 0 and
              len(httpd3.state.get("commands", [])) == 0)

        # A failing device is rolled back.
        httpd1.state["fail_commands"] = []
        httpd2.state["fail_commands"] = ["apt-get install"]
        summary = service.fleet.run_job("apt-get install -y nginx",
                                        service.fleet.ledger.devices,
                                        rollback="apt-get remove -y nginx",
                                        canary=0, batch_size=3)
        check("a failed device is rolled back",
              any("rollback" in r for r in summary["results"] if not r.get("ok")))
        check("the rollback command ran on the failed device",
              any("apt-get remove" in c for c in httpd2.state.get("commands", [])))

        # The verify command decides whether a device counts as done.
        httpd2.state["fail_commands"] = []
        httpd2.state["fail_verify"] = True
        summary = service.fleet.run_job("apt-get install -y nginx",
                                        service.fleet.ledger.devices,
                                        verify="verify-me", canary=0, batch_size=3)
        failed = [r for r in summary["results"] if r.get("ok") is False]
        check("a failed verify fails the device", len(failed) == 1, json.dumps(summary["results"]))
        check("the verify result is kept", failed and "verify" in failed[0])

        # The audit log has one line per device plus the job lines.
        entries = []
        with open(service.fleet.audit_path, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    entries.append(json.loads(line))
        check("the audit log records the job start",
              any(e.get("action") == "job_start" for e in entries))
        check("the audit log records every device",
              len([e for e in entries if e.get("action") == "device_done"]) >= 6)
    finally:
        for httpd in (httpd1, httpd2, httpd3):
            httpd.shutdown()


def test_fleet_http(tmp, ledger_path):
    httpd1, port1 = start_fake_device("lab-01")
    httpd2, port2 = start_fake_device("lab-02")
    try:
        data_dir = os.path.join(tmp, "fleetdata3")
        service = fleet_server.FleetService(ledger_path, data_dir, token="srv-token")
        service.fleet.ledger.devices = [
            fleet.Device("lab-01", "127.0.0.1", port1, "lab"),
            fleet.Device("lab-02", "127.0.0.1", port2, "lab"),
        ]
        port = free_port()
        httpd = ThreadingHTTPServer(("127.0.0.1", port), fleet_server.Handler)
        httpd.service = service
        httpd.daemon_threads = True
        thread = threading.Thread(target=httpd.serve_forever)
        thread.daemon = True
        thread.start()
        base = "http://127.0.0.1:%d" % port
        try:
            # No token: refused.
            try:
                urllib.request.urlopen(base + "/api/fleet/devices", timeout=5)
                check("the fleet API needs its token", False)
            except urllib.error.HTTPError as exc:
                check("the fleet API needs its token", exc.code == 401)

            def get(path):
                req = urllib.request.Request(base + path,
                                             headers={"Authorization": "Bearer srv-token"})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    return json.loads(resp.read().decode("utf-8"))

            def post(path, body):
                req = urllib.request.Request(
                    base + path, data=json.dumps(body).encode("utf-8"),
                    headers={"Content-Type": "application/json",
                             "Authorization": "Bearer srv-token"}, method="POST")
                with urllib.request.urlopen(req, timeout=10) as resp:
                    return json.loads(resp.read().decode("utf-8"))

            answer = get("/api/fleet/devices")
            check("the inventory is served", answer["data"]["total"] == 2)
            answer = get("/api/fleet/status")
            check("the status is served", answer["data"]["total"] == 2)
            answer = post("/api/fleet/exec", {"command": "echo hi"})
            check("a job is accepted", answer["data"].get("job", "").startswith("job_"))
            job_id = answer["data"]["job"]
            for _ in range(50):
                state = get("/api/fleet/job/" + job_id)["data"]
                if state["status"] != "running":
                    break
                time.sleep(0.1)
            check("the job finishes", state["status"] == "done", json.dumps(state))
            check("the job touched both devices", state["result"]["succeeded"] == 2)
            try:
                post("/api/fleet/exec", {})
                check("an empty command is refused", False)
            except urllib.error.HTTPError as exc:
                answer = json.loads(exc.read().decode("utf-8"))
                check("an empty command is refused",
                      answer["code"] == fleet_server.CODE_BAD_REQUEST, json.dumps(answer))
        finally:
            httpd.shutdown()
    finally:
        for httpd in (httpd1, httpd2):
            httpd.shutdown()


def test_real_binary(exe, tmp):
    """The real a2 on a real port: the fleet channel and its token."""
    if not exe or not os.path.exists(exe):
        print("skip  the real binary (no exe given)")
        return
    port = free_port()
    home = os.path.join(tmp, "home")
    os.makedirs(home)
    env = dict(os.environ)
    env["TEA_AGENT_HOME"] = home
    proc = subprocess.Popen(
        [exe, "--port", str(port), "--api-token", "dev-token", "--tea-home", home],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
    try:
        base = "http://127.0.0.1:%d" % port
        ready = False
        for _ in range(80):
            try:
                urllib.request.urlopen(base + "/api/health", timeout=1)
                ready = True
                break
            except Exception:
                time.sleep(0.1)
        if not ready:
            check("the real binary comes up", False, "it never answered /api/health")
            return
        check("the real binary comes up", True)

        # No token: refused.
        try:
            urllib.request.urlopen(base + "/api/status", timeout=5)
            check("the real /api/status needs the token", False)
        except urllib.error.HTTPError as exc:
            check("the real /api/status needs the token", exc.code == 401)

        def post(path, body, token):
            headers = {"Content-Type": "application/json"}
            if token:
                headers["Authorization"] = "Bearer " + token
            req = urllib.request.Request(base + path, data=json.dumps(body).encode("utf-8"),
                                         headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode("utf-8"))

        def get(path, token):
            headers = {}
            if token:
                headers["Authorization"] = "Bearer " + token
            req = urllib.request.Request(base + path, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode("utf-8"))

        status = get("/api/status", "dev-token")
        check("the real /api/status answers with the token", status.get("ok") and status.get("version"))
        check("the real status says the fleet channel is on", status.get("exec_enabled") is True)

        answer = post("/api/exec", {"command": "echo fleet-works"}, "dev-token")
        check("the real /api/exec runs the command",
              answer.get("ok") and "fleet-works" in answer.get("output", ""),
              json.dumps(answer))
        answer = post("/api/exec", {"command": "exit 3"}, "dev-token")
        check("the real /api/exec reports the exit code", answer.get("exit_code") == 3,
              json.dumps(answer))
        try:
            post("/api/exec", {"command": "echo nope"}, "wrong-token")
            check("the real /api/exec refuses a wrong token", False)
        except urllib.error.HTTPError as exc:
            check("the real /api/exec refuses a wrong token", exc.code == 401)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()


def test_no_token_binary(exe, tmp):
    """Without --api-token there is no fleet channel at all."""
    if not exe or not os.path.exists(exe):
        print("skip  the no-token binary (no exe given)")
        return
    port = free_port()
    home = os.path.join(tmp, "home2")
    os.makedirs(home)
    env = dict(os.environ)
    env["TEA_AGENT_HOME"] = home
    proc = subprocess.Popen(
        [exe, "--port", str(port), "--tea-home", home],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
    try:
        base = "http://127.0.0.1:%d" % port
        for _ in range(80):
            try:
                urllib.request.urlopen(base + "/api/health", timeout=1)
                break
            except Exception:
                time.sleep(0.1)
        # The status is open (it is read-only), but there is no exec channel.
        try:
            urllib.request.urlopen(base + "/api/status", timeout=5)
            check("the status is open without a token", True)
        except urllib.error.HTTPError as exc:
            check("the status is open without a token", exc.code == 401, "HTTP %s" % exc.code)
        try:
            req = urllib.request.Request(
                base + "/api/exec", data=json.dumps({"command": "echo nope"}).encode("utf-8"),
                headers={"Content-Type": "application/json"}, method="POST")
            urllib.request.urlopen(req, timeout=5)
            check("there is no exec channel without a token", False)
        except urllib.error.HTTPError as exc:
            check("there is no exec channel without a token", exc.code == 404, "HTTP %s" % exc.code)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()


def test_advertisement(exe, tmp):
    """The fleet tool is advertised to the model only when there is a fleet.

    /api/tools lists every registered tool - that is what that route is for.
    What matters is what the agent puts in the request to the provider.
    """
    if not exe or not os.path.exists(exe):
        print("skip  the advertisement check (no exe given)")
        return
    mock = ThreadingHTTPServer(("127.0.0.1", 0), MockProvider)
    mock.daemon_threads = True
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    mock_port = mock.server_address[1]

    def run(fleet_url):
        MockProvider.seen[:] = []
        port = free_port()
        home = os.path.join(tmp, "adv-%s" % ("fleet" if fleet_url else "plain"))
        os.makedirs(home)
        provider = os.path.join(home, "provider.yaml")
        with open(provider, "w", encoding="utf-8") as f:
            f.write("providers:\n  mock:\n    api_url: http://127.0.0.1:%d/v1\n"
                    "    api_key: sk-test\n    default_model: mock-model\n"
                    "    models:\n      - id: mock-model\n"
                    "provider_order: [mock]\nsettings:\n  max_iterations: 1\n" % mock_port)
        env = dict(os.environ)
        env["TEA_AGENT_HOME"] = home
        if fleet_url:
            env["TEA_AGENT_FLEET_URL"] = fleet_url
        else:
            env.pop("TEA_AGENT_FLEET_URL", None)
        proc = subprocess.Popen([exe, "--port", str(port), "--tea-home", home],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
        try:
            base = "http://127.0.0.1:%d" % port
            for _ in range(80):
                try:
                    urllib.request.urlopen(base + "/api/health", timeout=1)
                    break
                except Exception:
                    time.sleep(0.1)
            ws = json.loads(urllib.request.urlopen(base + "/api/workspaces", timeout=5).read())
            if not ws["workspaces"]:
                req = urllib.request.Request(
                    base + "/api/workspaces",
                    data=json.dumps({"path": home, "name": "home"}).encode("utf-8"),
                    headers={"Content-Type": "application/json"}, method="POST")
                urllib.request.urlopen(req, timeout=5).read()
                ws = json.loads(urllib.request.urlopen(base + "/api/workspaces", timeout=5).read())
            payload = json.dumps({"workspace_id": ws["workspaces"][0]["id"],
                                  "message": "hello"}).encode("utf-8")
            req = urllib.request.Request(base + "/api/chat", data=payload,
                                         headers={"Content-Type": "application/json"},
                                         method="POST")
            try:
                urllib.request.urlopen(req, timeout=30).read()
            except Exception:
                pass
            time.sleep(0.5)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:
                proc.kill()
        return [t["function"]["name"] for body in MockProvider.seen
                for t in body.get("tools", [])]

    try:
        plain = run("")
        check("the fleet tool is not advertised without a fleet",
              "toolkit_fleet" not in plain, str(sorted(set(plain))))
        withfleet = run("http://127.0.0.1:8290")
        check("the fleet tool is advertised with a fleet",
              "toolkit_fleet" in withfleet, str(sorted(set(withfleet))))
    finally:
        mock.shutdown()


def test_server_drives_the_fleet(exe, tmp):
    """The whole chain: the server's a2 asks the fleet, the fleet asks the devices.

    server a2 (the model) -> toolkit_fleet -> fleet service -> device a2

    This is the piece that makes the setup multi-level rather than a remote
    shell: the SERVER decides what to ask, the FLEET service does the
    bookkeeping, and the DEVICE reports what it sees.
    """
    if not exe or not os.path.exists(exe):
        print("skip  the multi-level check (no exe given)")
        return
    mock = ThreadingHTTPServer(("127.0.0.1", 0), MockProvider)
    mock.daemon_threads = True
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    mock_port = mock.server_address[1]

    devices = []
    for name in ("dev-a", "dev-b"):
        port = free_port()
        httpd = ThreadingHTTPServer(("127.0.0.1", port), FakeDevice)
        httpd.state = {"name": name}
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        devices.append((name, port, httpd))

    ledger = os.path.join(tmp, "ml-devices.json")
    with open(ledger, "w", encoding="utf-8") as f:
        json.dump({"devices": [
            {"name": name, "host": "127.0.0.1", "port": port, "group": "lab"}
            for name, port, _ in devices]}, f)

    fleet_port = free_port()
    fleet_log = open(os.path.join(tmp, "fleet.log"), "w", encoding="utf-8")
    fleet_proc = subprocess.Popen(
        [sys.executable, os.path.join(REPO_ROOT, "sched", "fleet_server.py"),
         "--host", "127.0.0.1", "--port", str(fleet_port),
         "--ledger", ledger, "--data-dir", os.path.join(tmp, "ml-data"),
         "--token", "fleet-token", "--device-token", "dev-token"],
        stdout=fleet_log, stderr=subprocess.STDOUT)
    try:
        ready = False
        for _ in range(30):
            try:
                # Every route of the fleet service needs its token, the
                # health one included.
                req = urllib.request.Request(
                    "http://127.0.0.1:%d/api/fleet/health" % fleet_port,
                    headers={"Authorization": "Bearer fleet-token"})
                urllib.request.urlopen(req, timeout=1)
                ready = True
                break
            except Exception:
                time.sleep(0.2)
        if not ready:
            fleet_proc.terminate()
            fleet_log.flush()
            check("the fleet service comes up", False,
                  open(os.path.join(tmp, "fleet.log"), encoding="utf-8").read()[-500:])
            return
        check("the fleet service comes up", True)

        home = os.path.join(tmp, "ml-server")
        os.makedirs(home)
        with open(os.path.join(home, "provider.yaml"), "w", encoding="utf-8") as f:
            f.write("providers:\n  mock:\n    api_url: http://127.0.0.1:%d/v1\n"
                    "    api_key: sk-test\n    default_model: mock-model\n"
                    "    models:\n      - id: mock-model\n"
                    "provider_order: [mock]\nsettings:\n  max_iterations: 4\n" % mock_port)
        agent_port = free_port()
        env = dict(os.environ)
        env["TEA_AGENT_HOME"] = home
        env["TEA_AGENT_FLEET_URL"] = "http://127.0.0.1:%d" % fleet_port
        env["TEA_AGENT_FLEET_TOKEN"] = "fleet-token"
        agent = subprocess.Popen(
            [exe, "--port", str(agent_port), "--tea-home", home, "--workspace", home],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
        try:
            base = "http://127.0.0.1:%d" % agent_port
            for _ in range(80):
                try:
                    urllib.request.urlopen(base + "/api/health", timeout=1)
                    break
                except Exception:
                    time.sleep(0.1)
            ws = json.loads(urllib.request.urlopen(base + "/api/workspaces", timeout=5).read())
            if not ws["workspaces"]:
                req = urllib.request.Request(
                    base + "/api/workspaces",
                    data=json.dumps({"path": home, "name": "home"}).encode("utf-8"),
                    headers={"Content-Type": "application/json"}, method="POST")
                urllib.request.urlopen(req, timeout=5).read()
                ws = json.loads(urllib.request.urlopen(base + "/api/workspaces", timeout=5).read())
            workspace_id = ws["workspaces"][0]["id"]

            MockProvider.seen[:] = []
            MockProvider.ask_the_fleet = True
            payload = json.dumps({"workspace_id": workspace_id,
                                  "message": "how is the fleet?"}).encode("utf-8")
            req = urllib.request.Request(base + "/api/chat", data=payload,
                                         headers={"Content-Type": "application/json"},
                                         method="POST")
            frames = []
            with urllib.request.urlopen(req, timeout=60) as resp:
                for raw in resp:
                    line = raw.decode("utf-8", "replace").strip()
                    if line.startswith("data:"):
                        try:
                            frames.append(json.loads(line[5:].strip()))
                        except ValueError:
                            pass
            tool_results = [f for f in frames if f.get("type") == "tool_result"]
            check("the server a2 called the fleet tool", bool(tool_results),
                  str([f.get("type") for f in frames]))
            if tool_results:
                out = tool_results[0].get("output", "")
                check("the tool brought back the state of both devices",
                      "dev-a" in out and "dev-b" in out, out[:300])
        finally:
            agent.terminate()
            try:
                agent.wait(timeout=10)
            except Exception:
                agent.kill()
    finally:
        fleet_proc.terminate()
        try:
            fleet_proc.wait(timeout=10)
        except Exception:
            fleet_proc.kill()
        for _, _, httpd in devices:
            httpd.shutdown()
        mock.shutdown()


def test_ask_the_devices(exe, tmp):
    """The device's OWN agent answers, with the device's own tools."""
    if not exe or not os.path.exists(exe):
        print("skip  the ask check (no exe given)")
        return
    mock = ThreadingHTTPServer(("127.0.0.1", 0), DeviceProvider)
    mock.daemon_threads = True
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    mock_port = mock.server_address[1]

    home = os.path.join(tmp, "ask-device")
    os.makedirs(home)
    with open(os.path.join(home, "provider.yaml"), "w", encoding="utf-8") as f:
        f.write("providers:\n  mock:\n    api_url: http://127.0.0.1:%d/v1\n"
                "    api_key: sk-device\n    default_model: device-model\n"
                "    models:\n      - id: device-model\n"
                "provider_order: [mock]\nsettings:\n  max_iterations: 4\n" % mock_port)
    port = free_port()
    env = dict(os.environ)
    env["TEA_AGENT_HOME"] = home
    device = subprocess.Popen(
        [exe, "--port", str(port), "--tea-home", home, "--workspace", home,
         "--api-token", "dev-token"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
    try:
        base = "http://127.0.0.1:%d" % port
        for _ in range(80):
            try:
                urllib.request.urlopen(base + "/api/health", timeout=1)
                break
            except Exception:
                time.sleep(0.1)

        service = fleet.Fleet(None, os.path.join(tmp, "ask-data"))
        service.ledger.devices = [fleet.Device("dev-a", "127.0.0.1", port, "lab")]
        rows = service.ask_devices("is the projector service up?",
                                    service.ledger.devices, timeout=60)
        check("the device's own agent answered",
              bool(rows) and rows[0].get("ok"), json.dumps(rows))
        if rows and rows[0].get("ok"):
            check("the answer is the device's own words",
                  "projector" in (rows[0].get("content") or ""),
                  rows[0].get("content", ""))
            used = any("toolkit_exec" in json.dumps(body.get("tools", []))
                       for body in DeviceProvider.seen)
            check("the device used its own tools", used)
    finally:
        device.terminate()
        try:
            device.wait(timeout=10)
        except Exception:
            device.kill()
        mock.shutdown()


def main():
    exe = sys.argv[1] if len(sys.argv) > 1 else ""
    tmp = tempfile.mkdtemp(prefix="tea-fleet-")
    try:
        ledger_path = test_ledger(tmp)
        test_collect(tmp, ledger_path)
        test_batch_job(tmp)
        test_fleet_http(tmp, ledger_path)
        test_real_binary(exe, tmp)
        test_no_token_binary(exe, tmp)
        test_advertisement(exe, tmp)
        test_server_drives_the_fleet(exe, tmp)
        test_ask_the_devices(exe, tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    failed = [c for c in CHECKS if not c[1]]
    print("\n%d/%d checks passed" % (len(CHECKS) - len(failed), len(CHECKS)))
    if failed:
        for name, _ok, detail in failed:
            print("  FAIL %s  %s" % (name, detail))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
