#!/usr/bin/env python3
"""SWE-bench-Live/Windows harness for tea_a2.

The official protocol (microsoft/SWE-bench-Live) runs every instance inside a
Windows container image from DockerHub.  This machine has no container
runtime, so this harness does the same thing **natively**:

    prepare  clone the repo at base_commit into a work tree
    rollout  run tea_a2 on the issue, collect `git diff HEAD` as the patch
    eval     apply test_patch + the agent patch, run rebuild/test/print,
             parse the log with the instance's own log_parser and judge
             FAIL_TO_PASS / PASS_TO_PASS with the official rule

Everything the official script does to a container is done here to a work
tree; the commands that the dataset carries (`/testbed`, `C:\\testbed`) are
rewritten to point at that work tree.

Usage:
    python bench/sweb.py list
    python bench/sweb.py prepare  <instance_id> [...]
    python bench/sweb.py rollout  <instance_id> [...]
    python bench/sweb.py eval     <instance_id> [...]
    python bench/sweb.py run      <instance_id> [...]      # prepare+rollout+eval
    python bench/sweb.py report
"""
import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BENCH = os.path.join(ROOT, ".tea_agent_run", "bench")
WORK = os.path.join(BENCH, "work")
HOME = os.path.join(BENCH, "home")          # tea home of the bench agents
GITCONFIG = os.path.join(BENCH, "gitconfig")
LOGDIR = os.path.join(BENCH, "logs")

DEFAULT_AGENT = os.path.join(ROOT, "build-msvc", "bin", "tea_agent.exe")
DEFAULT_DATA = os.path.join(BENCH, "instances.jsonl")

# ---------------------------------------------------------------- small helpers


