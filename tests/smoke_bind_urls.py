#!/usr/bin/env python3
"""What the banner says a browser should open.

`--host 0.0.0.0` exists so another device on the LAN can reach the UI, so the
line printed at startup has to be this machine's active address - not the
loopback, which is unreachable from the phone the user is holding. A specific
bind still prints exactly what was asked for, and the loopback fallback has to
stay available (and labelled) when the machine has no usable address at all.

    python3 tests/smoke_bind_urls.py [path-to-tea_agent]

Local only: no provider is contacted (the runs below are in setup mode, which is
also what exercises the "open http://... to upload provider.yaml" hint).
"""
import http.client
import os
import re
import shutil
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
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def local_ipv4():
    """The machine's own IPv4 addresses, default route first (same idea as the
    agent's probe): a UDP connect picks a source address without sending."""
    found = []
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 53))
        found.append(s.getsockname()[0])
    except OSError:
        pass
    finally:
        s.close()
    try:
        for name in socket.gethostbyname_ex(socket.gethostname())[2]:
            found.append(name)
    except OSError:
        pass
    out = []
    for ip in found:
        if ip.startswith("127.") or ip == "0.0.0.0" or ip in out:
            continue
        out.append(ip)
    return out


def http_get(host, port, path, timeout=3):
    c = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        c.request("GET", path)
        r = c.getresponse()
        return r.status, r.read().decode("utf-8", "replace")
    finally:
        c.close()


def wait_health(host, port, timeout=30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            status, body = http_get(host, port, "/api/health", timeout=2)
            if status == 200 and '"ok":true' in body.replace(" ", "").lower():
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


def run(tmp, host, probe_host=None):
    """Start the agent in setup mode (no provider.yaml) and return
    (console text, port, probe result).

    `probe_host`, when given, is fetched *while the server is still running*:
    the point of the banner is that a different machine can reach it, so the
    advertised address has to answer on this one."""
    tag = str(abs(hash(host)) % 100000)
    home = os.path.join(tmp, "home_" + tag)
    ws = os.path.join(tmp, "ws_" + tag)
    os.makedirs(home, exist_ok=True)
    os.makedirs(ws, exist_ok=True)
    port = free_port()
    proc = subprocess.Popen(
        [EXE, "--host", host, "--port", str(port), "--tea-home", home, "--workspace", ws,
         "--log-level", "error", "--console-level", "error"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True, encoding="utf-8",
        errors="replace")
    probe = None
    # A specific bind only answers on that address; a wildcard bind answers on
    # the loopback (and on every interface, which the probe below checks).
    ready_host = "127.0.0.1" if host in ("0.0.0.0", "::", "[::]", "*") else host
    try:
        if not wait_health(ready_host, port):
            raise RuntimeError("agent did not become healthy on %s:%d" % (host, port))
        if probe_host:
            try:
                probe = http_get(probe_host, port, "/")
            except Exception as exc:  # noqa: BLE001 - the failure is the assertion
                probe = ("error", str(exc))
        time.sleep(0.3)
    finally:
        stop(proc)
    out = proc.stdout.read() if proc.stdout else ""
    return out, port, probe


def banner_urls(out):
    """Every URL printed on the Web UI lines (the line itself, not its label)."""
    urls = []
    for line in out.splitlines():
        if line.startswith("  Web UI      : ") or line.startswith("                http://"):
            m = re.search(r"(http://\S+)", line)
            if m:
                urls.append(m.group(1))
    return urls


def main():
    if not os.path.isfile(EXE):
        print("missing binary: " + EXE)
        return 2
    # A run directory that is removed even when a check throws: an interrupted
    # test must not leave tea_bind_* folders in the repo.
    tmp = tempfile.mkdtemp(prefix="tea_bind_")
    try:
        return run_checks(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def run_checks(tmp):
    addresses = local_ipv4()

    # 1) Wildcard bind: the LAN address is what gets advertised, loopback last.
    probe_host = addresses[0] if addresses else None
    out, port, probe = run(tmp, "0.0.0.0", probe_host=probe_host)
    urls = banner_urls(out)
    loopback_url = "http://127.0.0.1:%d" % port
    check("the wildcard banner prints a Web UI URL at all", len(urls) >= 1, out[:600])
    if addresses:
        first = urls[0] if urls else ""
        host = first[len("http://"):].rsplit(":", 1)[0] if first.startswith("http://") else ""
        check("the wildcard banner advertises an active LAN address, not the loopback",
              host in addresses, "banner=%s addresses=%s" % (first, addresses))
        check("the advertised address is the one the default route would use",
              host == addresses[0], "banner=%s expected=%s" % (host, addresses[0]))
        check("loopback is still listed for this machine", loopback_url in urls, urls)
        check("the loopback line says it is local to this machine",
              any("(this machine only)" in l for l in out.splitlines() if loopback_url in l),
              out[:800])
        check("the advertised address really serves the UI while the server runs",
              isinstance(probe, tuple) and probe[0] == 200 and "app.js" in probe[1],
              probe if not isinstance(probe, tuple) or probe[0] != 200 else "no app.js")
        check("the setup hint names the same LAN address",
              ("hint: open " + first + "/") in out, out[:600])
    else:
        check("with no usable address the banner falls back to loopback",
              urls[:1] == [loopback_url], urls)

    # 2) Loopback bind: exactly one URL, and no misleading label.
    loop_out, loop_port, _ = run(tmp, "127.0.0.1")
    loop_urls = banner_urls(loop_out)
    expected_loop = "http://127.0.0.1:%d" % loop_port
    check("binding the loopback prints exactly that URL",
          loop_urls == [expected_loop], loop_urls)
    check("a single URL is not labelled as local-only",
          "(this machine only)" not in loop_out, loop_out[:600])

    # 3) A specific non-loopback address is printed verbatim (an explicit bind is
    #    a deliberate choice; expanding it would be wrong).
    if addresses:
        one = addresses[0]
        one_out, one_port, one_probe = run(tmp, one, probe_host=one)
        check("binding one address prints only that address",
              banner_urls(one_out) == ["http://%s:%d" % (one, one_port)],
              banner_urls(one_out))
        check("that address answers a request bound to it",
              isinstance(one_probe, tuple) and one_probe[0] == 200, one_probe)

    # 4) --help still documents the flag this behaviour hangs off.
    help_out = subprocess.run([EXE, "--help"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True).stdout
    check("--help still documents --host", "--host <addr>" in help_out, help_out[:300])

    failed = [n for n, ok in results if not ok]
    print("\n%d/%d checks passed" % (len(results) - len(failed), len(results)))
    print("ALL PASS" if not failed else "%d FAILED" % len(failed))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
