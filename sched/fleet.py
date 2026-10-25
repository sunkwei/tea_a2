#!/usr/bin/env python3
"""The fleet engine: one school server, forty devices, one a2 on each.

The server holds the only copy of the device inventory (the ledger), runs the
daily collection, and executes the batch jobs (upgrade, restart, patch). Every
device is reached over its own a2 HTTP API:

    GET  /api/status   a structured snapshot (version, uptime, os, cpu, mem)
    POST /api/exec     run one command there, get stdout and the exit code

The point of /api/exec is that it does NOT go through the model. "Restart this
service on 40 devices" is then 40 HTTP calls and nothing else - no tokens, no
model, no waiting for an LLM to decide that `systemctl restart` is what was
meant. The LLM is for the questions a script cannot answer.

Three ways to drive a fleet, in increasing order of "the model is in the loop":

  1. this module (deterministic):  fleet.py exec --command "apt-get upgrade"
  2. the fleet server (HTTP):      POST /api/fleet/exec
  3. the device's own agent:       POST /api/fleet/chat  ->  each device's a2
                                    runs the prompt on the device, with the
                                    device's own provider and its own context.

Usage:

  python3 sched/fleet.py devices
  python3 sched/fleet.py collect
  python3 sched/fleet.py exec --command "systemctl restart nginx" --group lab
  python3 sched/fleet.py upgrade --package nginx --group lab
  python3 sched/fleet.py report

Standard library only: these scripts run on the device next to the agent,
where a pip install is not an option.
"""

import argparse
import base64
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

DEFAULT_DATA_DIR = os.path.expanduser(
    os.environ.get("TEA_FLEET_HOME", os.path.join("~", ".tea_agent", "fleet")))
DEFAULT_LEDGER = os.path.join(DEFAULT_DATA_DIR, "devices.json")
# The shared secret, in a file of its own so it does not have to live in the
# ledger (which people edit) or in a unit file (which people read).
DEFAULT_TOKEN_FILE = os.path.join(DEFAULT_DATA_DIR, "token")
# The token the fleet service shows the DEVICES - a different secret from the
# one its own callers present.
DEFAULT_DEVICE_TOKEN_FILE = os.path.join(DEFAULT_DATA_DIR, "device-token")
DEFAULT_PORT = int(os.environ.get("TEA_FLEET_DEVICE_PORT", "8282"))
DEFAULT_TIMEOUT = float(os.environ.get("TEA_FLEET_TIMEOUT", "15"))
# How many devices are talked to at once. The school server has 40 of them
# and they are all on the same LAN, so this is high enough to finish in
# seconds and low enough not to look like a flood.
DEFAULT_CONCURRENCY = int(os.environ.get("TEA_FLEET_CONCURRENCY", "8"))


class FleetError(Exception):
    """Something in the ledger or on a device is wrong."""


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def day_stamp():
    return time.strftime("%Y-%m-%d", time.gmtime())


