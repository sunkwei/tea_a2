#!/usr/bin/env python3
"""Checks the single-file deployment story: embedded CA roots and a static binary.

Two things make "copy one file to the device" work, and both are easy to break:

  1. the trusted roots are compiled into the ELF, so https:// works with no
     sidecar PEM next to the binary;
  2. the binary does not depend on the build host's glibc version.

Point 2 has a trap that is not obvious from `file`: a statically linked glibc
cannot dlopen NSS modules, and on hosts whose /etc/nsswitch.conf lists
systemd-resolved (`resolve`) or mDNS, getaddrinfo segfaults. The binary pins the
hosts database to "files dns" for that reason; this test resolves a real name
through the binary's own code path so a regression shows up as a crash rather
than a support ticket.

Usage: python3 tests/smoke_single_file.py [path-to-tea_agent-binary]

Set TEA_EXPECT_STATIC=0 when checking a deliberately dynamic build
(-DTEA_AGENT_STATIC_LIBC=OFF): the static assertions and the NSS crash check
are then skipped, and only the embedded-CA-bundle part is verified.
"""

import http.client
import json
import os
import random
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PEM_MARKER = b"-----BEGIN CERTIFICATE-----"


def free_port():
    while True:
        port = random.randint(20000, 60000)
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue


def wait_health(port, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            conn.request("GET", "/api/health")
            if conn.getresponse().status == 200:
                return True
        except OSError:
            pass
        time.sleep(0.15)
    return False


def main():
    binary = (
        sys.argv[1] if len(sys.argv) > 1 else os.path.join(REPO_ROOT, "build", "bin", "tea_agent")
    )
    if not os.path.exists(binary):
        print("binary not found:", binary)
        return 1

    failures = []
    tmp = tempfile.mkdtemp(prefix="tea_single_")
    os.makedirs(os.path.join(tmp, "home"))
    os.makedirs(os.path.join(tmp, "ws"))
    config = os.path.join(tmp, "provider.yaml")
    with open(config, "w", encoding="utf-8") as f:
        f.write(
            """version: 1
providers:
  local:
    api_url: http://127.0.0.1:1
    api_key: sk-test
    default_model: m
    models: {m: {supports_tools: true}}
"""
        )

    try:
        # ---- 1) the CA bundle is inside the binary ------------------------
        with open(binary, "rb") as f:
            blob = f.read()
        pem_count = blob.count(PEM_MARKER)
        if pem_count < 50:
            failures.append(
                "1: expected the CA bundle inside the ELF, found %d certificate markers" % pem_count
            )

        # ---- 2) the binary is static (ELF) --------------------------------
        expect_static = os.environ.get("TEA_EXPECT_STATIC", "1") != "0"
        kind = subprocess.run(["file", "-b", binary], capture_output=True, text=True).stdout
        is_static = "statically linked" in kind
        # Only assert on ELF: on Windows/macOS this check does not apply.
        if expect_static and "ELF" in kind and not is_static:
            failures.append("2: expected a statically linked ELF, got: %s" % kind.strip())
        elif not expect_static and is_static:
            print("note: TEA_EXPECT_STATIC=0 but the binary is static; checks still apply")
        if is_static:
            # `ldd` prints a localised "not a dynamic executable"; the reliable
            # signal is whether any resolved library appears ("name => path").
            ldd = subprocess.run(["ldd", binary], capture_output=True, text=True)
            listed = [l for l in (ldd.stdout + ldd.stderr).splitlines() if "=>" in l]
            if listed:
                failures.append("2: ldd still resolves shared libraries: %s" % listed[:3])

        # ---- 3) it runs without a PEM next to it --------------------------
        # The binary directory is copied alone into an empty directory: no
        # ca-bundle/, no cacert.pem, nothing to fall back to.
        solo = os.path.join(tmp, "solo")
        os.makedirs(solo)
        solo_bin = os.path.join(solo, "tea_agent")
        shutil.copy2(binary, solo_bin)
        port = free_port()
        proc = subprocess.Popen(
            [solo_bin, "--config", config, "--tea-home", os.path.join(tmp, "home"),
             "--port", str(port), "--no-console",
             "--log-file", os.path.join(tmp, "log.txt"),
             "--workspace", os.path.join(tmp, "ws")],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            if not wait_health(port):
                failures.append("3: the binary copied on its own did not start")
            else:
                log = open(os.path.join(tmp, "log.txt"), encoding="utf-8").read()
                if "CA bundle embedded in the binary" not in log:
                    failures.append("3: expected the embedded bundle to be used, log says: %s"
                                    % [l for l in log.splitlines() if "tls:" in l])
                # No ca-bundle directory was created next to it either.
                if os.path.exists(os.path.join(solo, "ca-bundle")):
                    failures.append("3: something wrote a ca-bundle/ next to the binary")
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

        # ---- 4) name resolution does not crash in the static build --------
        # Resolve through the binary's own socket path: the local provider
        # points at a host name, so the request has to call getaddrinfo. A
        # static binary with unconfigured NSS dies here (SIGSEGV).
        if is_static:
            config2 = os.path.join(tmp, "provider_dns.yaml")
            with open(config2, "w", encoding="utf-8") as f:
                f.write(
                    """version: 1
providers:
  remote:
    api_url: https://api.deepseek.com
    api_key: sk-test-not-a-real-key
    default_model: m
    models: {m: {supports_tools: true}}
"""
                )
            port = free_port()
            proc = subprocess.Popen(
                [solo_bin, "--config", config2, "--tea-home", os.path.join(tmp, "home"),
                 "--port", str(port), "--no-console",
                 "--log-file", os.path.join(tmp, "log2.txt"),
                 "--workspace", os.path.join(tmp, "ws")],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                if not wait_health(port):
                    failures.append("4: the binary did not start for the DNS check")
                else:
                    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                    conn.request("GET", "/api/workspaces")
                    ws = json.loads(conn.getresponse().read())["workspaces"][0]["id"]
                    # The request must reach the network layer: a bogus key gets
                    # 401 from the provider, which proves DNS+TLS worked. A
                    # crash or a resolve error looks completely different.
                    body = json.dumps({"message": "ping", "workspace_id": ws})
                    try:
                        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=40)
                        conn.request("POST", "/api/chat", body=body,
                                     headers={"Content-Type": "application/json"})
                        raw = conn.getresponse().read().decode("utf-8", "replace")
                    except Exception as e:  # noqa: BLE001 - a dead server is the failure
                        raw = ""
                        failures.append("4: the chat request died: %s" % e)
                    if "cannot resolve host" in raw:
                        failures.append("4: DNS failed in the static binary: %s" % raw[:300])
                    if "401" not in raw and "Authentication" not in raw and "invalid" not in raw:
                        # Anything but a provider auth error means we never got
                        # an answer from the network.
                        if raw:
                            failures.append("4: expected an auth error from the provider, got: %s"
                                            % raw[:300])
                    # Still alive afterwards: no SIGSEGV inside getaddrinfo.
                    if proc.poll() is not None:
                        failures.append("4: the binary exited during name resolution "
                                        "(signal %s)" % proc.returncode)
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if failures:
        print("FAIL")
        for f in failures:
            print(" -", f)
        return 1
    print("PASS: CA bundle embedded, binary static, and name resolution survives static linking")
    return 0


if __name__ == "__main__":
    sys.exit(main())
