#!/usr/bin/env python3
"""The fleet server: the school server's control plane for its 40 devices.

The device ledger lives here, the daily collection runs here, and the batch
jobs (upgrade, restart, patch) are started here. Everything it exposes is an
HTTP API, so there are three ways in:

  * the server's own a2 agent, through the `toolkit_fleet` tool - the agent
    decides WHAT should happen and asks this service to do it;
  * the WeChat / Telegram bots, which already talk to the task queue and can
    talk to this one the same way;
  * cron / a systemd timer, for the daily collection.

The third way in is the one that matters for the multi-level setup: this
service does the deterministic part (collect, upgrade), while the devices
themselves run their own a2 for the part that needs thinking (a device that
misbehaves is asked, in its own words, with its own context).

Usage:

  python3 sched/fleet_server.py                     # serve on 127.0.0.1:8290
  python3 sched/fleet_server.py --collect-once     # just do a collection
  python3 sched/fleet_server.py --devices           # print the inventory

Standard library only: this runs on the server next to the agent.
"""

import argparse
import json
import os
import sys
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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fleet  # noqa: E402  (the engine next door)

DEFAULT_HOST = os.environ.get("TEA_FLEET_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("TEA_FLEET_PORT", "8290"))
DEFAULT_LEDGER = fleet.DEFAULT_LEDGER
DEFAULT_DATA_DIR = fleet.DEFAULT_DATA_DIR

CODE_OK = 0
CODE_BAD_REQUEST = 40001
CODE_UNAUTHENTICATED = 40101
CODE_NOT_FOUND = 40401
CODE_CONFLICT = 40901
CODE_INTERNAL = 50001


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def log(message):
    print("%s  %s" % ( (time.strftime("%Y-%m-%d %H:%M:%S"), message)), flush=True)


class FleetService(object):
    """The engine plus the job bookkeeping the API needs."""

    def __init__(self, ledger_path=None, data_dir=None, token=None, device_token=None):
        # Two different secrets:
        #   self.token       - who may call THIS service;
        #   the device token - what this service shows the devices.
        # In a school they are different, so they are separate arguments.
        self.fleet = fleet.Fleet(ledger_path, data_dir, default_token=device_token)
        # The same order as the engine: the flag, then the file, then the
        # environment. The file is what the systemd unit points at.
        self.token = token or fleet.read_token_file() or \
            os.environ.get("TEA_FLEET_TOKEN") or ""
        self.jobs = {}
        self.job_order = []
        self.lock = threading.Lock()
        self.started_ms = int(time.time() * 1000)

    def load(self):
        self.fleet.ledger.load()

    def devices(self, groups=None, names=None, labels=None):
        return self.fleet.ledger.select(groups=groups, names=names, labels=labels)

    def collect(self, groups=None, names=None, labels=None, save=True):
        devices = self.devices(groups, names, labels)
        rows = self.fleet.collect(devices)
        if save:
            self.fleet.save_collection(rows)
        return rows

    def start_job(self, command, groups=None, names=None, labels=None, **kwargs):
        """Run a batch job in the background and hand back its id.

        A job on 40 devices takes minutes; the caller does not want to hold
        an HTTP connection for that, so the job runs in a thread and the
        caller polls GET /api/fleet/job/{id}.
        """
        devices = self.devices(groups, names, labels)
        if not devices:
            return None, "no devices selected"
        job_id = "job_" + time.strftime("%Y%m%d_%H%M%S", time.gmtime()) + \
            "_" + str(len(self.jobs) + 1)
        state = {"id": job_id, "command": command, "status": "running",
                 "started_at": now_iso(), "devices": [d.name for d in devices],
                 "result": None}

        def run():
            try:
                summary = self.fleet.run_job(command, devices, job_id=job_id, **kwargs)
                state["result"] = summary
                state["status"] = "done" if summary.get("ok") else "failed"
            except Exception as exc:  # a job must never take the server down
                state["status"] = "failed"
                state["error"] = str(exc)
            state["finished_at"] = now_iso()

        thread = threading.Thread(target=run)
        thread.daemon = True
        with self.lock:
            self.jobs[job_id] = state
            self.job_order.append(job_id)
            # Keep the last 50 jobs; the audit log has the rest.
            if len(self.job_order) > 50:
                old = self.job_order.pop(0)
                self.jobs.pop(old, None)
        thread.start()
        return job_id, None

    def job(self, job_id):
        with self.lock:
            return self.jobs.get(job_id)

    def jobs_list(self):
        with self.lock:
            return [self.jobs[j] for j in self.job_order]


class Handler(BaseHTTPRequestHandler):
    """The HTTP API."""

    server_version = "tea_fleet/1.0"

    def log_message(self, *args):
        pass  # the service logs what matters itself

    # ---- plumbing ---------------------------------------------------------
    @property
    def service(self):
        return self.server.service

    def _request_id(self):
        return "req_" + os.urandom(6).hex()

    def _envelope(self, code, message, data):
        return {"code": code, "message": message,
                "request_id": self._request_id(), "data": data}

    def _json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _ok(self, data=None, message="ok"):
        self._json(200, self._envelope(CODE_OK, message, data if data is not None else {}))

    def _error(self, status, code, message):
        self._json(status, self._envelope(code, message, {}))

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length).decode("utf-8", "replace")
        try:
            parsed = json.loads(raw)
        except ValueError:
            return None
        return parsed if isinstance(parsed, dict) else None

    def _query(self):
        query = {}
        if "?" not in self.path:
            return query
        from urllib.parse import parse_qs
        for key, values in parse_qs(self.path.split("?", 1)[1]).items():
            query[key] = values[0] if values else ""
        return query

    def _authorized(self):
        """The bearer token, when the service was given one."""
        expected = self.service.token
        if not expected:
            return True
        header = self.headers.get("Authorization") or ""
        if header.startswith("Bearer "):
            if header[7:].strip() == expected:
                return True
        if (self.headers.get("X-Api-Token") or "").strip() == expected:
            return True
        return False

    def _select(self, body, query):
        groups = body.get("groups") or query.get("groups")
        names = body.get("devices") or query.get("devices")
        labels = body.get("labels") or query.get("labels")
        if isinstance(groups, str):
            groups = [g for g in groups.split(",") if g]
        if isinstance(names, str):
            names = [n for n in names.split(",") if n]
        if isinstance(labels, dict):
            pass
        elif isinstance(labels, str):
            parsed = {}
            for pair in labels.split(","):
                if "=" in pair:
                    key, value = pair.split("=", 1)
                    parsed[key.strip()] = value.strip()
            labels = parsed
        else:
            labels = None
        return groups, names, labels

    # ---- routes -----------------------------------------------------------
    def do_OPTIONS(self):  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Api-Token")
        self.end_headers()

    def do_GET(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        query = self._query()
        if not self._authorized():
            self._error(401, CODE_UNAUTHENTICATED, "this fleet service needs its token")
            return
        if path == "/api/fleet/health":
            self._ok({"ok": True, "uptime_s": (int(time.time() * 1000) - self.service.started_ms) / 1000.0,
                      "devices": len(self.service.fleet.ledger.devices),
                      "auth": bool(self.service.token)})
            return
        if path == "/api/fleet/devices":
            groups, names, labels = self._select({}, query)
            rows = []
            for d in self.service.devices(groups, names, labels):
                rows.append({"name": d.name, "host": d.host, "port": d.port,
                             "group": d.group, "enabled": d.enabled, "labels": d.labels,
                             "probe": bool(d.probe)})
            self._ok({"devices": rows, "total": len(rows)})
            return
        if path == "/api/fleet/status":
            # The last collection, if there is one; a fresh one otherwise.
            rows = self.service.fleet.history(query.get("day") or None)
            if not rows:
                rows = self.service.collect(save=False)
            self._ok({"at": now_iso(), "devices": rows,
                      "online": len([r for r in rows if r.get("ok")]),
                      "total": len(rows)})
            return
        if path == "/api/fleet/report":
            day = query.get("day") or None
            rows = self.service.fleet.history(day)
            if not rows:
                rows = self.service.collect(save=False)
            self._json(200, {"code": CODE_OK, "message": "ok",
                             "request_id": self._request_id(),
                             "data": {"markdown": fleet.render_report(rows, day)}})
            return
        if path == "/api/fleet/history":
            rows = self.service.fleet.history(query.get("day") or None)
            self._ok({"rows": rows, "total": len(rows)})
            return
        if path == "/api/fleet/audit":
            rows = self.service.fleet.history(None)
            limit = int(query.get("limit") or 200)
            entries = []
            if os.path.exists(self.service.fleet.audit_path):
                with open(self.service.fleet.audit_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entries.append(json.loads(line))
                        except ValueError:
                            continue
            self._ok({"entries": entries[-limit:], "total": len(entries)})
            return
        if path == "/api/fleet/jobs":
            self._ok({"jobs": self.service.jobs_list()})
            return
        if path.startswith("/api/fleet/job/"):
            job_id = path[len("/api/fleet/job/"):]
            state = self.service.job(job_id)
            if not state:
                self._error(404, CODE_NOT_FOUND, "no such job: %s" % job_id)
                return
            self._ok(state)
            return
        self._error(404, CODE_NOT_FOUND, "no such route: %s" % path)

    def do_POST(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        if not self._authorized():
            self._error(401, CODE_UNAUTHENTICATED, "this fleet service needs its token")
            return
        body = self._body()
        if body is None:
            self._error(400, CODE_BAD_REQUEST, "the body must be a JSON object")
            return
        query = self._query()
        groups, names, labels = self._select(body, query)

        if path == "/api/fleet/collect":
            rows = self.service.collect(groups, names, labels,
                                        save=not body.get("dry_run"))
            self._ok({"devices": rows,
                      "online": len([r for r in rows if r.get("ok")]),
                      "total": len(rows)})
            return
        if path == "/api/fleet/exec":
            command = str(body.get("command") or "").strip()
            if not command:
                self._error(400, CODE_BAD_REQUEST, "'command' is required")
                return
            job_id, err = self.service.start_job(
                command, groups, names, labels,
                verify=body.get("verify") or None,
                rollback=body.get("rollback") or None,
                batch_size=int(body.get("batch_size") or 0),
                canary=int(body.get("canary") or 0),
                pause_s=float(body.get("pause_s") or 0),
                max_failure_rate=float(body.get("max_failure_rate") or 0),
                timeout=float(body.get("timeout_s") or 120),
                dry_run=bool(body.get("dry_run")))
            if err:
                self._error(400, CODE_BAD_REQUEST, err)
                return
            self._ok({"job": job_id, "status": "running"})
            return
        if path == "/api/fleet/upgrade":
            package = str(body.get("package") or "").strip()
            if not package:
                self._error(400, CODE_BAD_REQUEST, "'package' is required")
                return
            manager = str(body.get("manager") or "auto")
            command, rollback = fleet.upgrade_commands(package, manager,
                                                       body.get("rollback") or "")
            job_id, err = self.service.start_job(
                command, groups, names, labels,
                verify=body.get("verify") or None,
                rollback=rollback,
                batch_size=int(body.get("batch_size") or 5),
                canary=int(body.get("canary") or 1),
                pause_s=float(body.get("pause_s") or 30),
                max_failure_rate=float(body.get("max_failure_rate") or 0),
                timeout=float(body.get("timeout_s") or 600),
                dry_run=bool(body.get("dry_run")))
            if err:
                self._error(400, CODE_BAD_REQUEST, err)
                return
            self._ok({"job": job_id, "status": "running", "command": command})
            return
        if path == "/api/fleet/ask":
            # The multi-level path: each device's OWN agent answers, with the
            # device's own provider and context. The server only relays.
            prompt = str(body.get("prompt") or "").strip()
            if not prompt:
                self._error(400, CODE_BAD_REQUEST, "'prompt' is required")
                return
            devices = self.service.devices(groups, names, labels)
            if not devices:
                self._error(400, CODE_BAD_REQUEST, "no devices selected")
                return
            rows = self.service.fleet.ask_devices(
                prompt, devices, timeout=float(body.get("timeout_s") or 300),
                workspace_id=body.get("workspace_id") or None)
            self._ok({"devices": rows,
                      "answered": len([r for r in rows if r.get("ok")])})
            return
        self._error(404, CODE_NOT_FOUND, "no such route: %s" % path)


def serve(args):
    service = FleetService(args.ledger, args.data_dir, args.token or None,
                           args.device_token or fleet.read_token_file(args.device_token_file) or None)
    try:
        service.load()
    except fleet.FleetError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.service = service
    httpd.daemon_threads = True
    log("fleet: serving on http://%s:%d (%d device(s))%s" % (
        args.host, args.port, len(service.fleet.ledger.devices),
        "" if not service.token else " (token required)"))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log("fleet: stopping")
    finally:
        httpd.server_close()
    return 0


def main(argv):
    parser = argparse.ArgumentParser(description="the fleet control plane")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--ledger", default=DEFAULT_LEDGER)
    parser.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    parser.add_argument("--token", default="", help="the token callers must present")
    parser.add_argument("--token-file", default=fleet.DEFAULT_TOKEN_FILE,
                        help="the file the service token is read from")
    parser.add_argument("--device-token", default="",
                        help="the token this service presents to the devices")
    parser.add_argument("--device-token-file", default=fleet.DEFAULT_DEVICE_TOKEN_FILE,
                        help="the file the device token is read from")
    parser.add_argument("--collect-once", action="store_true",
                        help="do one collection and exit (what the timer runs)")
    parser.add_argument("--devices", action="store_true", help="print the inventory and exit")
    args = parser.parse_args(argv)

    if args.collect_once or args.devices:
        service = FleetService(args.ledger, args.data_dir, args.token or None,
                               args.device_token or fleet.read_token_file(args.device_token_file) or None)
        try:
            service.load()
        except fleet.FleetError as exc:
            print("error: %s" % exc, file=sys.stderr)
            return 1
        if args.devices:
            for d in service.fleet.ledger.devices:
                print("%-24s %-16s %s" % (d.name, "%s:%d" % (d.host, d.port),
                                          d.group or "-"))
            return 0
        rows = service.collect()
        for r in rows:
            print("%-24s %s" % (r["name"], "ok" if r.get("ok") else "FAIL " + str(r.get("error"))))
        log("fleet: %d/%d online" % (len([r for r in rows if r.get("ok")]), len(rows)))
        return 0

    return serve(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