def log(message):
    print("%s  %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), message), flush=True)


def read_token_file(path=None):
    """The shared secret, from the file the unit points at.

    An empty string when there is no file: the caller then falls back to the
    ledger's own token and to the environment.
    """
    path = path or DEFAULT_TOKEN_FILE
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except (IOError, OSError):
        return ""


# ---------------------------------------------------------------------------
# the ledger
# ---------------------------------------------------------------------------

class Device(object):
    """One device in the inventory."""

    def __init__(self, name, host, port=None, group="", token=None, labels=None,
                 enabled=True, probe=None):
        self.name = name
        self.host = host
        self.port = port or DEFAULT_PORT
        self.group = group or ""
        self.token = token
        self.labels = labels or {}
        self.enabled = enabled
        # An optional command whose output is collected with the status. It is
        # how a device reports the things /api/status knows nothing about
        # (the free space of a data disk, the temperature of a board, the
        # version of the app that runs on it).
        self.probe = probe

    @property
    def url(self):
        return "http://%s:%d" % (self.host, self.port)

    def to_json(self):
        return {
            "name": self.name,
            "host": self.host,
            "port": self.port,
            "group": self.group,
            "token": self.token,
            "labels": self.labels,
            "enabled": self.enabled,
            "probe": self.probe,
        }

    @staticmethod
    def from_json(raw):
        if not isinstance(raw, dict):
            raise FleetError("a device entry must be an object")
        name = str(raw.get("name") or "").strip()
        host = str(raw.get("host") or "").strip()
        if not name:
            raise FleetError("a device entry needs a name")
        if not host:
            raise FleetError("device %s needs a host" % name)
        return Device(
            name=name,
            host=host,
            port=int(raw.get("port") or DEFAULT_PORT),
            group=str(raw.get("group") or ""),
            token=raw.get("token"),
            labels=raw.get("labels") or {},
            enabled=bool(raw.get("enabled", True)),
            probe=raw.get("probe"),
        )

    def __repr__(self):
        return "<Device %s %s>" % (self.name, self.url)


class Ledger(object):
    """The device inventory, kept in one JSON file on the server."""

    def __init__(self, path=None, default_token=None):
        self.path = path or DEFAULT_LEDGER
        self.default_token = default_token
        self.devices = []

    def load(self):
        if not os.path.exists(self.path):
            raise FleetError("no ledger at %s (create it, or pass --ledger)" % self.path)
        with open(self.path, "r", encoding="utf-8") as f:
            try:
                raw = json.load(f)
            except ValueError as exc:
                raise FleetError("the ledger at %s is not valid JSON: %s" % (self.path, exc))
        if not isinstance(raw, dict):
            raise FleetError("the ledger at %s must be an object" % self.path)
        entries = raw.get("devices")
        if not isinstance(entries, list):
            raise FleetError("the ledger at %s has no 'devices' list" % self.path)
        if raw.get("defaults", {}).get("token") and not self.default_token:
            self.default_token = raw["defaults"]["token"]
        self.devices = [Device.from_json(e) for e in entries]
        names = set()
        for d in self.devices:
            if d.name in names:
                raise FleetError("two devices are called %s" % d.name)
            names.add(d.name)
        return self

    def save(self):
        directory = os.path.dirname(self.path)
        if directory and not os.path.isdir(directory):
            os.makedirs(directory)
        payload = {
            "defaults": {"port": DEFAULT_PORT},
            "devices": [d.to_json() for d in self.devices],
        }
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        os.replace(tmp, self.path)

    def token_for(self, device):
        if device.token:
            return device.token
        if self.default_token:
            return self.default_token
        return os.environ.get("TEA_FLEET_TOKEN") or ""

    def select(self, groups=None, names=None, labels=None, include_disabled=False):
        """The devices a job touches.

        An empty selector means "every enabled device": that is the shape a
        daily collection wants, and a batch job that names nothing should
        still do something.
        """
        groups = set(g or "" for g in (groups or []))
        names = set(names or [])
        out = []
        for d in self.devices:
            if not d.enabled and not include_disabled:
                continue
            if names and d.name not in names:
                continue
            if groups and d.group not in groups:
                continue
            if labels:
                ok = True
                for key, value in labels.items():
                    if str(d.labels.get(key, "")) != str(value):
                        ok = False
                        break
                if not ok:
                    continue
            out.append(d)
        return out


# ---------------------------------------------------------------------------
# talking to a device
# ---------------------------------------------------------------------------

def http_json(url, method="GET", body=None, token=None, timeout=15):
    """One JSON request. Returns (status, parsed_body_or_text)."""
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            status = resp.status
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        status = exc.code
    except urllib.error.URLError as exc:
        return 0, {"error": "cannot reach %s: %s" % (url, exc.reason)}
    except Exception as exc:  # socket timeouts and the like
        return 0, {"error": "cannot reach %s: %s" % (url, exc)}
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, {"error": raw[:500]}


def device_status(device, token, timeout=DEFAULT_TIMEOUT):
    """One device's snapshot. Never raises: a device that is down is a result."""
    status, body = http_json(device.url + "/api/status", token=token, timeout=timeout)
    if status == 0:
        return {"name": device.name, "host": device.host, "ok": False,
                "error": body.get("error", "unreachable"), "at": now_iso()}
    if status != 200:
        message = body.get("error", {})
        if isinstance(message, dict):
            message = message.get("message", message)
        return {"name": device.name, "host": device.host, "ok": False,
                "error": "HTTP %s: %s" % (status, message), "at": now_iso()}
    row = {
        "name": device.name,
        "host": device.host,
        "group": device.group,
        "ok": True,
        "at": now_iso(),
        "version": body.get("version"),
        "uptime_s": body.get("uptime_s"),
        "os": body.get("os"),
        "arch": body.get("arch"),
        "cpu_count": body.get("cpu_count"),
        "memory_total_bytes": body.get("memory_total_bytes"),
        "disk_free_bytes": body.get("disk_free_bytes"),
        "provider": body.get("provider"),
        "model": body.get("model"),
        "exec_enabled": body.get("exec_enabled"),
    }
    # The probe is the device's own way of reporting what the snapshot does
    # not know. Its output is kept verbatim - parsing it here would mean
    # guessing a format for every kind of device.
    if device.probe:
        result = device_exec(device, device.probe, token, timeout=timeout)
        row["probe_ok"] = result.get("ok", False)
        row["probe_output"] = result.get("output", "")[:8000]
        if not result.get("ok") and result.get("error"):
            row["probe_error"] = result["error"]
    return row


def device_exec(device, command, token, timeout=60, cwd=None):
    """Run one command on a device. Returns the device's own answer."""
    body = {"command": command, "timeout_ms": int(timeout * 1000)}
    if cwd:
        body["cwd"] = cwd
    status, answer = http_json(device.url + "/api/exec", method="POST", body=body,
                               token=token, timeout=timeout + 5)
    if status == 0:
        return {"ok": False, "error": answer.get("error", "unreachable"),
                "exit_code": -1, "output": ""}
    if status != 200:
        message = answer.get("error", "")
        if isinstance(message, dict):
            message = message.get("message", message)
        return {"ok": False, "error": "HTTP %s: %s" % (status, message),
                "exit_code": -1, "output": ""}
    return answer


def device_workspace(device, token, timeout=30):
    """The first workspace a device has, or "" when it has none.

    POST /api/chat insists on a workspace_id, and the fleet cannot know it:
    the workspace is registered on the DEVICE, and a device may have
    several. So the fleet asks the device.
    """
    status, answer = http_json(device.url + "/api/workspaces", token=token, timeout=timeout)
    if status != 200 or not isinstance(answer, dict):
        return ""
    workspaces = answer.get("workspaces") or []
    for entry in workspaces:
        if isinstance(entry, dict) and entry.get("id"):
            return entry["id"]
    return ""


def device_chat(device, prompt, token, timeout=300, workspace_id=None):
    """Ask a DEVICE's own agent to do something, and wait for its answer.

    This is the multi-level path: the device runs the prompt with its own
    provider, its own model and its own context, and answers in its own
    words. The fleet server only relays.
    """
    if not workspace_id:
        workspace_id = device_workspace(device, token)
    if not workspace_id:
        return {"ok": False,
                "error": "%s has no workspace to work in (register one with --workspace)"
                          % device.name}
    body = {"message": prompt, "workspace_id": workspace_id}
    req = urllib.request.Request(
        device.url + "/api/chat", data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + token} if token else
                {"Content-Type": "application/json"},
        method="POST")
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        return {"ok": False, "error": "HTTP %s: %s" % (
            exc.code, exc.read().decode("utf-8", "replace")[:300])}
    except Exception as exc:
        return {"ok": False, "error": "cannot reach %s: %s" % (device.url, exc)}
    content = []
    with resp:
        for raw_line in resp:
            line = raw_line.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                frame = json.loads(payload)
            except ValueError:
                continue
            kind = frame.get("type")
            if kind == "content_delta" and frame.get("text"):
                content.append(frame["text"])
            elif kind == "error":
                return {"ok": False, "error": frame.get("message", "the device agent failed")}
    return {"ok": True, "content": "".join(content).strip()}


