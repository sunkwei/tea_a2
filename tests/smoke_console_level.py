#!/usr/bin/env python3
"""Console verbosity policy for the server.

A server left running in a terminal must not scroll per-request INFO lines past
the operator: the console defaults to WARN, while the log *file* keeps the full
story at INFO. `--console-level` widens it again, `--no-console` / `--quiet`
turn it off, and a typo must not silently widen it.

    python3 tests/smoke_console_level.py [path-to-tea_agent]

Local only: a fake provider is never contacted.
"""
import http.client
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXE = sys.argv[1] if len(sys.argv) > 1 else os.path.join(REPO_ROOT, "build", "bin", "tea_agent")

results = []


def check(name, ok, detail=""):
    results.append((name, ok))
    print(("PASS " if ok else "FAIL ") + name + (("  " + str(detail)) if (detail and not ok) else ""))


def free_port():
    for _ in range(50):
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        finally:
            s.close()
        if port > 1024:
            return port
    raise RuntimeError("no free port")


def wait_health(port, timeout=30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            c.request("GET", "/api/health")
            r = c.getresponse()
            body = r.read().decode("utf-8", "replace")
            c.close()
            if r.status == 200 and json.loads(body).get("ok"):
                return True
        except Exception:
            pass
        time.sleep(0.15)
    return False


def stop(proc):
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def write_config(tmp, port):
    path = os.path.join(tmp, "provider.yaml")
    with open(path, "w", encoding="utf-8") as f:
        f.write(
            "version: 1\n"
            "providers:\n"
            "  mock:\n"
            f"    api_url: http://127.0.0.1:{port}\n"
            "    api_key: sk-test\n"
            "    default_model: m\n"
            "    models: {m: {}}\n"
            "default: {provider: mock, model: m}\n"
        )
    return path


def run(tmp, extra_args, hit_api=True, settle=0.6):
    """Start the agent, optionally make one request that logs at INFO, stop it.

    Returns (console_text, logfile_text)."""
    home = os.path.join(tmp, "home_" + str(abs(hash(tuple(extra_args))) % 100000))
    ws = os.path.join(tmp, "ws")
    os.makedirs(home, exist_ok=True)
    os.makedirs(ws, exist_ok=True)
    log_file = os.path.join(tmp, "run_%d.log" % (abs(hash(tuple(extra_args))) % 100000))
    port = free_port()
    proc = subprocess.Popen(
        [EXE, "--host", "127.0.0.1", "--port", str(port), "--tea-home", home,
         "--workspace", ws, "--log-file", log_file] + extra_args,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
        errors="replace")
    try:
        if not wait_health(port):
            raise RuntimeError("agent did not become healthy")
        if hit_api:
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            c.request("GET", "/api/providers")
            c.getresponse().read()
            c.close()
        time.sleep(settle)
    finally:
        stop(proc)
    out = proc.stdout.read() if proc.stdout else ""
    try:
        with open(log_file, "r", encoding="utf-8", errors="replace") as f:
            log_text = f.read()
    except OSError:
        log_text = ""
    return out, log_text


def main():
    if not os.path.isfile(EXE):
        print("missing binary: " + EXE)
        return 2
    tmp = tempfile.mkdtemp(prefix="tea_console_")
    write_config(tmp, free_port())

    # 1) Default: the console only carries WARN/ERROR, the file still gets INFO.
    default_out, default_log = run(tmp, [])
    console_lines = [l for l in default_out.splitlines()
                     if re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \[", l)]
    levels = set(re.findall(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \[([A-Z]+)\]",
                            "\n".join(console_lines), re.M))
    check("default console carries no INFO lines",
          "INFO" not in levels, "levels seen: %s" % sorted(levels))
    check("default console keeps WARNING/ERROR visible at all times",
          levels <= {"WARN", "ERROR"}, "levels seen: %s" % sorted(levels))
    check("the greeting banner is still printed", "Press Ctrl+C to stop." in default_out,
          default_out[:400])
    check("the log file still records INFO lines", "[INFO]" in default_log,
          default_log[:400])
    check("the log file records the console policy",
          "[INFO] console: WARN (log file: INFO)" in default_log, default_log[:400])
    check("the banner is not duplicated as a log line",
          default_out.count("Press Ctrl+C to stop.") == 1)

    # 2) --console-level info restores the per-request chatter on the terminal.
    info_out, _ = run(tmp, ["--console-level", "info"])
    info_levels = set(re.findall(r"\d{2}:\d{2}:\d{2} \[([A-Z]+)\]", info_out))
    check("--console-level info echoes INFO to the console", "INFO" in info_levels,
          "levels seen: %s" % sorted(info_levels))
    check("--log-file receives the same INFO lines",
          "[INFO] server: listening on http://" in run(tmp, ["--console-level", "info"])[1])

    # 3) --no-console and --quiet silence the log echo entirely.
    for flag in ("--no-console", "--quiet"):
        out, log_text = run(tmp, [flag])
        stamped = re.findall(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \[", out, re.M)
        check(flag + " echoes no log lines", not stamped, "%d stamped lines" % len(stamped))
        check(flag + " still writes the log file", "[INFO] server: listening" in log_text)
        if flag == "--quiet":
            check("--quiet also drops the banner", "Press Ctrl+C to stop." not in out,
                  out[:200])

    # 4) A typo in --console-level must not silently widen the console.
    typo_out, typo_log = run(tmp, ["--console-level", "verbose"])
    check("a bad --console-level is reported", "is not a level; using warn" in typo_out,
          typo_out[:400])
    check("a bad --console-level falls back to WARN",
          "INFO" not in set(re.findall(r"\d{2}:\d{2}:\d{2} \[([A-Z]+)\]", typo_out)))

    # 5) --log-level still controls the file, independently of the console.
    err_out, err_log = run(tmp, ["--log-level", "error"])
    check("--log-level error narrows the log file", "[INFO]" not in err_log, err_log[:300])
    check("--help documents both thresholds",
          "--console-level" in subprocess.run([EXE, "--help"], capture_output=True,
                                              text=True).stdout)

    failed = [n for n, ok in results if not ok]
    print("\n%d/%d checks passed" % (len(results) - len(failed), len(results)))
    print("ALL PASS" if not failed else "%d FAILED" % len(failed))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
