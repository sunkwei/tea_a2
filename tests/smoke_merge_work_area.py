#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Smoke test for `tools/merge_work_area.py`.

The tool merges one workspace's runtime store (`.tea_agent_run/`) into
another's. A plain copy is not enough: the target has its own sessions, its
own memory, and session ids that may already be taken.

What this pins:

  * the transcripts, the sidebar index, the memory notes, the per-turn
    trajectories, `agent.md`, the images the conversations point at, the audit
    chains, the log and the scripts all end up in the target;
  * the target's own sessions survive the merge (they do not disappear from
    the sidebar);
  * a session id that both sides use is renamed, and the lineage of a forked
    session follows the new id (a branch keeps pointing at its parent);
  * a note that is already there is not duplicated;
  * the source store is never written to;
  * a second run changes nothing (the merge is idempotent);
  * `--dry-run` prints the plan and touches no file;
  * a broken `index.json` in the target is rebuilt from the session files, so
    the target's own conversations do not vanish from the sidebar.

Usage: python tests/smoke_merge_work_area.py
"""

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(REPO_ROOT, "tools", "merge_work_area.py")

failures = []
checks = []


def check(name, ok, detail=""):
    checks.append(name)
    print(("PASS " if ok else "FAIL ") + name + (("  " + str(detail)) if not ok else ""))
    if not ok:
        failures.append(name)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def write_text(path, text):
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)
    with io.open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)


def write_json(path, obj):
    write_text(path, json.dumps(obj, indent=2, ensure_ascii=False, sort_keys=True) + "\n")


def read_json(path):
    with io.open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def read_text(path):
    with io.open(path, "r", encoding="utf-8", newline="") as handle:
        return handle.read()


def tree_digest(root):
    """A digest of a whole directory: every file's path and its content."""
    digest = hashlib.sha256()
    for base, dirs, files in os.walk(root):
        dirs.sort()
        for name in sorted(files):
            path = os.path.join(base, name)
            rel = os.path.relpath(path, root).replace("\\", "/")
            digest.update(rel.encode("utf-8"))
            digest.update(b"\0")
            with open(path, "rb") as handle:
                digest.update(handle.read())
            digest.update(b"\0")
    return digest.hexdigest()


def run_tool(args):
    proc = subprocess.Popen([sys.executable, TOOL] + args,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            universal_newlines=True)
    out, err = proc.communicate()
    return proc.returncode, out, err


def session(sid, title, created, updated, messages, forked_from="", fork_turn=0,
            todos=None):
    data = {
        "id": sid,
        "title": title,
        "created_at": created,
        "updated_at": updated,
        "messages": messages,
    }
    if forked_from:
        data["forked_from"] = forked_from
    if fork_turn:
        data["fork_turn"] = fork_turn
    if todos is not None:
        data["todos"] = todos
    return data


def message(role, content, images=None):
    data = {"role": role, "content": content, "created_at": "2026-01-01T00:00:00Z"}
    if images:
        data["images"] = images
    return data


def turn(turn_id, session_id, started, ok=True):
    return {
        "turn_id": turn_id,
        "session_id": session_id,
        "workspace_id": "ws_source",
        "started_at": started,
        "finished_at": started,
        "ok": ok,
        "iterations": 1,
        "steps": [],
        "files_changed": [],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15,
                  "estimated": False, "cached_tokens": 0},
        "version": 1,
    }