def say(msg):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def run(cmd, cwd=None, env=None, timeout=3600, stdin=None):
    """Run a command, return (rc, combined output)."""
    proc = subprocess.run(
        cmd, cwd=cwd, env=env, timeout=timeout,
        input=stdin.encode("utf-8") if isinstance(stdin, str) else stdin,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    out = proc.stdout.decode("utf-8", "replace")
    return proc.returncode, out


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def http(url, payload=None, timeout=60, method=None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url, data=data, method=method or ("POST" if data else "GET"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


# ---------------------------------------------------------------- the instances


def load_instances(path):
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def find_instance(instances, iid):
    for inst in instances:
        if inst["instance_id"] == iid:
            return inst
    raise SystemExit("no such instance: " + iid)


def inst_dir(iid):
    return os.path.join(WORK, iid)


def repo_dir(iid):
    return os.path.join(inst_dir(iid), "repo")


def eval_dir(iid):
    return os.path.join(inst_dir(iid), "eval")


def patch_file(iid):
    return os.path.join(inst_dir(iid), "patch.diff")


# ---------------------------------------------------------------- the environment


def bench_env(iid, extra_path=None):
    """The environment every build/test command runs in.

    Caches and temp dirs are pushed into the work tree: the harness must not
    depend on a writable user profile (and the agent's own sandbox may not
    have one).
    """
    d = inst_dir(iid)
    tmp = os.path.join(d, "tmp")
    for sub in ("tmp", "npm-cache", "gopath", "dotnet", "pip-cache", "poetry"):
        os.makedirs(os.path.join(d, sub), exist_ok=True)
    env = os.environ.copy()
    env["TEMP"] = tmp
    env["TMP"] = tmp
    env["TMPDIR"] = tmp
    env["GIT_CONFIG_GLOBAL"] = GITCONFIG
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["npm_config_cache"] = os.path.join(d, "npm-cache")
    env["NPM_CONFIG_UPDATE_NOTIFIER"] = "false"
    env["GOPATH"] = os.path.join(d, "gopath")
    env["GOCACHE"] = os.path.join(d, "gopath", "cache")
    env["GOMODCACHE"] = os.path.join(d, "gopath", "pkg", "mod")
    env["GOFLAGS"] = "-buildvcs=false"
    env["GOPROXY"] = os.environ.get("BENCH_GOPROXY", "https://goproxy.cn,direct")
    env["DOTNET_CLI_HOME"] = os.path.join(d, "dotnet")
    env["NUGET_PACKAGES"] = os.path.join(d, "dotnet", "packages")
    env["DOTNET_NOLOGO"] = "1"
    env["DOTNET_CLI_TELEMETRY_OPTOUT"] = "1"
    env["DOTNET_SKIP_FIRST_TIME_EXPERIENCE"] = "1"
    env["PIP_CACHE_DIR"] = os.path.join(d, "pip-cache")
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    env["PYTHONPYCACHEPREFIX"] = os.path.join(d, "tmp", "pycache")
    env["POETRY_CACHE_DIR"] = os.path.join(d, "poetry")
    env["PIP_REQUIRE_VIRTUALENV"] = "0"
    # Go and the MinGW toolchain, if they are on this machine.
    extra = []
    for cand in (os.path.join(ROOT, ".toolchain", "go", "bin"),
                 os.path.join(ROOT, ".toolchain", "mingw64", "bin")):
        if os.path.isdir(cand):
            extra.append(cand)
    if extra_path:
        extra.append(extra_path)
    if extra:
        env["PATH"] = os.pathsep.join(extra + [env.get("PATH", "")])
    return env


def write_gitconfig():
    os.makedirs(os.path.dirname(GITCONFIG), exist_ok=True)
    with open(GITCONFIG, "w", encoding="utf-8") as fh:
        fh.write("[user]\n\tname = bench\n\temail = bench@example.com\n")
        fh.write("[core]\n\tautocrlf = false\n\tlongpaths = true\n")
        fh.write("[init]\n\tdefaultBranch = main\n")


# ---------------------------------------------------------------- git helpers


def git(args, cwd, env=None, timeout=1800, stdin=None):
    return run(["git"] + args, cwd=cwd, env=env or bench_env("x"), timeout=timeout, stdin=stdin)


def clone_base(inst, env):
    """clone the repo at base_commit into <work>/<id>/repo (shallow, one commit)"""
    iid = inst["instance_id"]
    repo = repo_dir(iid)
    if os.path.isdir(os.path.join(repo, ".git")):
        return repo
    os.makedirs(repo, exist_ok=True)
    url = "https://github.com/%s.git" % inst["repo"]
    steps = [
        ["init", "-q", "."],
        ["remote", "add", "origin", url],
        ["-c", "core.autocrlf=false", "fetch", "--depth", "1", "origin", inst["base_commit"]],
        ["-c", "core.autocrlf=false", "checkout", "-q", "FETCH_HEAD"],
    ]
    for step in steps:
        rc, out = git(step, cwd=repo, env=env, timeout=3600)
        if rc != 0:
            if step[0] == "fetch":
                # some servers refuse a fetch by sha: fall back to the branch
                rc, out = git(["-c", "core.autocrlf=false", "fetch", "--depth", "50",
                               "origin", inst["base_commit"]], cwd=repo, env=env, timeout=3600)
                if rc != 0:
                    rc, out = git(["-c", "core.autocrlf=false", "fetch", "origin",
                                   inst["base_commit"]], cwd=repo, env=env, timeout=3600)
            if rc != 0:
                raise RuntimeError("git %s failed: %s" % (" ".join(step), out[-800:]))
    return repo


def apply_patch(cwd, patch_text, env):
    """git apply a patch; returns (ok, message).

    A plain `git apply` refuses a patch whose context the test patch has
    already changed. The official harness applies "best effort" too, so
    --3way and --reject are tried before giving up.
    """
    if not patch_text.strip():
        return True, "empty patch"
    for extra, label in (([], "applied"), (["--3way"], "applied (3way)"),
                          (["--reject"], "applied (reject)")):
        rc, out = git(["apply", "--whitespace=nowarn"] + extra + ["-"], cwd=cwd, env=env,
                      timeout=300, stdin=patch_text)
        if rc == 0:
            return True, label
    return False, out[-800:]


TEST_FILE_RE = re.compile(
    r"(^|/)(tests?|testing|__tests__|spec)/|_test\.go$|_test\.py$|test_.*\.py$|"
    r".*\.test\.(ts|tsx|js|jsx)$|.*\.spec\.(ts|tsx|js|jsx)$|"
    r"Tests?\.java$|.*_test\.cc$|.*_test\.cpp$|.*_test\.rs$|"
    r".*Test\.cs$|.*Tests\.cs$", re.IGNORECASE)


def strip_test_files(patch_text):
    """Drop the file sections of a patch that touch test files."""
    sections = re.split(r"(?m)^(?=diff --git )", patch_text)
    kept = []
    dropped = []
    for section in sections:
        if not section.strip():
            continue
        m = re.match(r"diff --git a/(\S+) b/(\S+)", section)
        path = m.group(2) if m else ""
        if path and TEST_FILE_RE.search(path):
            dropped.append(path)
        else:
            kept.append(section)
    return "".join(kept), dropped


def split_patch(patch_text):
    """Split a unified diff into its per-file sections."""
    sections = re.split(r"(?m)^(?=diff --git )", patch_text)
    return [s for s in sections if s.strip()]


def apply_patch_sections(cwd, patch_text, env):
    """Apply every section of a patch on its own.

    Returns (applied, failed) - the names of the files that made it and the
    ones that did not.
    """
    applied, failed = [], []
    for section in split_patch(patch_text):
        m = re.match(r"diff --git a/(\S+) b/(\S+)", section)
        path = m.group(2) if m else section.splitlines()[0][:60]
        ok = False
        for extra in ([], ["--3way"]):
            rc, out = git(["apply", "--whitespace=nowarn"] + extra + ["-"], cwd=cwd, env=env,
                          timeout=300, stdin=section)
            if rc == 0:
                ok = True
                break
        if ok:
            applied.append(path)
        else:
            failed.append(path)
    return applied, failed


def collect_patch(repo, env):
    """`git diff HEAD` plus the files the agent created (untracked)."""
    rc, untracked = git(["ls-files", "--others", "--exclude-standard"], cwd=repo, env=env)
    files = [f for f in untracked.splitlines()
             if f.strip() and not f.startswith(".tea_agent_run/") and f.strip() != ".tea_agent_run"]
    if files:
        git(["add", "-N", "--"] + files, cwd=repo, env=env, timeout=300)
    rc, patch = git(["diff", "HEAD", "--text", "--", ".", ":(exclude).tea_agent_run"],
                    cwd=repo, env=env, timeout=600)
    return patch


# ---------------------------------------------------------------- the rollout

PROMPT = """You are working in a software repository. The root of this workspace is the \
repository root. Resolve the issue below.

<issue>
{problem}
</issue>

How to work:
- Read the code first: find the files the issue is about, understand how they work \
before changing anything.
- Reproduce the problem if you can (run the existing tests), then make the change.
- Keep the change minimal and in the style of the surrounding code. Do not rewrite \
unrelated parts of the repository.
- Do not add or change tests that the issue does not ask for.
- Do not look up the upstream fix, the pull request that closed the issue or any \
later commit of this repository: work the problem out yourself from the code in front \
of you.
- If the change is complete, say what you changed and why, and stop.

You have shell access (PowerShell and cmd) and file access in this workspace. \
Windows is the target platform.

Budget and scope:
- You have about 60 tool calls. Spend them on understanding the bug and \
changing the code, not on polishing.
- Do not install tools, linters or formatters, and do not run them: nobody \
looks at your formatting, the graders only run the tests.
- Do not write new tests. The graders bring their own.
- When the tests of the package you touched pass, stop.
"""


def start_agent(exe, repo, port, log_file, env):
    os.makedirs(os.path.dirname(log_file), exist_ok=True)
    cmd = [exe, "--host", "127.0.0.1", "--port", str(port),
           "--tea-home", HOME, "--config", os.path.join(HOME, "provider.yaml"),
           "--workspace", repo, "--log-file", log_file, "--quiet"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    deadline = time.time() + 90
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("the agent died at start-up:\n%s"
                               % proc.stdout.read().decode("utf-8", "replace")[-2000:])
        try:
            health = http("http://127.0.0.1:%d/api/health" % port, timeout=5)
            if health.get("ok"):
                return proc, health
        except Exception:
            pass
        time.sleep(1)
    proc.kill()
    raise RuntimeError("the agent did not answer /api/health in 90 s")


def stop_agent(proc):
    if proc.poll() is not None:
        return
    try:
        run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], timeout=60)
    except Exception:
        pass
    try:
        proc.wait(timeout=20)
    except Exception:
        proc.kill()


def workspace_id(port, repo):
    ws = http("http://127.0.0.1:%d/api/workspaces" % port, timeout=30)
    norm = os.path.normcase(os.path.abspath(repo))
    for entry in ws.get("workspaces") or []:
        path = os.path.normcase(os.path.abspath(entry.get("path", "")))
        if path == norm:
            return entry.get("id")
    ids = [e.get("id") for e in (ws.get("workspaces") or [])]
    raise RuntimeError("workspace %s not registered (%s)" % (repo, ids))


def rollout(inst, args):
    """Run tea_a2 on the issue, then write the patch to <work>/<id>/patch.diff."""
    iid = inst["instance_id"]
    env = bench_env(iid)
    repo = repo_dir(iid)
    if not os.path.isdir(os.path.join(repo, ".git")):
        raise RuntimeError("not prepared: " + iid)
    port = free_port()
    log_file = os.path.join(LOGDIR, "%s.agent.log" % iid)
    proc, health = start_agent(args.agent, repo, port, log_file, env)
    say("%s: agent up on %d (%s / %s)" % (iid, port,
                                          health.get("provider"), health.get("model")))
    try:
        wsid = workspace_id(port, repo)
        prompt = PROMPT.format(problem=inst["problem_statement"].strip())
        answer = http("http://127.0.0.1:%d/api/chat" % port,
                      {"workspace_id": wsid, "message": prompt, "async": True}, timeout=120)
        turn = answer.get("turn_id")
        say("%s: turn %s started" % (iid, turn))
        deadline = time.time() + args.rollout_timeout
        state = "running"
        info = {}
        while time.time() < deadline:
            time.sleep(10)
            try:
                info = http("http://127.0.0.1:%d/api/turns/%s" % (port, turn), timeout=30)
            except Exception as exc:
                say("%s: status failed: %s" % (iid, exc))
                continue
            state = info.get("state")
            if state != "running":
                break
            say("%s: %s (%d iterations, %.0f s)"
                % (iid, state, info.get("iterations") or 0, info.get("elapsed_ms", 0) / 1000.0))
        if state == "running":
            say("%s: rollout timeout, stopping the turn" % iid)
            try:
                http("http://127.0.0.1:%d/api/turns/%s/stop" % (port, turn),
                     {"confirm": True}, timeout=30)
            except Exception:
                pass
            time.sleep(10)
        try:
            traj = http("http://127.0.0.1:%d/api/turns/%s/trajectory" % (port, turn), timeout=60)
            with open(os.path.join(inst_dir(iid), "trajectory.json"), "w", encoding="utf-8") as fh:
                json.dump(traj, fh, ensure_ascii=False, indent=2)
        except Exception as exc:
            say("%s: no trajectory: %s" % (iid, exc))
        with open(os.path.join(inst_dir(iid), "turn.json"), "w", encoding="utf-8") as fh:
            json.dump(info, fh, ensure_ascii=False, indent=2)
    finally:
        stop_agent(proc)
    patch = collect_patch(repo, env)
    with open(patch_file(iid), "w", encoding="utf-8", newline="") as fh:
        fh.write(patch)
    say("%s: patch is %d bytes, turn state %s" % (iid, len(patch), state))
    return patch


# ---------------------------------------------------------------- the evaluation


def rewrite_paths(cmd, repo, tmp):
    """The dataset's commands talk about /testbed and C:\\testbed."""
    out = cmd
    out = out.replace("C:\\testbed", repo)
    out = out.replace("C:/testbed", repo)
    out = out.replace("/testbed", repo)
    out = out.replace("C:\\gsd-test-tmp", tmp)
    out = out.replace("C:\\t", tmp)
    node = shutil.which("node")
    if node:
        out = out.replace("C:\\node", os.path.dirname(node))
    return out


PS_TEMPLATE = """$ErrorActionPreference = 'Continue'
Set-Location -LiteralPath '{repo}'
$ErrorActionPreference = 'Continue'
try {{ . '{out}\\rebuild.ps1' *>&1 | Out-File -FilePath '{out}\\rebuild.log' -Encoding utf8 }} catch {{ $_ | Out-File -FilePath '{out}\\rebuild.log' -Append -Encoding utf8 }}
foreach ($d in @('reports','artifacts','TestResults','test-reports')) {{ New-Item -ItemType Directory -Force -Path $d | Out-Null }}
try {{ . '{out}\\test.ps1' *>&1 | Out-File -FilePath '{out}\\test.log' -Encoding utf8 }} catch {{ $_ | Out-File -FilePath '{out}\\test.log' -Append -Encoding utf8 }}
try {{ . '{out}\\print.ps1' *>&1 | Out-File -FilePath '{out}\\print.log' -Encoding utf8 }} catch {{ $_ | Out-File -FilePath '{out}\\print.log' -Append -Encoding utf8 }}
Write-Output 'BENCH-DONE'
"""


def evaluate(inst, args):
    """Apply test_patch + the agent patch in a fresh work tree, run the tests."""
    iid = inst["instance_id"]
    env = bench_env(iid)
    repo = repo_dir(iid)
    evald = eval_dir(iid)
    kind = "gold" if getattr(args, "gold", False) else "agent"
    outdir = os.path.join(inst_dir(iid), "out", kind)
    os.makedirs(outdir, exist_ok=True)
    result = {"instance_id": iid, "resolved": False, "stage": "eval"}

    # a second work tree at base_commit: the rollout tree keeps the agent's mess
    if not os.path.exists(os.path.join(evald, ".git")):
        rc, out = git(["worktree", "add", "--detach", evald, inst["base_commit"]],
                      cwd=repo, env=env, timeout=1800)
        if rc != 0:
            result["error"] = "worktree failed: " + out[-400:]
            return result
    patch = ""
    if getattr(args, "gold", False):
        patch = inst.get("patch") or ""
        result["patch_kind"] = "gold"
    elif os.path.exists(patch_file(iid)):
        patch = open(patch_file(iid), encoding="utf-8").read()
        result["patch_kind"] = "agent"

    def reset():
        git(["reset", "--hard", "--quiet", inst["base_commit"]], cwd=evald, env=env, timeout=600)
        git(["clean", "-fdq"], cwd=evald, env=env, timeout=600)

    # the official order: the test patch first, then the agent patch
    reset()
    tp = inst.get("test_patch") or ""
    ok, msg = apply_patch(evald, tp, env)
    if not ok and tp.strip():
        reset()
        applied, failed = apply_patch_sections(evald, tp, env)
        result["test_patch"] = "partial (%d files, %d failed)" % (len(applied), len(failed))
        result["test_patch_failed"] = failed
        ok = bool(applied)
    else:
        result["test_patch"] = "ok" if ok else msg
    if not ok:
        result["error"] = "test_patch did not apply"
        return result
    ok, msg = apply_patch(evald, patch, env)
    result["agent_patch"] = "ok" if ok else msg
    if not ok and patch.strip():
        reset()
        applied, failed = apply_patch_sections(evald, patch, env)
        if applied:
            ok, msg = True, "partial"
            result["agent_patch"] = "partial (%d files, %d failed)" % (len(applied), len(failed))
            result["agent_patch_failed"] = failed
    if not ok:
        # the other order (plain SWE-bench): the agent patch first
        reset()
        ok2, msg2 = apply_patch(evald, patch, env)
        if ok2:
            ok3, msg3 = apply_patch(evald, inst.get("test_patch") or "", env)
            if ok3:
                ok, msg = True, "applied (agent patch first)"
                result["agent_patch"] = ok
                result["test_patch"] = "ok (agent patch first)"
            else:
                ok, msg = False, "test patch does not apply after the agent patch: " + msg3[-300:]
                result["agent_patch"] = "ok (agent patch first)"
                result["test_patch"] = msg
        else:
            reset()
            ok, msg = False, msg
    if not ok and patch:
        # last resort: the agent's source change without its test changes
        stripped, dropped = strip_test_files(patch)
        if dropped and stripped.strip():
            reset()
            ok2, msg2 = apply_patch(evald, stripped, env)
            if ok2:
                ok3, msg3 = apply_patch(evald, inst.get("test_patch") or "", env)
                if ok3:
                    ok, msg = True, "applied without the test files"
                    result["agent_patch"] = "ok (test files dropped: %s)" % ", ".join(dropped)
                    result["test_patch"] = "ok"
    if not ok:
        result["error"] = "agent patch did not apply"
        return result

    # the container images carry these directories; a fresh work tree does not
    for sub in ("reports", "artifacts", "TestResults", "test-reports"):
        try:
            os.makedirs(os.path.join(evald, sub), exist_ok=True)
        except Exception:
            pass
    tmp = os.path.join(inst_dir(iid), "tmp")
    groups = {
        "rebuild": " ; ".join(inst.get("rebuild_cmds") or []),
        "test": " ; ".join(inst.get("test_cmds") or []),
        "print": " ; ".join(inst.get("print_cmds") or []),
    }
    for name, cmd in groups.items():
        if not cmd.strip():
            with open(os.path.join(outdir, name + ".ps1"), "w", encoding="utf-8-sig") as fh:
                fh.write("# nothing to run\n")
            continue
        with open(os.path.join(outdir, name + ".ps1"), "w", encoding="utf-8-sig") as fh:
            fh.write(rewrite_paths(cmd, evald, tmp) + "\n")
    script = PS_TEMPLATE.format(repo=evald, out=outdir)
    script_path = os.path.join(outdir, "run.ps1")
    with open(script_path, "w", encoding="utf-8-sig") as fh:
        fh.write(script)
    rc, log = run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script_path],
                  cwd=evald, env=env, timeout=args.eval_timeout)
    with open(os.path.join(outdir, "runner.log"), "w", encoding="utf-8") as fh:
        fh.write(log)
    result["runner_rc"] = rc

    print_log = ""
    p = os.path.join(outdir, "print.log")
    if os.path.exists(p):
        print_log = open(p, encoding="utf-8-sig", errors="replace").read()
    with open(os.path.join(outdir, "print.txt"), "w", encoding="utf-8") as fh:
        fh.write(print_log)
    result["print_bytes"] = len(print_log)

    if not print_log.strip():
        result["error"] = "no test output (see out/runner.log)"
        return result

    try:
        status = parse_log(inst, print_log)
    except Exception as exc:
        result["error"] = "log parser failed: %r" % (exc,)
        return result
    result["status_counts"] = {
        "pass": sum(1 for v in status.values() if "pass" in str(v).lower()),
        "fail": sum(1 for v in status.values() if "fail" in str(v).lower()),
        "skip": sum(1 for v in status.values() if "skip" in str(v).lower()),
        "total": len(status),
    }
    suc = set(t for t, v in status.items() if "pass" in str(v).lower())
    fail = set(t for t, v in status.items() if "fail" in str(v).lower())
    f2p = set(inst.get("FAIL_TO_PASS") or [])
    p2p = set(inst.get("PASS_TO_PASS") or [])
    f2p_ok = f2p.issubset(suc) or (len(suc & f2p) == len(f2p) and len(f2p) > 0)
    result["f2p_success"] = sorted(suc & f2p)
    result["f2p_failure"] = sorted(fail & f2p)
    result["f2p_missing"] = sorted(f2p - suc - fail)
    result["p2p_failure"] = sorted(fail & p2p)
    result["resolved"] = bool(not result["f2p_failure"] and not result["p2p_failure"] and f2p_ok)

    # the lenient judgement: the dataset sometimes carries a truncated or
    # misspelled test name, so the exact match above can never succeed
    def lenient_ok(name):
        for key, value in status.items():
            if key.startswith(name):
                return "pass" in str(value).lower()
        return None

    lenient_f2p = {}
    for name in f2p:
        lenient_f2p[name] = lenient_ok(name)
    lenient_suc = set(n for n, v in lenient_f2p.items() if v is True)
    lenient_fail = set(n for n, v in lenient_f2p.items() if v is False)
    lenient_missing = set(n for n, v in lenient_f2p.items() if v is None)
    lenient_p2p_fail = set(n for n in p2p if lenient_ok(n) is False)
    # the repaired judgement: a corrupted dataset name is matched against the
    # parsed test names by similarity
    import difflib

    def repaired(name):
        best, ratio = None, 0.0
        for key, value in status.items():
            r = difflib.SequenceMatcher(None, name, key).ratio()
            if r > ratio:
                best, ratio = key, r
        if best is not None and ratio >= 0.85:
            return "pass" if "pass" in str(status[best]).lower() else "fail"
        return None

    rep = {}
    for name in f2p:
        rep[name] = repaired(name)
    rep_suc = set(n for n, v in rep.items() if v is True)
    rep_fail = set(n for n, v in rep.items() if v is False)
    rep_missing = set(n for n, v in rep.items() if v is None)
    rep_p2p_fail = set(n for n in p2p if repaired(n) is False)
    result["repaired"] = {
        "f2p_success": sorted(rep_suc),
        "f2p_failure": sorted(rep_fail),
        "f2p_missing": sorted(rep_missing),
        "p2p_failure": sorted(rep_p2p_fail),
        "resolved": bool(not rep_fail and not rep_missing and not rep_p2p_fail and f2p),
    }

    result["lenient"] = {
        "f2p_success": sorted(lenient_suc),
        "f2p_failure": sorted(lenient_fail),
        "f2p_missing": sorted(lenient_missing),
        "p2p_failure": sorted(lenient_p2p_fail),
        "resolved": bool(not lenient_fail and not lenient_missing
                         and not lenient_p2p_fail and f2p),
    }
    return result


