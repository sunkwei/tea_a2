#!/usr/bin/env python3
"""The tray host (appstub) asks the programs it hosts for their version.

What this pins:

  * the version of a hosted program shows up in the tray menu - it is asked
    the way the program is normally run, plus "--version";
  * a program that does not know the flag shows *nothing*: a made up version
    is worse than no version;
  * "appstub --version" prints the same thing on the command line, and it
    starts nothing while it does so.

    python3 tests/smoke_appstub_version.py [path-to-tea_agent]

Windows only (appstub is a Windows program): on other platforms the test
says so and stops.
"""
import json
import os
import subprocess
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXE = sys.argv[1] if len(sys.argv) > 1 else os.path.join(REPO_ROOT, "build", "bin", "tea_agent")

results = []


def check(name, ok, detail=""):
    results.append((name, ok))
    print(("PASS " if ok else "FAIL ") + name + (("  " + str(detail)) if (detail and not ok) else ""))


def appstub_path():
    """appstub.exe lives next to tea_agent.exe."""
    if os.name != "nt":
        return None
    if not os.path.isfile(EXE):
        return None
    candidate = os.path.join(os.path.dirname(os.path.abspath(EXE)), "appstub.exe")
    return candidate if os.path.isfile(candidate) else None


def main():
    appstub = appstub_path()
    if appstub is None:
        print("SKIP: no appstub.exe next to %s (Windows only)" % EXE)
        return 0

    # What the agent says about itself - the answer the menu has to show.
    probe = subprocess.run([EXE, "--version"], stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, timeout=60)
    agent_version = probe.stdout.decode("utf-8", "replace").strip().splitlines()
    agent_version = agent_version[0] if agent_version else ""
    check("the agent answers --version", bool(agent_version), agent_version)

    tmp = tempfile.mkdtemp(prefix="appstub-version-")
    config = {
        "log_dir": tmp.replace("\\", "\\\\"),
        "apps": [
            # The real agent: it knows "--version".
            {"id": "agent", "name": "agent", "command": EXE, "args": [],
             "autostart": True},
            # A program that answers with a version of its own.
            {"id": "demo", "name": "demo", "command": sys.executable,
             "args": ["-c", "print('demo-app 9.9.9')"], "autostart": True},
            # A program that does not know the flag: it fails.
            {"id": "silent", "name": "silent", "command": sys.executable,
             "args": ["-c", "import sys; sys.exit(3)"], "autostart": True},
        ],
    }
    config_path = os.path.join(tmp, "config.json")
    with open(config_path, "w", encoding="utf-8") as fh:
        json.dump(config, fh, indent=2)

    run = subprocess.run([appstub, "--config", config_path, "--version"],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)
    out = run.stdout.decode("utf-8", "replace")

    check("appstub --version exits with 0", run.returncode == 0, run.returncode)
    check("it prints its own version", "appstub " in out, out)
    check("the agent's version is in the output", agent_version and agent_version in out, out)
    check("a program of its own is asked the same way", "demo-app 9.9.9" in out, out)
    check("a program without --version shows no version", "(no version)" in out, out)

    # "--version" is a question, not a start: nothing may be launched.
    log_path = os.path.join(tmp, "appstub.log")
    log = ""
    if os.path.isfile(log_path):
        with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
            log = fh.read()
    check("nothing was started", "started (pid" not in log, log[-400:])
    check("the versions are in the log", agent_version and agent_version in log, log[-400:])

    passed = sum(1 for _, ok in results if ok)
    print("%d/%d checks passed" % (passed, len(results)))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