# ---------------------------------------------------------------------------
# the fixtures
# ---------------------------------------------------------------------------
def build_source(root):
    """A store with two conversations (one of them a fork), memory, a
    trajectory per conversation, an experience log, an image, an audit chain,
    a log and a script."""
    run = os.path.join(root, "source", ".tea_agent_run")
    sessions = os.path.join(run, "sessions")

    root_session = session(
        "s_aaaa1111", "源工作区的对话", "2026-01-01T00:00:00Z", "2026-01-01T00:10:00Z",
        [message("user", "你好"),
         message("assistant", "你好，有什么可以帮你？",
                 images=[{"mime": "image/png", "path": ".tea_agent_run/artifacts/img_a.png"}])],
        todos=[{"id": "t1", "text": "第一步", "status": "done"},
               {"id": "t2", "text": "第二步", "status": "pending"}])
    fork_session = session(
        "s_bbbb2222", "分叉 1 · 源工作区的对话", "2026-01-02T00:00:00Z",
        "2026-01-02T00:05:00Z",
        [message("user", "继续"), message("assistant", "好的")],
        forked_from="s_aaaa1111", fork_turn=1)
    write_json(os.path.join(sessions, "s_aaaa1111.json"), root_session)
    write_json(os.path.join(sessions, "s_bbbb2222.json"), fork_session)
    write_json(os.path.join(sessions, "index.json"), [
        {"id": "s_aaaa1111", "title": "源工作区的对话", "created_at": "2026-01-01T00:00:00Z",
         "updated_at": "2026-01-01T00:10:00Z", "message_count": 2, "todo_total": 2,
         "todo_done": 1},
        {"id": "s_bbbb2222", "title": "分叉 1 · 源工作区的对话",
         "created_at": "2026-01-02T00:00:00Z", "updated_at": "2026-01-02T00:05:00Z",
         "message_count": 2, "forked_from": "s_aaaa1111", "fork_turn": 1},
    ])

    write_json(os.path.join(run, "memory", "memory.json"), {
        "notes": [
            {"id": "n_src00000001", "text": "源工作区独有的一条", "created_at": "2026-01-01T01:00:00Z"},
            {"id": "n_shared00001", "text": "两边都有的一条", "created_at": "2026-01-01T02:00:00Z"},
        ],
        "updated_at": "2026-01-01T02:00:00Z",
    })

    traj = os.path.join(run, "trajectories")
    write_text(os.path.join(traj, "s_aaaa1111.jsonl"),
               json.dumps(turn("t_one", "s_aaaa1111", "2026-01-01T00:01:00Z"),
                          ensure_ascii=False, sort_keys=True) + "\n" +
               json.dumps(turn("t_two", "s_aaaa1111", "2026-01-01T00:05:00Z"),
                          ensure_ascii=False, sort_keys=True) + "\n")
    write_text(os.path.join(traj, "s_bbbb2222.jsonl"),
               json.dumps(turn("t_three", "s_bbbb2222", "2026-01-02T00:01:00Z"),
                          ensure_ascii=False, sort_keys=True) + "\n")

    write_text(os.path.join(run, "agent.md"),
               "# agent.md - workspace experience\n\n"
               "## Pitfalls\n\n"
               "- 2026-01-01 源工作区的坑：合并时别把索引写坏\n"
               "- 2026-01-01 两边都有的坑\n"
               "\n## Commands & scripts worth reusing\n\n"
               "- 2026-01-01 源工作区的脚本：.tea_agent_run/scripts/hello.ps1\n")

    write_text(os.path.join(run, "artifacts", "img_a.png"), "PNG-BYTES-FROM-SOURCE")
    write_text(os.path.join(run, "audit", "audit-20260101.jsonl"),
               json.dumps({"event": "tool/call", "tool": "toolkit_exec", "ts": "2026-01-01T00:00:00Z",
                           "h": "abc", "prev": "0" * 64}, ensure_ascii=False,
                          sort_keys=True) + "\n")
    write_text(os.path.join(run, "logs", "agent.log"),
               "2026-01-01 00:00:00 [INFO] 源工作区启动\n"
               "2026-01-01 00:00:01 [INFO] 源工作区跑了一轮\n")
    write_text(os.path.join(run, "scripts", "hello.ps1"), "Write-Host 'hello from the source'\n")
    return run


def build_target(root):
    """A store with one conversation of its own, one note of its own and a
    note that both stores have."""
    run = os.path.join(root, "target", ".tea_agent_run")
    sessions = os.path.join(run, "sessions")
    own = session("s_cccc3333", "目标工作区的对话", "2026-02-01T00:00:00Z",
                  "2026-02-01T00:10:00Z",
                  [message("user", "目标的问题"), message("assistant", "目标的回答")])
    write_json(os.path.join(sessions, "s_cccc3333.json"), own)
    write_json(os.path.join(sessions, "index.json"), [
        {"id": "s_cccc3333", "title": "目标工作区的对话", "created_at": "2026-02-01T00:00:00Z",
         "updated_at": "2026-02-01T00:10:00Z", "message_count": 2},
    ])
    write_json(os.path.join(run, "memory", "memory.json"), {
        "notes": [
            {"id": "n_shared00001", "text": "两边都有的一条", "created_at": "2026-01-01T02:00:00Z"},
            {"id": "n_own0000001", "text": "目标工作区独有的一条", "created_at": "2026-02-01T01:00:00Z"},
        ],
        "updated_at": "2026-02-01T01:00:00Z",
    })
    write_text(os.path.join(run, "agent.md"),
               "# agent.md - workspace experience\n\n"
               "## Pitfalls\n\n"
               "- 2026-02-01 目标工作区的坑\n"
               "- 2026-01-01 两边都有的坑\n")
    write_text(os.path.join(run, "logs", "agent.log"), "2026-02-01 00:00:00 [INFO] 目标工作区启动\n")
    write_text(os.path.join(run, "meta.json"), json.dumps(
        {"schema": 1, "id": "ws_target", "name": "target", "path": "target",
         "created_at": "2026-02-01T00:00:00Z"}, indent=2, sort_keys=True) + "\n")
    return run