def parse_log(inst, log_text):
    src = inst.get("log_parser") or ""
    if not src.strip():
        raise RuntimeError("no log parser")
    ns = {}
    exec(compile(src, "<log_parser>", "exec"), ns)
    fn = ns.get("parser")
    if fn is None:
        for value in ns.values():
            if callable(value) and getattr(value, "__name__", "") not in ("print",):
                fn = value
                break
    if fn is None:
        raise RuntimeError("the parser defines no function")
    out = fn(log_text)
    return out if isinstance(out, dict) else {}


# ---------------------------------------------------------------- driver


def cmd_run_one(inst, args):
    iid = inst["instance_id"]
    t0 = time.time()
    report = {"instance_id": iid}
    try:
        env = bench_env(iid)
        clone_base(inst, env)
        report["prepared"] = True
        if os.path.exists(patch_file(iid)) and not getattr(args, "force", False):
            report["patch_bytes"] = os.path.getsize(patch_file(iid))
            report["rollout"] = "reused"
            say("%s: reusing the patch of the previous rollout" % iid)
        else:
            rollout(inst, args)
            report["patch_bytes"] = os.path.getsize(patch_file(iid))
            report["rollout"] = "done"
    except Exception as exc:
        report["error"] = "%s: %s" % (type(exc).__name__, exc)
        say("%s: FAILED %s" % (iid, report["error"]))
    try:
        res = evaluate(inst, args)
        report.update(res)
    except Exception as exc:
        report["error"] = report.get("error", "") + " eval: %s: %s" % (type(exc).__name__, exc)
    report["seconds"] = int(time.time() - t0)
    with open(os.path.join(inst_dir(iid), "report.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    say("%s: resolved=%s%s (%.0f s)"
        % (iid, report.get("resolved"), "  " + report.get("error", "") if report.get("error") else "",
           report["seconds"]))
    return report


def collect_reports():
    out = []
    if not os.path.isdir(WORK):
        return out
    for iid in sorted(os.listdir(WORK)):
        p = os.path.join(WORK, iid, "report.json")
        if os.path.exists(p):
            try:
                out.append(json.load(open(p, encoding="utf-8")))
            except Exception:
                pass
    return out


def print_report():
    reports = collect_reports()
    if not reports:
        say("no reports yet")
        return
    print("%-42s %-9s %6s %6s %s" % ("instance", "resolved", "F2P", "P2Pfail", "note"))
    resolved = 0
    for r in sorted(reports, key=lambda x: x["instance_id"]):
        note = r.get("error", "") or ""
        if not note and not r.get("resolved"):
            note = "f2p missing: %s" % ",".join(r.get("f2p_missing") or [])[:60]
        print("%-42s %-9s %6s %6s %s"
              % (r["instance_id"], r.get("resolved"),
                 len(r.get("f2p_success") or []), len(r.get("p2p_failure") or []),
                 note[:90]))
        resolved += 1 if r.get("resolved") else 0
    print("\nresolved %d / %d" % (resolved, len(reports)))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["list", "prepare", "rollout", "eval", "run", "report"])
    ap.add_argument("instances", nargs="*")
    ap.add_argument("--data", default=DEFAULT_DATA)
    ap.add_argument("--agent", default=DEFAULT_AGENT)
    ap.add_argument("--rollout-timeout", type=int, default=45 * 60)
    ap.add_argument("--eval-timeout", type=int, default=60 * 60)
    ap.add_argument("--jobs", type=int, default=1)
    ap.add_argument("--force", action="store_true",
                    help="roll out again even if a patch is there")
    ap.add_argument("--gold", action="store_true",
                    help="evaluate the dataset's own patch instead of the agent's")
    args = ap.parse_args()

    os.makedirs(WORK, exist_ok=True)
    os.makedirs(LOGDIR, exist_ok=True)
    write_gitconfig()
    instances = load_instances(args.data)

    if args.command == "list":
        for inst in instances:
            print("%-42s %-26s %-11s F2P=%-5d P2P=%-6d %s"
                  % (inst["instance_id"], inst["repo"], inst["language"],
                     len(inst.get("FAIL_TO_PASS") or []),
                     len(inst.get("PASS_TO_PASS") or []),
                     inst["base_commit"][:10]))
        return

    if args.command == "report":
        print_report()
        gold_dir = os.path.join(BENCH, "rejudge.json")
        if os.path.exists(gold_dir):
            os.system('python "%s"'
                      % os.path.join(os.path.dirname(os.path.abspath(__file__)), "rejudge.py"))
        return

    picked = [find_instance(instances, iid) for iid in args.instances]
    if not picked:
        raise SystemExit("no instances given")

    if args.command == "prepare":
        for inst in picked:
            env = bench_env(inst["instance_id"])
            clone_base(inst, env)
            say("%s: prepared" % inst["instance_id"])
        return

    if args.command == "rollout":
        def worker(inst):
            rollout(inst, args)

        if args.jobs > 1:
            threads = []
            for inst in picked:
                t = threading.Thread(target=worker, args=(inst,))
                t.start()
                threads.append(t)
                time.sleep(2)
            for t in threads:
                t.join()
        else:
            for inst in picked:
                worker(inst)
        return

    if args.command == "eval":
        def worker(inst):
            res = evaluate(inst, args)
            name = "gold.json" if getattr(args, "gold", False) else "report.json"
            with open(os.path.join(inst_dir(inst["instance_id"]), name),
                      "w", encoding="utf-8") as fh:
                json.dump(res, fh, ensure_ascii=False, indent=2)
            say("%s: resolved=%s lenient=%s %s"
                % (inst["instance_id"], res.get("resolved"),
                   (res.get("lenient") or {}).get("resolved"), res.get("error", "")))

        if args.jobs > 1:
            threads = []
            for inst in picked:
                t = threading.Thread(target=worker, args=(inst,))
                t.start()
                threads.append(t)
                time.sleep(2)
            for t in threads:
                t.join()
        else:
            for inst in picked:
                worker(inst)
        return

    # run: prepare + rollout + eval, optionally in parallel
    def worker(inst):
        cmd_run_one(inst, args)

    if args.jobs > 1:
        threads = []
        for inst in picked:
            t = threading.Thread(target=worker, args=(inst,))
            t.start()
            threads.append(t)
            time.sleep(2)
        for t in threads:
            t.join()
    else:
        for inst in picked:
            worker(inst)
    print_report()


if __name__ == "__main__":
    main()