# ---------------------------------------------------------------------------
# the engine
# ---------------------------------------------------------------------------

class Fleet(object):
    """The ledger plus the operations on it."""

    def __init__(self, ledger_path=None, data_dir=None, default_token=None):
        self.ledger = Ledger(ledger_path, default_token=default_token)
        self.data_dir = data_dir or DEFAULT_DATA_DIR
        self._lock = threading.Lock()

    # ---- paths ------------------------------------------------------------
    def history_path(self, day=None):
        return os.path.join(self.data_dir, "history", "%s.jsonl" % (day or day_stamp()))

    def report_path(self, day=None):
        return os.path.join(self.data_dir, "reports", "%s.md" % (day or day_stamp()))

    @property
    def audit_path(self):
        return os.path.join(self.data_dir, "audit.log")

    def ensure_dirs(self):
        for sub in ("history", "reports"):
            path = os.path.join(self.data_dir, sub)
            if not os.path.isdir(path):
                os.makedirs(path)

    # ---- audit ------------------------------------------------------------
    def audit(self, action, detail=None, job_id=None, device=None, ok=None, extra=None):
        """One line in the audit log: who did what to which device, and how it went.

        Append-only, one JSON object per line, so it can be tailed, grepped
        and shipped to a log collector without any parsing.
        """
        row = {"at": now_iso(), "action": action}
        if job_id:
            row["job"] = job_id
        if device:
            row["device"] = device
        if ok is not None:
            row["ok"] = bool(ok)
        if detail:
            row["detail"] = detail
        if extra:
            row.update(extra)
        self.ensure_dirs()
        line = json.dumps(row, ensure_ascii=False, sort_keys=True)
        with self._lock:
            with open(self.audit_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        return row

    # ---- collection -------------------------------------------------------
    def collect(self, devices=None, timeout=DEFAULT_TIMEOUT, concurrency=DEFAULT_CONCURRENCY):
        """Ask every device how it is. Returns one row per device.

        A device that does not answer is a row with an error, never an
        exception: one dead board must not stop the report for the other 39.
        """
        if devices is None:
            devices = self.ledger.load().select()
        rows = []

        def one(device):
            token = self.ledger.token_for(device)
            return device_status(device, token, timeout=timeout)

        with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
            for row in pool.map(one, devices):
                rows.append(row)
        rows.sort(key=lambda r: r["name"])
        online = len([r for r in rows if r.get("ok")])
        self.audit("collect", detail="%d/%d device(s) answered" % (online, len(rows)),
                   ok=online == len(rows),
                   extra={"devices": [r["name"] for r in rows]})
        return rows

    def save_collection(self, rows, day=None):
        """Append the rows to today's history and rewrite today's report."""
        self.ensure_dirs()
        with self._lock:
            with open(self.history_path(day), "a", encoding="utf-8") as f:
                for row in rows:
                    f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        report = render_report(rows)
        path = self.report_path(day)
        with open(path, "w", encoding="utf-8") as f:
            f.write(report)
        return path

    def history(self, day=None):
        path = self.history_path(day)
        if not os.path.exists(path):
            return []
        rows = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
        return rows

    # ---- jobs -------------------------------------------------------------
    def run_job(self, command, devices=None, verify=None, rollback=None,
                batch_size=0, canary=0, pause_s=0, max_failure_rate=0.0,
                timeout=120, dry_run=False, job_id=None):
        """Run a command on a set of devices, carefully.

        The order of business:

          1. the canary devices go first. If they fail, nothing else is
             touched - a bad package on one device is a signal, not a reason
             to break forty;
          2. the rest go in batches, and the run stops as soon as the
             failure rate crosses the threshold;
          3. a device that failed is rolled back (when a rollback command is
             given) so it is not left in a half-upgraded state;
          4. every single step goes into the audit log.

        Returns a dict: the job id, the per-device results and the totals.
        """
        if devices is None:
            devices = self.ledger.load().select()
        devices = list(devices)
        if not devices:
            return {"ok": False, "error": "no devices selected", "results": []}
        if job_id is None:
            job_id = "job_" + time.strftime("%Y%m%d_%H%M%S", time.gmtime()) + \
                "_" + base64.b32encode(os.urandom(3)).decode("ascii").rstrip("=").lower()
        self.audit("job_start", detail=command, job_id=job_id,
                   extra={"devices": [d.name for d in devices], "dry_run": bool(dry_run),
                          "canary": canary, "batch_size": batch_size})

        results = []
        stopped = False
        # The canary is a prefix of the list, so it is the same code path.
        plan = []
        if canary > 0:
            plan.append(("canary", devices[:canary]))
            rest = devices[canary:]
        else:
            rest = devices
        if batch_size > 0:
            for i in range(0, len(rest), batch_size):
                plan.append(("batch", rest[i:i + batch_size]))
        elif rest:
            plan.append(("batch", rest))

        for phase_index, (phase, group) in enumerate(plan):
            if stopped:
                for d in group:
                    results.append({"device": d.name, "ok": None, "skipped": True,
                                    "reason": "the run was stopped before this device"})
                continue
            for d in group:
                if stopped:
                    results.append({"device": d.name, "ok": None, "skipped": True,
                                    "reason": "the run was stopped before this device"})
                    continue
                results.append(self._run_on_device(d, command, verify, rollback,
                                                   timeout, dry_run, job_id, phase))
            # The canary decides whether the rest of the fleet gets touched.
            if phase == "canary":
                failed = [r for r in results if r.get("ok") is False]
                if failed:
                    stopped = True
                    log("canary failed on %d device(s) - the rest of the fleet is left alone"
                        % len(failed))
            if not stopped:
                failures = len([r for r in results if r.get("ok") is False])
                attempted = len([r for r in results if r.get("ok") is not None])
                if attempted and max_failure_rate > 0:
                    rate = float(failures) / float(attempted)
                    if rate > max_failure_rate:
                        stopped = True
                        log("failure rate %.0f%% is over the threshold - stopping the run"
                            % (rate * 100))
            # Pause BETWEEN the phases, not after the last one.
            if pause_s and not stopped and phase_index + 1 < len(plan):
                time.sleep(pause_s)

        ok_count = len([r for r in results if r.get("ok") is True])
        failed_count = len([r for r in results if r.get("ok") is False])
        skipped_count = len([r for r in results if r.get("skipped")])
        summary = {"ok": failed_count == 0, "job": job_id, "command": command,
                   "results": results, "succeeded": ok_count, "failed": failed_count,
                   "skipped": skipped_count, "stopped_early": stopped,
                   "dry_run": bool(dry_run), "at": now_iso()}
        self.audit("job_done", detail=command, job_id=job_id, ok=summary["ok"],
                   extra={"succeeded": ok_count, "failed": failed_count,
                          "skipped": skipped_count, "stopped_early": stopped})
        return summary

    def _run_on_device(self, device, command, verify, rollback, timeout, dry_run,
                       job_id, phase):
        token = self.ledger.token_for(device)
        if dry_run:
            self.audit("device_skip", detail=command, job_id=job_id, device=device.name,
                       extra={"phase": phase, "reason": "dry run"})
            return {"device": device.name, "ok": True, "dry_run": True, "phase": phase}
        result = device_exec(device, command, token, timeout=timeout)
        row = {"device": device.name, "phase": phase, "ok": bool(result.get("ok")),
               "exit_code": result.get("exit_code"),
               "output": (result.get("output") or "")[:4000]}
        if result.get("error"):
            row["error"] = result["error"]
        if not row["ok"] and rollback:
            back = device_exec(device, rollback, token, timeout=timeout)
            row["rollback"] = {"ok": bool(back.get("ok")),
                               "output": (back.get("output") or "")[:2000]}
            self.audit("device_rollback", detail=rollback, job_id=job_id,
                       device=device.name, ok=bool(back.get("ok")))
        elif row["ok"] and verify:
            check = device_exec(device, verify, token, timeout=timeout)
            row["verify"] = {"ok": bool(check.get("ok")),
                             "output": (check.get("output") or "")[:2000]}
            if not check.get("ok"):
                row["ok"] = False
                row["error"] = "the verify command failed"
                if rollback:
                    back = device_exec(device, rollback, token, timeout=timeout)
                    row["rollback"] = {"ok": bool(back.get("ok")),
                                       "output": (back.get("output") or "")[:2000]}
        self.audit("device_done", detail=command, job_id=job_id, device=device.name,
                   ok=row["ok"], extra={"phase": phase, "exit_code": row.get("exit_code")})
        return row

    # ---- the multi-level path --------------------------------------------
    def ask_devices(self, prompt, devices=None, timeout=300, workspace_id=None):
        """Let each device's own agent answer the prompt.

        The devices think for themselves, with their own provider and their
        own context; the server only collects the answers. This is what makes
        the setup multi-level rather than a remote shell: the server does not
        have to know how a device is put right, the device does.
        """
        if devices is None:
            devices = self.ledger.load().select()
        rows = []

        def one(device):
            token = self.ledger.token_for(device)
            answer = device_chat(device, prompt, token, timeout=timeout,
                                 workspace_id=workspace_id)
            row = {"device": device.name, "ok": bool(answer.get("ok"))}
            if answer.get("ok"):
                row["content"] = answer.get("content", "")
            else:
                row["error"] = answer.get("error", "the device agent failed")
            return row

        with ThreadPoolExecutor(max_workers=max(1, DEFAULT_CONCURRENCY)) as pool:
            for row in pool.map(one, devices):
                rows.append(row)
        # These rows name the device under "device" (the collection rows use
        # "name"), so the sort key is not the same.
        rows.sort(key=lambda r: r.get("device") or "")
        return rows


# ---------------------------------------------------------------------------
# the report
# ---------------------------------------------------------------------------

def _human_bytes(value):
    try:
        n = float(value)
    except (TypeError, ValueError):
        return "?"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return "%.1f %s" % (n, unit)
        n /= 1024
    return "%.1f TiB" % n


def render_report(rows, title=None):
    """The daily report: a table a human reads in ten seconds."""
    stamp = title or day_stamp()
    out = []
    out.append("# 机群日报 %s" % stamp)
    out.append("")
    up = len([r for r in rows if r.get("ok")])
    down = len(rows) - up
    out.append("- 设备总数: %d" % len(rows))
    out.append("- 在线: %d    离线: %d" % (up, down))
    versions = {}
    for r in rows:
        if r.get("ok") and r.get("version"):
            versions[r["version"]] = versions.get(r["version"], 0) + 1
    if versions:
        out.append("- 版本分布: %s" % ", ".join(
            "%s x%d" % (v, n) for v, n in sorted(versions.items())))
    out.append("")

    out.append("| 设备 | 地址 | 状态 | 版本 | 运行时间 | 系统 | 磁盘可用 |")
    out.append("| --- | --- | --- | --- | --- | --- | --- |")
    for r in sorted(rows, key=lambda x: x.get("name") or ""):
        if r.get("ok"):
            uptime = r.get("uptime_s")
            try:
                uptime = "%d 小时" % (int(uptime) // 3600)
            except (TypeError, ValueError):
                uptime = "?"
            out.append("| %s | %s | 在线 | %s | %s | %s | %s |" % (
                r.get("name"), r.get("host"), r.get("version") or "?", uptime,
                r.get("os") or "?", _human_bytes(r.get("disk_free_bytes"))))
        else:
            out.append("| %s | %s | **离线** | - | - | - | %s |" % (
                r.get("name"), r.get("host"), r.get("error") or "?"))
    out.append("")

    problems = [r for r in rows if not r.get("ok")]
    if problems:
        out.append("## 异常设备")
        out.append("")
        for r in problems:
            out.append("- **%s** (%s): %s" % (r.get("name"), r.get("host"),
                                              r.get("error") or "unknown"))
        out.append("")

    probes = [r for r in rows if r.get("ok") and r.get("probe_output")]
    if probes:
        out.append("## 设备自检输出")
        out.append("")
        for r in probes:
            out.append("### %s" % r.get("name"))
            out.append("")
            out.append("```")
            out.append(r.get("probe_output", "").rstrip())
            out.append("```")
            out.append("")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# the command line
# ---------------------------------------------------------------------------

def parse_labels(pairs):
    out = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise FleetError("a label must be key=value, got %r" % pair)
        key, value = pair.split("=", 1)
        out[key.strip()] = value.strip()
    return out


def main(argv):
    parser = argparse.ArgumentParser(description="the fleet engine")
    parser.add_argument("--ledger", default=DEFAULT_LEDGER, help="the device inventory")
    parser.add_argument("--data-dir", default=DEFAULT_DATA_DIR, help="where history and reports go")
    parser.add_argument("--token", default="",
                        help="the API token of the devices (the same one they were "
                             "started with)")
    parser.add_argument("--token-file", default=DEFAULT_DEVICE_TOKEN_FILE,
                        help="the file the device token is read from")
    parser.add_argument("--group", action="append", default=[], help="only this group")
    parser.add_argument("--device", action="append", default=[], help="only this device")
    parser.add_argument("--label", action="append", default=[], help="only devices with key=value")
    parser.add_argument("--json", action="store_true", help="print JSON, not a table")
    # NOT dest="command": the `exec` subcommand has its own --command
    # option, and the subparser's namespace would overwrite the name of the
    # subcommand with the option's value.
    sub = parser.add_subparsers(dest="subcommand")

    sub.add_parser("devices", help="the inventory")
    collect = sub.add_parser("collect", help="ask every device how it is")
    collect.add_argument("--no-save", action="store_true", help="do not write the history")
    report = sub.add_parser("report", help="print today's report")
    report.add_argument("--day", default="", help="which day (default: today)")

    run = sub.add_parser("exec", help="run a command on the selected devices")
    run.add_argument("--command", required=True)
    run.add_argument("--verify", default="", help="a command that must succeed afterwards")
    run.add_argument("--rollback", default="", help="a command that undoes the change")
    run.add_argument("--batch-size", type=int, default=0, help="devices per batch (0 = all at once)")
    run.add_argument("--canary", type=int, default=0, help="devices to try first")
    run.add_argument("--pause", type=float, default=0, help="seconds between batches")
    run.add_argument("--max-failure-rate", type=float, default=0.0,
                     help="stop the run when the failure rate is over this (0 = never stop)")
    run.add_argument("--timeout", type=float, default=120)
    run.add_argument("--dry-run", action="store_true")

    upgrade = sub.add_parser("upgrade", help="upgrade a package on the selected devices")
    upgrade.add_argument("--package", required=True)
    upgrade.add_argument("--manager", default="auto", choices=["auto", "apt", "dnf", "apk", "yum"])
    upgrade.add_argument("--verify", default="")
    upgrade.add_argument("--rollback", default="")
    upgrade.add_argument("--batch-size", type=int, default=5)
    upgrade.add_argument("--canary", type=int, default=1)
    upgrade.add_argument("--pause", type=float, default=30)
    upgrade.add_argument("--max-failure-rate", type=float, default=0.0)
    upgrade.add_argument("--timeout", type=float, default=600)
    upgrade.add_argument("--dry-run", action="store_true")

    ask = sub.add_parser("ask", help="let each device's own agent answer a prompt")
    ask.add_argument("--prompt", required=True)
    ask.add_argument("--timeout", type=float, default=300)

    args = parser.parse_args(argv)
    if not args.subcommand:
        parser.print_help()
        return 2

    token = args.token or read_token_file(args.token_file) or None
    fleet = Fleet(args.ledger, args.data_dir, default_token=token)
    try:
        fleet.ledger.load()
    except FleetError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    try:
        devices = fleet.ledger.select(groups=args.group, names=args.device,
                                      labels=parse_labels(args.label))
    except FleetError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1

    if args.subcommand == "devices":
        rows = [{"name": d.name, "host": d.host, "port": d.port, "group": d.group,
                 "enabled": d.enabled, "labels": d.labels} for d in devices]
        if args.json:
            print(json.dumps(rows, indent=2, ensure_ascii=False))
        else:
            for r in rows:
                print("%-24s %-16s %-12s %s" % (r["name"], "%s:%d" % (r["host"], r["port"]),
                                                r["group"] or "-",
                                                "enabled" if r["enabled"] else "disabled"))
            print("\n%d device(s)" % len(rows))
        return 0

    if args.subcommand == "collect":
        # A device that has a probe in the ledger runs it as part of the
        # collection - that is where the free space of a data disk or the
        # version of the app on it comes from.
        rows = fleet.collect(devices)
        path = None
        if not args.no_save:
            path = fleet.save_collection(rows)
        if args.json:
            print(json.dumps(rows, indent=2, ensure_ascii=False))
        else:
            for r in rows:
                if r.get("ok"):
                    print("%-24s ok    %-8s %s" % (r["name"], r.get("version") or "?",
                                                  r.get("os") or ""))
                else:
                    print("%-24s FAIL  %s" % (r["name"], r.get("error")))
            print("\n%d/%d online" % (len([r for r in rows if r.get("ok")]), len(rows)))
            if path:
                print("report: %s" % path)
        return 0 if all(r.get("ok") for r in rows) else 1

    if args.subcommand == "report":
        rows = fleet.history(args.day or None)
        if not rows:
            # No history: report what a fresh collection says.
            rows = fleet.collect(devices)
        print(render_report(rows, args.day or None), end="")
        return 0

    if args.subcommand == "exec":
        summary = fleet.run_job(args.command, devices, verify=args.verify or None,
                                rollback=args.rollback or None, batch_size=args.batch_size,
                                canary=args.canary, pause_s=args.pause,
                                max_failure_rate=args.max_failure_rate,
                                timeout=args.timeout, dry_run=args.dry_run)
        if args.json:
            print(json.dumps(summary, indent=2, ensure_ascii=False))
        else:
            for r in summary["results"]:
                if r.get("skipped"):
                    print("%-24s skipped  %s" % (r["device"], r.get("reason", "")))
                else:
                    print("%-24s %-6s exit=%s" % (r["device"],
                                                  "ok" if r.get("ok") else "FAIL",
                                                  r.get("exit_code")))
            print("\n%d ok, %d failed, %d skipped (job %s)" % (
                summary["succeeded"], summary["failed"], summary["skipped"],
                summary["job"]))
        return 0 if summary["ok"] else 1

    if args.subcommand == "upgrade":
        command, rollback = upgrade_commands(args.package, args.manager, args.rollback)
        summary = fleet.run_job(command, devices, verify=args.verify or None,
                                rollback=rollback, batch_size=args.batch_size,
                                canary=args.canary, pause_s=args.pause,
                                max_failure_rate=args.max_failure_rate,
                                timeout=args.timeout, dry_run=args.dry_run)
        if args.json:
            print(json.dumps(summary, indent=2, ensure_ascii=False))
        else:
            for r in summary["results"]:
                if r.get("skipped"):
                    print("%-24s skipped  %s" % (r["device"], r.get("reason", "")))
                else:
                    print("%-24s %-6s exit=%s" % (r["device"],
                                                  "ok" if r.get("ok") else "FAIL",
                                                  r.get("exit_code")))
            print("\n%d ok, %d failed, %d skipped (job %s)" % (
                summary["succeeded"], summary["failed"], summary["skipped"],
                summary["job"]))
        return 0 if summary["ok"] else 1

    if args.subcommand == "ask":
        rows = fleet.ask_devices(args.prompt, devices, timeout=args.timeout)
        if args.json:
            print(json.dumps(rows, indent=2, ensure_ascii=False))
        else:
            for r in rows:
                print("=== %s ===" % r["device"])
                print(r.get("content") or r.get("error") or "")
                print("")
        return 0 if all(r.get("ok") for r in rows) else 1

    parser.print_help()
    return 2


def upgrade_commands(package, manager, rollback_cmd):
    """The command that upgrades a package, and the one that undoes it.

    `auto` picks the package manager the device actually has - the fleet is
    not all one kind of machine, and a school has a mix of Debian boards and
    maybe one Fedora box.
    """
    if manager == "apt":
        return "apt-get install -y %s" % package, rollback_cmd
    if manager == "dnf":
        return "dnf install -y %s" % package, rollback_cmd
    if manager == "yum":
        return "yum install -y %s" % package, rollback_cmd
    if manager == "apk":
        return "apk add --no-cache %s" % package, rollback_cmd
    auto = ("if command -v apt-get >/dev/null 2>&1; then apt-get install -y %s; "
            "elif command -v dnf >/dev/null 2>&1; then dnf install -y %s; "
            "elif command -v yum >/dev/null 2>&1; then yum install -y %s; "
            "elif command -v apk >/dev/null 2>&1; then apk add --no-cache %s; "
            "else echo 'no known package manager' >&2; exit 127; fi"
            % (package, package, package, package))
    return auto, rollback_cmd


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