# ---------------------------------------------------------------------------
# the checks
# ---------------------------------------------------------------------------
def check_merge(root):
    src = build_source(root)
    dst = build_target(root)
    src_before = tree_digest(src)

    code, out, err = run_tool([src, dst, "--json"])
    check("the merge succeeds", code == 0, err or out)
    report = {}
    try:
        report = json.loads(out)
    except ValueError:
        pass
    check("the report is JSON", bool(report), out[:200])

    # -- the source is untouched ------------------------------------------
    check("the source store is not written to", tree_digest(src) == src_before)

    # -- sessions ---------------------------------------------------------
    sessions = os.path.join(dst, "sessions")
    files = sorted(n for n in os.listdir(sessions) if n.endswith(".json")
                   and n != "index.json")
    check("every session of both sides is in the target",
          files == ["s_aaaa1111.json", "s_bbbb2222.json", "s_cccc3333.json"], files)
    index = read_json(os.path.join(sessions, "index.json"))
    ids = sorted(e.get("id") for e in index)
    check("the sidebar index holds every session",
          ids == ["s_aaaa1111", "s_bbbb2222", "s_cccc3333"], ids)
    fork = read_json(os.path.join(sessions, "s_bbbb2222.json"))
    check("the forked session still points at its parent",
          fork.get("forked_from") == "s_aaaa1111", fork.get("forked_from"))
    check("the target's own session is still there",
          os.path.isfile(os.path.join(sessions, "s_cccc3333.json")))
    own_index = [e for e in index if e.get("id") == "s_cccc3333"]
    check("the target's own session is still in the index", len(own_index) == 1, own_index)
    merged_index = [e for e in index if e.get("id") == "s_aaaa1111"]
    check("the merged session is in the index with its title",
          len(merged_index) == 1 and merged_index[0].get("title") == "源工作区的对话",
          merged_index)
    check("the merged session keeps its task list",
          len(read_json(os.path.join(sessions, "s_aaaa1111.json")).get("todos", [])) == 2)
    check("the provenance of the merge is written down",
          os.path.isfile(os.path.join(dst, "merges.jsonl")))

    # -- memory -----------------------------------------------------------
    memory = read_json(os.path.join(dst, "memory", "memory.json"))
    notes = memory.get("notes", [])
    texts = [n.get("text") for n in notes]
    check("the memory of both sides is in the target",
          "目标工作区独有的一条" in texts and "源工作区独有的一条" in texts, texts)
    check("a note that is already there is not duplicated",
          texts.count("两边都有的一条") == 1, texts)

    # -- trajectories -----------------------------------------------------
    traj = os.path.join(dst, "trajectories")
    check("the trajectory of every session is copied",
          os.path.isfile(os.path.join(traj, "s_aaaa1111.jsonl")) and
          os.path.isfile(os.path.join(traj, "s_bbbb2222.jsonl")))
    lines = [l for l in read_text(os.path.join(traj, "s_aaaa1111.jsonl")).split("\n") if l.strip()]
    check("every turn of the trajectory is there", len(lines) == 2, len(lines))
    record = json.loads(lines[0])
    check("the trajectory points at the target's workspace",
          record.get("workspace_id") == "ws_target", record.get("workspace_id"))

    # -- agent.md ---------------------------------------------------------
    doc = read_text(os.path.join(dst, "agent.md"))
    check("the experience of both sides is in agent.md",
          "源工作区的坑" in doc and "目标工作区的坑" in doc, doc)
    check("a line that is already there is not duplicated",
          doc.count("两边都有的坑") == 1, doc)
    check("the sections of the source are kept",
          "## Commands & scripts worth reusing" in doc, doc)

    # -- artifacts --------------------------------------------------------
    check("the image of the merged conversation is copied",
          os.path.isfile(os.path.join(dst, "artifacts", "img_a.png")))
    check("the image is copied byte for byte",
          read_text(os.path.join(dst, "artifacts", "img_a.png")) == "PNG-BYTES-FROM-SOURCE")

    # -- audit ------------------------------------------------------------
    audit_dir = os.path.join(dst, "audit")
    audits = sorted(os.listdir(audit_dir)) if os.path.isdir(audit_dir) else []
    check("the audit chain is copied under a new name",
          len(audits) == 1 and audits[0].endswith(".jsonl") and
          "from" in audits[0], audits)
    if audits:
        chain = [json.loads(l) for l in
                 read_text(os.path.join(audit_dir, audits[0])).split("\n") if l.strip()]
        check("the copied chain is intact", len(chain) == 1 and
              chain[0].get("h") == "abc", chain)

    # -- log & scripts ----------------------------------------------------
    log = read_text(os.path.join(dst, "logs", "agent.log"))
    check("the log of the source is appended", "源工作区跑了一轮" in log, log)
    check("the log of the target is kept", "目标工作区启动" in log, log)
    check("the script of the source is copied",
          os.path.isfile(os.path.join(dst, "scripts", "hello.ps1")))
    return src, dst


