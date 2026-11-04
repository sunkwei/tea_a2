#!/usr/bin/env python3
"""Smoke test: configuring a provider gives you a workspace to work in.

A fresh install has no provider.yaml and no workspace. The person configures a
provider - one API key through the quick setup, or an uploaded provider.yaml -
and the conversation needs somewhere to live: the file tools, the sessions and
the trajectories all hang off a workspace.

So both configuration paths create a default workspace at $HOME when (and only
when) there is none. What this pins:

  * a fresh install has no workspace at all;
  * uploading a provider.yaml creates one workspace, at $HOME, and says so in
    the response;
  * the quick setup (one API key) does the same;
  * the workspace lands in the index, so it survives a restart;
  * replacing the config again does not pile up a second workspace;
  * a workspace that already exists is never touched: the home directory is not
    added next to it, and the response does not claim one was created;
  * the runtime directory (.tea_agent_run) really is materialised, so the first
    chat has somewhere to write.
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


class Agent:
    """One agent process with its own home directory."""

    def __init__(self, binary, home, tea_home, extra_args=None):
        self.binary = binary
        self.home = home
        self.tea_home = tea_home
        self.port = free_port()
        self.proc = subprocess.Popen(
            [binary, "--tea-home", tea_home, "--host", "127.0.0.1", "--port", str(self.port),
             "--no-console", "--log-file", os.path.join(tea_home, "logs", "tea_agent.log")]
            + list(extra_args or []),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env=dict(os.environ, HOME=home))

    def wait(self):
        return wait_health(self.port)

    def stop(self):
        try:
            self.proc.terminate()
            self.proc.wait(timeout=10)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass


def main():
    binary = (sys.argv[1] if len(sys.argv) > 1
              else os.path.join(REPO_ROOT, "build", "bin", "tea_agent"))
    if not os.path.exists(binary):
        print("binary not found:", binary)
        return 1

    tmp = tempfile.mkdtemp(prefix="tea_ws_default_")
    failures = []
    checks = []

    def check(name, ok, detail=""):
        checks.append(name)
        print(("PASS " if ok else "FAIL ") + name + (("  " + str(detail)) if not ok else ""))
        if not ok:
            failures.append(name)

    try:
        # ---- the upload path ---------------------------------------------
        home = os.path.join(tmp, "upload_home")
        tea_home = os.path.join(home, ".tea_agent")
        os.makedirs(home)
        agent = Agent(binary, home, tea_home)
        try:
            check("the agent comes up", agent.wait(), agent.port)
            _, ws, _ = call(agent.port, "GET", "/api/workspaces")
            check("a fresh install has no workspace",
                  ws.get("workspaces") == [], ws.get("workspaces"))

            status, up, _ = call(agent.port, "POST", "/api/config", {"yaml": PROVIDER_YAML})
            check("the upload is applied", status == 200 and up.get("ok") is True, (status, up))
            created = up.get("workspace") or {}
            check("the upload says which workspace it made", bool(created.get("id")), up)

            _, ws, _ = call(agent.port, "GET", "/api/workspaces")
            entries = ws.get("workspaces") or []
            check("there is exactly one workspace now", len(entries) == 1, entries)
            if entries:
                want = os.path.realpath(home)
                got = os.path.realpath(entries[0].get("path") or "")
                check("it is the home directory", got == want, (got, want))
                check("the response and the list agree",
                      created.get("id") == entries[0].get("id"), (created, entries[0]))
            check("the runtime directory is there",
                  os.path.isdir(os.path.join(home, ".tea_agent_run")),
                  os.path.join(home, ".tea_agent_run"))
            index = os.path.join(tea_home, "workspaces.json")
            check("the workspace is in the index", os.path.isfile(index), index)
            if os.path.isfile(index):
                saved = json.load(open(index, encoding="utf-8"))
                paths = [w.get("path") for w in (saved.get("workspaces") or [])]
                check("the index holds the home directory",
                      any(os.path.realpath(p or "") == os.path.realpath(home) for p in paths), paths)

            # ---- replacing the config does not pile up workspaces ---------
            status, up2, _ = call(agent.port, "POST", "/api/config", {"yaml": PROVIDER_YAML})
            check("the config can be replaced", status == 200, (status, up2))
            check("the second upload created nothing", not (up2.get("workspace") or {}).get("id"),
                  up2.get("workspace"))
            _, ws, _ = call(agent.port, "GET", "/api/workspaces")
            check("there is still exactly one workspace",
                  len(ws.get("workspaces") or []) == 1, ws.get("workspaces"))
        finally:
            agent.stop()

        # ---- it survives a restart ---------------------------------------
        agent = Agent(binary, home, tea_home)
        try:
            check("the agent comes up again", agent.wait(), agent.port)
            _, ws, _ = call(agent.port, "GET", "/api/workspaces")
            entries = ws.get("workspaces") or []
            check("the workspace survived the restart", len(entries) == 1, entries)
            if entries:
                got = os.path.realpath(entries[0].get("path") or "")
                check("it is still the home directory",
                      got == os.path.realpath(home), (got, os.path.realpath(home)))
        finally:
            agent.stop()

        # ---- the quick setup path ----------------------------------------
        home2 = os.path.join(tmp, "quick_home")
        tea_home2 = os.path.join(home2, ".tea_agent")
        os.makedirs(home2)
        agent = Agent(binary, home2, tea_home2)
        try:
            check("the agent comes up", agent.wait(), agent.port)
            status, done, _ = call(agent.port, "POST", "/api/config/quickstart",
                                   {"api_key": "sk-quickstart-0123456789"})
            check("the quick setup succeeds", status == 200 and done.get("ok") is True,
                  (status, done))
            created = done.get("workspace") or {}
            check("the quick setup says which workspace it made", bool(created.get("id")), done)
            _, ws, _ = call(agent.port, "GET", "/api/workspaces")
            entries = ws.get("workspaces") or []
            check("there is exactly one workspace now", len(entries) == 1, entries)
            if entries:
                got = os.path.realpath(entries[0].get("path") or "")
                check("it is the home directory", got == os.path.realpath(home2),
                      (got, os.path.realpath(home2)))
            check("the runtime directory is there",
                  os.path.isdir(os.path.join(home2, ".tea_agent_run")),
                  os.path.join(home2, ".tea_agent_run"))
        finally:
            agent.stop()

        # ---- an existing workspace is never touched ------------------------
        home3 = os.path.join(tmp, "existing_home")
        tea_home3 = os.path.join(home3, ".tea_agent")
        project = os.path.join(tmp, "project")
        os.makedirs(home3)
        os.makedirs(project)
        agent = Agent(binary, home3, tea_home3, extra_args=["--workspace", project])
        try:
            check("the agent comes up", agent.wait(), agent.port)
            _, ws, _ = call(agent.port, "GET", "/api/workspaces")
            entries = ws.get("workspaces") or []
            check("the project is registered", len(entries) == 1
                  and os.path.realpath(entries[0].get("path") or "") == os.path.realpath(project),
                  entries)
            before = entries[0].get("id") if entries else None

            status, up, _ = call(agent.port, "POST", "/api/config", {"yaml": PROVIDER_YAML})
            check("the upload is applied", status == 200 and up.get("ok") is True, (status, up))
            check("the upload created nothing", not (up.get("workspace") or {}).get("id"),
                  up.get("workspace"))
            _, ws, _ = call(agent.port, "GET", "/api/workspaces")
            entries = ws.get("workspaces") or []
            check("the existing workspace is still the only one", len(entries) == 1, entries)
            if entries:
                check("it is the same workspace", entries[0].get("id") == before,
                      (before, entries[0].get("id")))
                got = os.path.realpath(entries[0].get("path") or "")
                check("the home directory was not added", got == os.path.realpath(project),
                      (got, os.path.realpath(project)))
            check("the home directory did not become a workspace",
                  not os.path.isdir(os.path.join(home3, ".tea_agent_run")),
                  os.path.join(home3, ".tea_agent_run"))

            # The same for the quick setup.
            status, done, _ = call(agent.port, "POST", "/api/config/quickstart",
                                   {"api_key": "sk-another-0123456789"})
            check("the quick setup succeeds", status == 200, (status, done))
            check("the quick setup created nothing", not (done.get("workspace") or {}).get("id"),
                  done.get("workspace"))
            _, ws, _ = call(agent.port, "GET", "/api/workspaces")
            check("there is still exactly one workspace",
                  len(ws.get("workspaces") or []) == 1, ws.get("workspaces"))
        finally:
            agent.stop()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    print("%d/%d checks passed" % (len(checks) - len(failures), len(checks)))
    if failures:
        print("FAILED:", ", ".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
