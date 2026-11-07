#!/usr/bin/env python3
"""Smoke test: a workspace path may be written with $HOME (or ~).

People type the paths they have in front of them. A path copied out of a
`.bashrc`, a README or a ticket arrives as `$HOME/projects/foo` - and on
Windows as `%USERPROFILE%\\projects\\foo`. The agent's home directory is the
one that matters (it is the machine the agent runs on), so the server expands
those spellings before it looks at the path.

What this pins:

  * `$HOME/project`, `${HOME}/project` and `~/project` all register the same
    directory, and the index holds the **expanded** path (so the UI, the file
    tools and the boundary check all see a real path);
  * `create: true` creates the directory under the expanded home;
  * a path that does not exist is still refused (with the expanded path in the
    message, so the user can see what was looked for);
  * a relative path is still refused - and so is `$HOMEDIR/...`: that is a
    different variable, not the home directory with a suffix;
  * the command line does the same: `--workspace '$HOME/project'` (quoted, so
    the shell does not expand it) registers the expanded path.

    python3 tests/smoke_workspace_home_var.py [path-to-tea_agent]
"""

import http.client
import json
import os
import random
import shutil
import socket
import subprocess
import sys
import tempfile
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PROVIDER_YAML = """version: 1
providers:
  mock:
    api_url: http://127.0.0.1:9/v1
    api_key: sk-test
    models:
      see: {}
"""


def free_port():
    while True:
        port = random.randint(20000, 60000)
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue


def wait_health(port, timeout=30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            conn.request("GET", "/api/health")
            if conn.getresponse().status == 200:
                return True
        except OSError:
            pass
        time.sleep(0.2)
    return False


def call(port, method, path, body=None, timeout=30):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    headers = {"Content-Type": "application/json"} if body is not None else {}
    conn.request(method, path, body=json.dumps(body) if body is not None else None,
                 headers=headers)
    resp = conn.getresponse()
    raw = resp.read()
    try:
        return resp.status, json.loads(raw or b"null"), raw.decode("utf-8", "replace")
    except ValueError:
        return resp.status, {"raw": raw.decode("utf-8", "replace")}, raw.decode("utf-8", "replace")


def main():
    binary = (sys.argv[1] if len(sys.argv) > 1
              else os.path.join(REPO_ROOT, "build", "bin", "tea_agent"))
    if not os.path.exists(binary):
        print("binary not found:", binary)
        return 1

    tmp = tempfile.mkdtemp(prefix="tea_homevar_")
    home = os.path.join(tmp, "home")
    tea_home = os.path.join(home, ".tea_agent")
    os.makedirs(home)
    for name in ("proj_a", "proj_b", "proj_c"):
        os.makedirs(os.path.join(home, name))
    config = os.path.join(tmp, "provider.yaml")
    with open(config, "w", encoding="utf-8") as f:
        f.write(PROVIDER_YAML)

    failures = []
    checks = []

    def check(name, ok, detail=""):
        checks.append(name)
        print(("PASS " if ok else "FAIL ") + name + (("  " + str(detail)) if not ok else ""))
        if not ok:
            failures.append(name)

    def real(p):
        return os.path.realpath(p or "")

    port = free_port()
    env = dict(os.environ, HOME=home, USERPROFILE=home)
    proc = subprocess.Popen([
        binary, "--config", config, "--tea-home", tea_home,
        "--host", "127.0.0.1", "--port", str(port), "--no-console",
        "--log-file", os.path.join(tmp, "agent.log"),
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    try:
        if not wait_health(port):
            print("server did not become healthy")
            return 1

        # ---- the three spellings of the home directory --------------------
        for label, path, target in (
            ("$HOME", "$HOME/proj_a", os.path.join(home, "proj_a")),
            ("${HOME}", "${HOME}/proj_b", os.path.join(home, "proj_b")),
            ("~", "~/proj_c", os.path.join(home, "proj_c")),
        ):
            status, data, _ = call(port, "POST", "/api/workspaces", {"path": path})
            check("%s is accepted" % label, status == 200 and (data or {}).get("id"),
                  (status, data))
            check("%s registers the expanded path" % label,
                  real((data or {}).get("path")) == real(target),
                  ((data or {}).get("path"), real(target)))
            check("%s does not leak into the index" % label,
                  "$HOME" not in json.dumps(data) and "${HOME}" not in json.dumps(data),
                  data)

        _, listing, _ = call(port, "GET", "/api/workspaces")
        paths = [w.get("path") for w in (listing.get("workspaces") or [])]
        check("the index holds the expanded paths",
              all("$HOME" not in (p or "") for p in paths), paths)
        check("all three directories are registered",
              all(real(p) in [real(os.path.join(home, n)) for n in ("proj_a", "proj_b", "proj_c")]
                  for p in paths), paths)

        # ---- the name comes from the real directory ------------------------
        _, one, _ = call(port, "POST", "/api/workspaces", {"path": "$HOME/proj_a"})
        check("the name is the directory name, not '$HOME'",
              (one or {}).get("name") == "proj_a", (one or {}).get("name"))

        # ---- create: true makes the directory under the expanded home ------
        status, made, _ = call(port, "POST", "/api/workspaces",
                               {"path": "$HOME/proj_new", "create": True})
        check("create: true works with $HOME", status == 200 and (made or {}).get("id"),
              (status, made))
        check("the directory was really created",
              os.path.isdir(os.path.join(home, "proj_new")), os.path.join(home, "proj_new"))

        # ---- a missing directory is still refused --------------------------
        status, err, _ = call(port, "POST", "/api/workspaces", {"path": "$HOME/nope"})
        check("a missing directory is refused", status == 400, (status, err))
        message = ((err or {}).get("error") or {}).get("message") or ""
        check("the message names the expanded path",
              real(os.path.join(home, "nope")) in message, err)

        # ---- relative paths are still refused ------------------------------
        status, err, _ = call(port, "POST", "/api/workspaces", {"path": "relative/dir"})
        check("a relative path is refused", status == 400, (status, err))
        check("the refusal says what a full path looks like",
              "absolute" in ((err or {}).get("error", {}).get("message") or "").lower()
              or "full path" in ((err or {}).get("error", {}).get("message") or "").lower(),
              err)
        status, err, _ = call(port, "POST", "/api/workspaces", {"path": "$HOMEDIR/dir"})
        check("$HOMEDIR is not $HOME (it is another variable)", status == 400, (status, err))
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            proc.kill()

    # ---- the command line does the same -----------------------------------
    cli_home = os.path.join(tmp, "cli_home")
    os.makedirs(os.path.join(cli_home, "cli_proj"))
    cli_port = free_port()
    cli = subprocess.Popen([
        binary, "--config", config, "--tea-home", os.path.join(cli_home, ".tea_agent"),
        "--host", "127.0.0.1", "--port", str(cli_port), "--no-console",
        "--log-file", os.path.join(tmp, "cli.log"),
        # Quoted, so the shell does NOT expand it: the agent has to.
        "--workspace", "$HOME/cli_proj",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env=dict(os.environ, HOME=cli_home, USERPROFILE=cli_home))
    try:
        if not wait_health(cli_port):
            print("the command line instance did not become healthy")
            return 1
        _, listing, _ = call(cli_port, "GET", "/api/workspaces")
        paths = [w.get("path") for w in (listing.get("workspaces") or [])]
        check("--workspace '$HOME/...' is expanded too",
              any(real(p) == real(os.path.join(cli_home, "cli_proj")) for p in paths), paths)
    finally:
        try:
            cli.terminate()
            cli.wait(timeout=10)
        except Exception:
            cli.kill()

    shutil.rmtree(tmp, ignore_errors=True)

    print()
    print("%d/%d checks passed" % (len(checks) - len(failures), len(checks)))
    if failures:
        print("FAILED:", ", ".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