def check_idempotent(src, dst):
    """A second run must not duplicate anything."""
    before = tree_digest(dst)
    code, out, err = run_tool([src, dst, "--json"])
    check("the second run succeeds", code == 0, err or out)
    sessions = os.path.join(dst, "sessions")
    files = sorted(n for n in os.listdir(sessions) if n.endswith(".json")
                   and n != "index.json")
    check("the second run copies no session",
          files == ["s_aaaa1111.json", "s_bbbb2222.json", "s_cccc3333.json"], files)
    notes = read_json(os.path.join(dst, "memory", "memory.json")).get("notes", [])
    check("the second run copies no note", len(notes) == 3, notes)
    log = read_text(os.path.join(dst, "logs", "agent.log"))
    check("the second run appends no log line",
          log.count("源工作区跑了一轮") == 1, log)
    doc = read_text(os.path.join(dst, "agent.md"))
    check("the second run adds no line to agent.md",
          doc.count("源工作区的坑") == 1, doc)
    report = {}
    try:
        report = json.loads(out)
    except ValueError:
        pass
    skipped = sum(1 for a in report.get("actions", []) if a.get("action") == "skipped")
    check("the second run reports what it skipped", skipped > 0, report.get("counts"))


def check_conflict(root):
    """Both sides use the same session id: the source's one is renamed and the
    lineage follows."""
    src = build_source(root)
    dst = build_target(root)
    # The target already knows a session with that id - and a different one.
    clash = session("s_aaaa1111", "目标工作区已经有的对话", "2026-02-02T00:00:00Z",
                    "2026-02-02T00:10:00Z", [message("user", "同名但不同的会话")])
    write_json(os.path.join(dst, "sessions", "s_aaaa1111.json"), clash)
    write_json(os.path.join(dst, "sessions", "index.json"), [
        {"id": "s_aaaa1111", "title": "目标工作区已经有的对话",
         "created_at": "2026-02-02T00:00:00Z", "updated_at": "2026-02-02T00:10:00Z",
         "message_count": 1},
        {"id": "s_cccc3333", "title": "目标工作区的对话", "created_at": "2026-02-01T00:00:00Z",
         "updated_at": "2026-02-01T00:10:00Z", "message_count": 2},
    ])

    code, out, err = run_tool([src, dst, "--json"])
    check("the merge with a clashing id succeeds", code == 0, err or out)
    report = {}
    try:
        report = json.loads(out)
    except ValueError:
        pass
    mapping = report.get("session_map", {})
    new_id = mapping.get("s_aaaa1111", "")
    check("the clashing session got a new id", new_id and new_id != "s_aaaa1111", mapping)
    check("the target's own session was not touched",
          read_json(os.path.join(dst, "sessions", "s_aaaa1111.json")).get("title")
          == "目标工作区已经有的对话")
    check("the source's session is in the target under the new id",
          os.path.isfile(os.path.join(dst, "sessions", new_id + ".json")))
    fork = read_json(os.path.join(dst, "sessions", "s_bbbb2222.json"))
    check("the fork points at the renamed parent",
          fork.get("forked_from") == new_id, fork.get("forked_from"))
    check("the trajectory of the renamed session follows",
          os.path.isfile(os.path.join(dst, "trajectories", new_id + ".jsonl")))
    index = read_json(os.path.join(dst, "sessions", "index.json"))
    ids = [e.get("id") for e in index]
    check("the index holds both the old and the new id",
          "s_aaaa1111" in ids and new_id in ids, ids)
    fork_entries = [e for e in index if e.get("id") == "s_bbbb2222"]
    check("the index of the fork points at the renamed parent",
          fork_entries and fork_entries[0].get("forked_from") == new_id, fork_entries)


def check_conflict_modes(root):
    """--conflict skip keeps the target's session, --conflict overwrite
    replaces it."""
    for mode, expect_source in (("skip", False), ("overwrite", True)):
        base = os.path.join(root, mode)
        os.makedirs(base)
        src = build_source(base)
        dst = build_target(base)
        clash = session("s_aaaa1111", "目标工作区已经有的对话", "2026-02-02T00:00:00Z",
                        "2026-02-02T00:10:00Z", [message("user", "同名但不同的会话")])
        write_json(os.path.join(dst, "sessions", "s_aaaa1111.json"), clash)
        code, out, err = run_tool([src, dst, "--conflict", mode, "--json"])
        check("the merge with --conflict %s succeeds" % mode, code == 0, err or out)
        title = read_json(os.path.join(dst, "sessions", "s_aaaa1111.json")).get("title")
        if expect_source:
            check("--conflict overwrite replaces the session",
                  title == "源工作区的对话", title)
        else:
            check("--conflict skip keeps the target's session",
                  title == "目标工作区已经有的对话", title)


def check_dry_run(root):
    """--dry-run must not touch a file."""
    src = build_source(root)
    dst = build_target(root)
    before = tree_digest(dst)
    code, out, err = run_tool([src, dst, "--dry-run"])
    check("the dry run succeeds", code == 0, err or out)
    check("the dry run changes nothing", tree_digest(dst) == before)
    check("the dry run says what it would do", "sessions" in out and "copied" in out, out)


def check_broken_index(root):
    """A broken index.json in the target must not make its own sessions
    disappear: the index is rebuilt from the session files."""
    src = build_source(root)
    dst = build_target(root)
    write_text(os.path.join(dst, "sessions", "index.json"), "{ this is not json")
    code, out, err = run_tool([src, dst])
    check("the merge with a broken index succeeds", code == 0, err or out)
    index = read_json(os.path.join(dst, "sessions", "index.json"))
    ids = sorted(e.get("id") for e in index)
    check("the index is rebuilt and holds every session",
          ids == ["s_aaaa1111", "s_bbbb2222", "s_cccc3333"], ids)


def check_workspace_root(root):
    """A workspace root (the directory that holds .tea_agent_run) is accepted."""
    src = build_source(root)
    dst = build_target(root)
    code, out, err = run_tool([os.path.dirname(src), os.path.dirname(dst)])
    check("a workspace root is accepted as a run directory", code == 0, err or out)
    check("the sessions landed in the right place",
          os.path.isfile(os.path.join(dst, "sessions", "s_aaaa1111.json")))


def main():
    if not os.path.isfile(TOOL):
        print("FAIL the tool is there: %s" % TOOL)
        return 1
    root = tempfile.mkdtemp(prefix="merge-work-area-")
    try:
        src, dst = check_merge(root)
        check_idempotent(src, dst)
        check_conflict(os.path.join(root, "conflict"))
        check_conflict_modes(os.path.join(root, "modes"))
        check_dry_run(os.path.join(root, "dry"))
        check_broken_index(os.path.join(root, "broken"))
        check_workspace_root(os.path.join(root, "roots"))
    finally:
        shutil.rmtree(root, ignore_errors=True)

    print()
    print("%d/%d checks passed" % (len(checks) - len(failures), len(checks)))
    if failures:
        print("failed: %s" % ", ".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
