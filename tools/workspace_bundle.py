#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""workspace_bundle.py -- 把一个工作区打成 zip，在另一台设备上还原。

`tea_agent` 把一个工作区知道的东西都放在 `<workspace>/.tea_agent_run/` 里。
「工作区包」把这些东西装进**一个 zip**，拷到另一台设备上导入，那边就多出这些
主题（会话）、轨迹、图片、记忆与工作区的经验：

    manifest.json              清单：导出时间、工作区、每个主题的元信息
    sessions/<id>.json         主题的完整记录（与磁盘上逐字节相同）
    trajectories/<id>.jsonl    每轮的轨迹（工具名、原始参数、输出）
    images/<topic>/<name>      贴进对话里的图片（消息里只存路径，字节在这里）
    memory.json                记忆条目
    agent.md                   这个工作区的经验

**这个包与服务端那份是同一个格式**：`GET /api/workspaces/{id}/bundle` 导出的包，
本工具能导入；本工具导出的包，`POST /api/workspaces/{id}/bundle/import` 也能导入。
所以这个工具是给「设备上还没升级到带这个接口的版本」用的——它只动目录里的文件，
不需要重启 agent，也不需要重新发版。

导入的规矩只有一条：**本地的数据说了算**。本地已经有的主题、轨迹、记忆条目都不动，
报告里说清留了哪些。所以同一个包导入两次，第二次什么也不会变。

用法：

    python tools/workspace_bundle.py export WORKSPACE [-o OUT.zip] [--json]
    python tools/workspace_bundle.py import WORKSPACE BUNDLE.zip [--dry-run] [--json]
    python tools/workspace_bundle.py inspect BUNDLE.zip [--json]

WORKSPACE 可以给工作区根目录（里面有一个 `.tea_agent_run/`），也可以直接给那个目录。
"""

from __future__ import print_function

import argparse
import gzip
import io
import json
import os
import re
import shutil
import sys
import time
import zipfile

RUN_DIR_NAME = ".tea_agent_run"

# The kind of a bundle. The agent refuses a zip that says something else.
BUNDLE_KIND = "tea_a2.workspace.bundle"
BUNDLE_SCHEMA = 1

# The agent's cap for agent.md (it is injected into the prompt of every turn).
AGENT_DOC_MAX_BYTES = 32 * 1024

# The memory caps, the same ones the agent enforces (see src/workspace.cpp:
# kMemoryManualCap / kMemoryAgentCap). The note that is confirmed longest ago
# goes first; among the agent's notes the weakest goes first.
MEMORY_MANUAL_CAP = 20
MEMORY_AGENT_CAP = 100
MEMORY_HALF_LIFE_DAYS = 14.0
MEMORY_EVICTED_KEEP = 200

# The id of a session may only hold these characters (the C++ side enforces the
# same, see trajectory_id_is_safe()).
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

# What a brand-new agent.md starts as. Keep in sync with kAgentDocTemplate in
# src/workspace.cpp: a file that is still this template says nothing, so a
# restore may replace it without losing anything.
AGENT_DOC_TEMPLATE = (
    "# agent.md - workspace experience\n"
    "\n"
    "Accumulated by the agent for THIS workspace. Maintained with `toolkit_agent_doc`;\n"
    "edited by hand is fine too. Keep it short: one line per entry, delete what is no\n"
    "longer true instead of piling up history. Reusable scripts live in `.tea_agent_run/scripts/`.\n"
    "\n"
    "## Overview\n"
    "\n"
    "## Commands & scripts worth reusing\n"
    "\n"
    "## Pitfalls\n"
    "\n"
    "## Conventions\n"
)


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def now_iso8601():
    """UTC, second resolution - the spelling the agent writes."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def read_json(path):
    """The parsed file, or None when it is missing or broken."""
    try:
        with io.open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (IOError, OSError, ValueError):
        return None


def dump_json(obj):
    """The same spelling the C++ side writes (indent 2, sorted keys, UTF-8)."""
    return json.dumps(obj, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def read_bytes(path):
    """The file as bytes, or None when it cannot be read."""
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except (IOError, OSError):
        return None


def write_bytes_atomic(path, data):
    """Write the bytes so a reader never sees half a file."""
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)
    tmp = "%s.tmp-%d" % (path, os.getpid())
    with open(tmp, "wb") as handle:
        handle.write(data)
    os.replace(tmp, path)


def write_text_atomic(path, text):
    write_bytes_atomic(path, text.encode("utf-8"))


def read_session_bytes(path):
    """The session file as it is stored, decompressing the .gz twin.

    The history cleaner compresses the cold half of the history; the bytes are
    the same JSON, only the way they are stored differs.
    """
    data = read_bytes(path)
    if data is None:
        return None
    if path.endswith(".gz"):
        try:
            return gzip.decompress(data)
        except (IOError, OSError, EOFError):
            return None
    return data


def resolve_run_dir(path):
    """Accept a `.tea_agent_run` directory or the workspace root that holds it.

    The workspace root always wins: a directory that is *not* named
    `.tea_agent_run` is a workspace, so the runtime store is the
    `.tea_agent_run` inside it - even when it does not exist yet (that is the
    normal case for a workspace the import creates).
    """
    full = os.path.abspath(os.path.expanduser(path))
    if os.path.basename(full) == RUN_DIR_NAME:
        return full
    return os.path.join(full, RUN_DIR_NAME)


def workspace_root_of(run_dir):
    """The workspace a run directory belongs to (its parent)."""
    if os.path.basename(run_dir) == RUN_DIR_NAME:
        return os.path.dirname(run_dir)
    return run_dir


def session_id_from_file(name):
    """The session id behind a session file name."""
    if name.endswith(".json.gz"):
        return name[:-len(".json.gz")]
    if name.endswith(".json"):
        return name[:-len(".json")]
    return ""


def trajectory_id_from_file(name):
    """The session id behind a trajectory file name."""
    if name.endswith(".jsonl.gz"):
        return name[:-len(".jsonl.gz")]
    if name.endswith(".jsonl"):
        return name[:-len(".jsonl")]
    return ""


def safe_id(session_id):
    return bool(session_id) and bool(SAFE_ID_RE.match(session_id))


def list_sessions(run_dir):
    """Every session of a workspace: (id, path). Sorted by id."""
    out = []
    sessions_dir = os.path.join(run_dir, "sessions")
    if not os.path.isdir(sessions_dir):
        return out
    for name in sorted(os.listdir(sessions_dir)):
        if name == "index.json":
            continue
        session_id = session_id_from_file(name)
        if not safe_id(session_id):
            continue
        out.append((session_id, os.path.join(sessions_dir, name)))
    return out


def session_meta(data):
    """What the manifest says about one session."""
    meta = {
        "title": "",
        "created_at": "",
        "updated_at": "",
        "messages": 0,
        "todo_total": 0,
        "todo_done": 0,
        "forked_from": "",
        "fork_turn": 0,
    }
    if not isinstance(data, dict):
        return meta
    meta["title"] = data.get("title") or ""
    meta["created_at"] = data.get("created_at") or ""
    meta["updated_at"] = data.get("updated_at") or ""
    messages = data.get("messages")
    meta["messages"] = len(messages) if isinstance(messages, list) else 0
    todos = data.get("todos")
    if isinstance(todos, list):
        meta["todo_total"] = len(todos)
        meta["todo_done"] = len([t for t in todos
                                 if isinstance(t, dict) and t.get("status") == "done"])
    meta["forked_from"] = data.get("forked_from") or ""
    try:
        meta["fork_turn"] = int(data.get("fork_turn") or 0)
    except (TypeError, ValueError):
        meta["fork_turn"] = 0
    return meta


def session_image_paths(data):
    """The pictures a session refers to (the paths as they are stored)."""
    out = []
    if not isinstance(data, dict):
        return out
    messages = data.get("messages")
    if not isinstance(messages, list):
        return out
    for message in messages:
        if not isinstance(message, dict):
            continue
        images = message.get("images")
        if not isinstance(images, list):
            continue
        for image in images:
            if not isinstance(image, dict):
                continue
            path = image.get("path")
            if not path or path in out:
                continue
            out.append(path)
    return out


def find_image_file(workspace_root, run_dir, stored_path):
    """Where the picture of a message actually is.

    The message stores a workspace-relative path, but the runtime directory
    does not have to live inside the workspace - then the path is absolute.
    Both are looked for, and the artifacts directory is the last resort.
    """
    if not stored_path:
        return None
    candidates = []
    if os.path.isabs(stored_path):
        candidates.append(os.path.abspath(stored_path))
    else:
        candidates.append(os.path.abspath(os.path.join(workspace_root, stored_path)))
    base = os.path.basename(stored_path.replace("\\", "/"))
    if base:
        candidates.append(os.path.join(run_dir, "artifacts", "images", base))
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    return None


def memory_notes(path):
    """The notes of a memory file (an empty list when there is no file)."""
    data = read_json(path)
    if not isinstance(data, dict):
        return []
    notes = data.get("notes")
    return notes if isinstance(notes, list) else []


def iso_to_epoch(text):
    """ISO-8601 UTC -> epoch seconds (0 when the string is not a timestamp)."""
    if not text:
        return 0
    try:
        return int(time.mktime(time.strptime(text[:19], "%Y-%m-%dT%H:%M:%S"))
                   - time.timezone)
    except (TypeError, ValueError):
        return 0


def note_weight(note, now):
    """The effective weight of a note: the base decayed by its age."""
    base = note.get("weight_base")
    if not isinstance(base, (int, float)) or base <= 0:
        base = 1.0 if note.get("source") != "agent" else 0.90
    base = min(1.0, float(base))
    anchor = note.get("decay_anchor") or note.get("created_at") or ""
    age_days = max(0.0, (now - iso_to_epoch(anchor)) / 86400.0)
    return base * (0.5 ** (age_days / MEMORY_HALF_LIFE_DAYS))


def note_last_write(note):
    """When the note was last confirmed (max of created_at and last_merge_at)."""
    created = note.get("created_at") or ""
    merged = note.get("last_merge_at") or ""
    return max(created, merged)


def apply_memory_caps(notes, now):
    """The same caps the agent applies; returns (kept, evicted)."""
    kept = list(notes)
    evicted = []

    def drop(index, reason):
        note = kept.pop(index)
        evicted.append({
            "id": note.get("id", ""),
            "text": note.get("text", ""),
            "source": note.get("source", "agent"),
            "created_at": note.get("created_at", ""),
            "last_write": note_last_write(note),
            "weight_base": note.get("weight_base", 0.0),
            "weight_effective": note_weight(note, now),
            "evicted_at": now_iso8601(),
            "reason": reason,
        })

    # Manual notes: the one confirmed longest ago goes first.
    while len([n for n in kept if n.get("source") != "agent"]) > MEMORY_MANUAL_CAP:
        oldest = None
        for index, note in enumerate(kept):
            if note.get("source") == "agent":
                continue
            stamp = iso_to_epoch(note_last_write(note))
            if oldest is None or stamp < oldest[0]:
                oldest = (stamp, index)
        if oldest is None:
            break
        drop(oldest[1], "manual_cap")

    # Agent notes: the weakest goes first, oldest among equals.
    while len([n for n in kept if n.get("source") == "agent"]) > MEMORY_AGENT_CAP:
        weakest = None
        for index, note in enumerate(kept):
            if note.get("source") != "agent":
                continue
            weight = note_weight(note, now)
            stamp = iso_to_epoch(note_last_write(note))
            if weakest is None or weight < weakest[0] or \
                    (weight == weakest[0] and stamp < weakest[1]):
                weakest = (weight, stamp, index)
        if weakest is None:
            break
        drop(weakest[2], "agent_cap")
    return kept, evicted


def append_eviction_audit(run_dir, evicted):
    """Keep a trace of what the caps dropped."""
    if not evicted:
        return
    path = os.path.join(run_dir, "memory", "evicted.jsonl")
    lines = []
    if os.path.isfile(path):
        try:
            with io.open(path, "r", encoding="utf-8") as handle:
                lines = [line for line in handle.read().split("\n") if line.strip()]
        except (IOError, OSError):
            lines = []
    for note in evicted:
        lines.append(json.dumps(note, ensure_ascii=False, sort_keys=True))
    if len(lines) > MEMORY_EVICTED_KEEP:
        lines = lines[len(lines) - MEMORY_EVICTED_KEEP:]
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)
    write_text_atomic(path, "\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------
def export_workspace(workspace_path, out_path):
    """Pack a workspace into a zip. Returns the report."""
    run_dir = resolve_run_dir(workspace_path)
    workspace_root = workspace_root_of(run_dir)
    report = {"sessions": 0, "trajectories": 0, "images": 0, "notes": 0,
              "agent_doc": False, "skipped": []}
    topics = []

    if not os.path.isdir(run_dir):
        raise RuntimeError("no runtime directory at %s" % run_dir)

    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as bundle:
        for session_id, path in list_sessions(run_dir):
            data = read_session_bytes(path)
            if data is None:
                report["skipped"].append("%s: cannot be read" % path)
                continue
            try:
                parsed = json.loads(data.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                report["skipped"].append("%s: not a session file" % path)
                continue
            entry = "sessions/%s.json" % session_id
            bundle.writestr(entry, data)

            topic = {"id": session_id, "file": entry}
            topic.update(session_meta(parsed))

            # The pictures the messages refer to.
            images = []
            for stored in session_image_paths(parsed):
                source = find_image_file(workspace_root, run_dir, stored)
                base = os.path.basename(stored.replace("\\", "/"))
                if source is None or not base:
                    report["skipped"].append("%s: the picture is not on this device" % stored)
                    continue
                with open(source, "rb") as handle:
                    payload = handle.read()
                image_entry = "images/%s/%s" % (session_id, base)
                bundle.writestr(image_entry, payload)
                images.append({"path": stored, "file": image_entry})
                report["images"] += 1
            topic["images"] = images

            # The trajectory of that topic.
            for candidate in (os.path.join(run_dir, "trajectories", "%s.jsonl" % session_id),
                              os.path.join(run_dir, "trajectories", "%s.jsonl.gz" % session_id)):
                payload = read_session_bytes(candidate)
                if payload is None:
                    continue
                topic["trajectory"] = "trajectories/%s.jsonl" % session_id
                bundle.writestr(topic["trajectory"], payload)
                report["trajectories"] += 1
                break

            topics.append(topic)
            report["sessions"] += 1

        # The memory.
        memory_path = os.path.join(run_dir, "memory", "memory.json")
        notes = memory_notes(memory_path)
        if notes or os.path.isfile(memory_path):
            bundle.writestr("memory.json",
                            dump_json({"notes": notes, "updated_at": now_iso8601()}))
            report["notes"] = len(notes)

        # The workspace's own experience.
        agent_doc = os.path.join(run_dir, "agent.md")
        if os.path.isfile(agent_doc):
            with io.open(agent_doc, "r", encoding="utf-8", newline="") as handle:
                text = handle.read()
            if text.strip():
                bundle.writestr("agent.md", text)
                report["agent_doc"] = True

        manifest = {
            "schema": BUNDLE_SCHEMA,
            "kind": BUNDLE_KIND,
            "version": "tool",
            "exported_at": now_iso8601(),
            "workspace": {
                "id": os.path.basename(workspace_root),
                "name": os.path.basename(workspace_root),
                "path": workspace_root,
            },
            "counts": {
                "sessions": report["sessions"],
                "trajectories": report["trajectories"],
                "images": report["images"],
                "notes": report["notes"],
            },
            "sessions": topics,
            "notes": report["notes"],
            "agent_doc": report["agent_doc"],
        }
        bundle.writestr("manifest.json", dump_json(manifest))
    return report


# ---------------------------------------------------------------------------
# import
# ---------------------------------------------------------------------------
class Importer(object):
    """Restores a bundle into a workspace. The local data always wins."""

    def __init__(self, workspace_path, dry_run=False):
        self.run_dir = resolve_run_dir(workspace_path)
        self.workspace_root = workspace_root_of(self.run_dir)
        self.dry_run = dry_run
        self.restored = {"sessions": 0, "trajectories": 0, "images": 0, "notes": 0,
                         "agent_doc": False}
        self.conflicts = {"sessions": 0, "trajectories": 0, "notes": 0, "agent_doc": False}
        self.notes = []
        self.warnings = []

    # -- helpers ----------------------------------------------------------
    def write(self, path, data):
        if self.dry_run:
            return
        write_bytes_atomic(path, data)

    def local_session_exists(self, session_id):
        sessions_dir = os.path.join(self.run_dir, "sessions")
        for name in ("%s.json" % session_id, "%s.json.gz" % session_id):
            if os.path.exists(os.path.join(sessions_dir, name)):
                return True
        return False

    def local_trajectory_exists(self, session_id):
        trajectories_dir = os.path.join(self.run_dir, "trajectories")
        for name in ("%s.jsonl" % session_id, "%s.jsonl.gz" % session_id):
            if os.path.exists(os.path.join(trajectories_dir, name)):
                return True
        return False

    # -- the pictures ------------------------------------------------------
    def restore_images(self, bundle):
        """The pictures first: the topics point at them."""
        images_dir = os.path.join(self.run_dir, "artifacts", "images")
        mapping = {}
        for name in bundle.namelist():
            if not name.startswith("images/"):
                continue
            base = os.path.basename(name)
            if not base or base in (".", ".."):
                continue
            payload = bundle.read(name)
            target = os.path.join(images_dir, base)
            if os.path.exists(target):
                with open(target, "rb") as handle:
                    local = handle.read()
                if local == payload:
                    mapping[base] = self.display_path(target)
                    continue
                # A different picture with the same name: keep the local one and
                # put the imported one next to it, so neither topic loses its
                # picture.
                stem, ext = os.path.splitext(base)
                target = os.path.join(images_dir, "%s_%s%s" % (stem, os.urandom(3).hex(), ext))
                self.warnings.append(
                    "the picture %s already existed here; the imported one was stored as %s"
                    % (base, os.path.basename(target)))
            self.write(target, payload)
            mapping[base] = self.display_path(target)
            self.restored["images"] += 1
        return mapping

    def display_path(self, absolute):
        """The path as the sessions store it: relative to the workspace root."""
        try:
            relative = os.path.relpath(absolute, self.workspace_root)
        except ValueError:
            return absolute
        if relative.startswith(".."):
            return absolute.replace("\\", "/")
        return relative.replace("\\", "/")

    # -- the topics --------------------------------------------------------
    def restore_sessions(self, bundle, images):
        sessions_dir = os.path.join(self.run_dir, "sessions")
        entries = []
        for name in bundle.namelist():
            if name.startswith("sessions/") and name.endswith(".json"):
                entries.append(name)
        restored = []
        for entry in entries:
            session_id = session_id_from_file(os.path.basename(entry))
            if not safe_id(session_id):
                self.warnings.append("a topic with an unusable name was left out: %s" % entry)
                continue
            if self.local_session_exists(session_id):
                # The local topic wins: it is the one this device has been using.
                self.conflicts["sessions"] += 1
                self.notes.append("topic %s is already here; the local one was kept" % session_id)
                continue
            payload = bundle.read(entry)
            try:
                parsed = json.loads(payload.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                self.warnings.append("%s is not a session file" % entry)
                continue
            if not isinstance(parsed, dict):
                self.warnings.append("%s is not a session file" % entry)
                continue
            inside = parsed.get("id")
            if isinstance(inside, str) and inside:
                if not safe_id(inside):
                    self.warnings.append("a topic with an unusable id was left out: %s" % inside)
                    continue
                if inside != session_id and self.local_session_exists(inside):
                    self.conflicts["sessions"] += 1
                    self.notes.append("topic %s is already here; the local one was kept" % inside)
                    continue
                session_id = inside

            # The pictures of this topic point at where they live on *this*
            # device now. When nothing changes, the file is written byte for
            # byte as it came - a restore that changes nothing is the best one.
            rewritten = False
            messages = parsed.get("messages")
            if isinstance(messages, list) and images:
                for message in messages:
                    if not isinstance(message, dict):
                        continue
                    for image in message.get("images") or []:
                        if not isinstance(image, dict):
                            continue
                        stored = image.get("path")
                        if not stored:
                            continue
                        base = os.path.basename(stored.replace("\\", "/"))
                        target = images.get(base)
                        if target is None or target == stored:
                            continue
                        image["path"] = target
                        rewritten = True
            data = dump_json(parsed).encode("utf-8") if rewritten else payload
            self.write(os.path.join(sessions_dir, "%s.json" % session_id), data)
            restored.append((session_id, parsed))
            self.restored["sessions"] += 1
        self.update_index(restored)

    def update_index(self, restored):
        """Put the restored topics into the sidebar index.

        The index belongs to whoever changes it: a turn that finishes right
        now writes its own entry, and this must not drop it.
        """
        if not restored:
            return
        index_path = os.path.join(self.run_dir, "sessions", "index.json")
        index = read_json(index_path)
        if not isinstance(index, list):
            index = []
        for session_id, parsed in restored:
            meta = session_meta(parsed)
            entry = {
                "id": session_id,
                "title": meta["title"],
                "created_at": meta["created_at"],
                "updated_at": meta["updated_at"],
                "message_count": meta["messages"],
            }
            if meta["todo_total"] > 0:
                entry["todo_total"] = meta["todo_total"]
                entry["todo_done"] = meta["todo_done"]
            if meta["forked_from"]:
                entry["forked_from"] = meta["forked_from"]
            if meta["fork_turn"] > 0:
                entry["fork_turn"] = meta["fork_turn"]
            replaced = False
            for index_of, existing in enumerate(index):
                if isinstance(existing, dict) and existing.get("id") == session_id:
                    index[index_of] = entry
                    replaced = True
                    break
            if not replaced:
                index.append(entry)
        self.write(index_path, dump_json(index).encode("utf-8"))

    # -- the trajectories ---------------------------------------------------
    def restore_trajectories(self, bundle):
        trajectories_dir = os.path.join(self.run_dir, "trajectories")
        for name in bundle.namelist():
            if not name.startswith("trajectories/"):
                continue
            session_id = trajectory_id_from_file(os.path.basename(name))
            if not safe_id(session_id):
                self.warnings.append("a trajectory with an unusable name was left out: %s" % name)
                continue
            if self.local_trajectory_exists(session_id):
                self.conflicts["trajectories"] += 1
                self.notes.append("the trajectory of %s is already here; the local one was kept"
                                  % session_id)
                continue
            self.write(os.path.join(trajectories_dir, "%s.jsonl" % session_id), bundle.read(name))
            self.restored["trajectories"] += 1

    # -- the memory ---------------------------------------------------------
    def restore_memory(self, bundle):
        if "memory.json" not in bundle.namelist():
            return
        try:
            data = json.loads(bundle.read("memory.json").decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self.warnings.append("memory.json is not readable")
            return
        incoming = data.get("notes") if isinstance(data, dict) else None
        if not isinstance(incoming, list) or not incoming:
            return
        memory_path = os.path.join(self.run_dir, "memory", "memory.json")
        local = memory_notes(memory_path)
        known = set()
        for note in local:
            if isinstance(note, dict) and note.get("id"):
                known.add(note["id"])
        for note in incoming:
            if not isinstance(note, dict):
                continue
            note = dict(note)
            note_id = note.get("id")
            if not note_id:
                note_id = "m_" + os.urandom(8).hex()
                note["id"] = note_id
            if note_id in known:
                # The local note wins: it is the one this device has been using.
                self.conflicts["notes"] += 1
                self.notes.append("memory note %s is already here; the local one was kept" % note_id)
                continue
            known.add(note_id)
            local.append(note)
            self.restored["notes"] += 1
        kept, evicted = apply_memory_caps(local, time.time())
        if self.dry_run:
            return
        write_text_atomic(memory_path, dump_json({"notes": kept, "updated_at": now_iso8601()}))
        append_eviction_audit(self.run_dir, evicted)

    # -- agent.md -----------------------------------------------------------
    def restore_agent_doc(self, bundle):
        if "agent.md" not in bundle.namelist():
            return
        try:
            with io.open(os.path.join(self.run_dir, "agent.md"), "r", encoding="utf-8",
                         newline="") as handle:
                local = handle.read()
        except (IOError, OSError):
            local = ""
        pristine = local.strip() == AGENT_DOC_TEMPLATE.strip() or not local.strip()
        if local.strip() and not pristine:
            # A file with content of its own is a real conflict - the local one
            # wins.
            self.conflicts["agent_doc"] = True
            self.notes.append("agent.md is already here; the local one was kept")
            return
        text = bundle.read("agent.md").decode("utf-8", "replace")
        if len(text.encode("utf-8")) > AGENT_DOC_MAX_BYTES:
            self.warnings.append("agent.md is bigger than %d bytes; it was left out"
                                 % AGENT_DOC_MAX_BYTES)
            return
        self.write(os.path.join(self.run_dir, "agent.md"), text.encode("utf-8"))
        self.restored["agent_doc"] = True

    # -- the whole thing ---------------------------------------------------
    def run(self, bundle_path):
        if not zipfile.is_zipfile(bundle_path):
            raise RuntimeError("%s is not a zip file" % bundle_path)
        with zipfile.ZipFile(bundle_path, "r") as bundle:
            names = bundle.namelist()
            if "manifest.json" not in names:
                raise RuntimeError("there is no manifest.json in it - this is not a workspace bundle")
            try:
                manifest = json.loads(bundle.read("manifest.json").decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                raise RuntimeError("the manifest.json is not readable")
            if isinstance(manifest, dict):
                kind = manifest.get("kind")
                if kind and kind != BUNDLE_KIND:
                    raise RuntimeError("this zip is not a workspace bundle (kind: %s)" % kind)
                schema = manifest.get("schema")
                if isinstance(schema, int) and schema > BUNDLE_SCHEMA:
                    raise RuntimeError("this bundle was made by a newer version (schema %d)" % schema)
            images = self.restore_images(bundle)
            self.restore_sessions(bundle, images)
            self.restore_trajectories(bundle)
            self.restore_memory(bundle)
            self.restore_agent_doc(bundle)
        return self.report()

    def report(self):
        return {
            "ok": True,
            "run_dir": self.run_dir,
            "sessions": self.restored["sessions"],
            "sessions_conflict": self.conflicts["sessions"],
            "trajectories": self.restored["trajectories"],
            "trajectories_conflict": self.conflicts["trajectories"],
            "images": self.restored["images"],
            "notes": self.restored["notes"],
            "notes_conflict": self.conflicts["notes"],
            "agent_doc": self.restored["agent_doc"],
            "agent_doc_conflict": self.conflicts["agent_doc"],
            "conflicts": self.notes,
            "warnings": self.warnings,
        }


# ---------------------------------------------------------------------------
# the command line
# ---------------------------------------------------------------------------
def print_report(report, as_json):
    if as_json:
        print(dump_json(report).rstrip())
        return
    if "skipped" in report:
        print("导出了 %d 个主题、%d 条轨迹、%d 张图片、%d 条记忆%s"
              % (report["sessions"], report["trajectories"], report["images"],
                 report["notes"], "、agent.md" if report.get("agent_doc") else ""))
        for item in report.get("skipped") or []:
            print("  跳过：%s" % item)
        return
    print("还原了 %d 个主题、%d 条轨迹、%d 张图片、%d 条记忆%s"
          % (report["sessions"], report["trajectories"], report["images"],
             report["notes"], "、agent.md" if report.get("agent_doc") else ""))
    if report.get("sessions_conflict") or report.get("notes_conflict") or \
            report.get("trajectories_conflict"):
        print("保留本地：%d 个主题、%d 条轨迹、%d 条记忆"
              % (report.get("sessions_conflict", 0), report.get("trajectories_conflict", 0),
                 report.get("notes_conflict", 0)))
    for item in report.get("conflicts") or []:
        print("  %s" % item)
    for item in report.get("warnings") or []:
        print("  注意：%s" % item)


def main(argv):
    parser = argparse.ArgumentParser(
        description="把一个工作区打成 zip（工作区包），在另一台设备上还原。")
    parser.add_argument("--json", action="store_true", help="机器可读的报告（JSON）")
    sub = parser.add_subparsers(dest="command")

    # --json is accepted before the subcommand ("--json export ...") and after
    # it ("export ... --json"): SUPPRESS keeps the subcommand from resetting
    # what the main parser already decided.
    export = sub.add_parser("export", help="把工作区打成 zip")
    export.add_argument("--json", action="store_true", default=argparse.SUPPRESS, dest="json",
                         help="机器可读的报告（JSON）")
    export.add_argument("workspace", help="工作区目录（或它的 .tea_agent_run）")
    export.add_argument("-o", "--output", help="写到哪里（默认 <工作区>-bundle-<时间>.zip）")

    importer = sub.add_parser("import", help="把工作区包还原到工作区")
    importer.add_argument("--json", action="store_true", default=argparse.SUPPRESS, dest="json",
                           help="机器可读的报告（JSON）")
    importer.add_argument("workspace", help="工作区目录（或它的 .tea_agent_run）")
    importer.add_argument("bundle", help="工作区包（zip）")
    importer.add_argument("--dry-run", action="store_true", help="只报告，不动文件")

    inspect = sub.add_parser("inspect", help="看看包里有什么")
    inspect.add_argument("--json", action="store_true", default=argparse.SUPPRESS, dest="json",
                          help="机器可读的报告（JSON）")
    inspect.add_argument("bundle", help="工作区包（zip）")

    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 2

    try:
        if args.command == "export":
            run_dir = resolve_run_dir(args.workspace)
            root = workspace_root_of(run_dir)
            output = args.output
            if not output:
                stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
                output = os.path.join(os.path.dirname(root) or ".",
                                      "%s-bundle-%s.zip" % (os.path.basename(root), stamp))
            report = export_workspace(args.workspace, output)
            report["output"] = output
            print_report(report, args.json)
            return 0

        if args.command == "import":
            report = Importer(args.workspace, args.dry_run).run(args.bundle)
            print_report(report, args.json)
            return 0

        if args.command == "inspect":
            if not zipfile.is_zipfile(args.bundle):
                print("%s is not a zip file" % args.bundle, file=sys.stderr)
                return 1
            with zipfile.ZipFile(args.bundle, "r") as bundle:
                manifest = {}
                if "manifest.json" in bundle.namelist():
                    try:
                        manifest = json.loads(bundle.read("manifest.json").decode("utf-8"))
                    except (ValueError, UnicodeDecodeError):
                        manifest = {}
                report = {"manifest": manifest, "entries": bundle.namelist()}
            if args.json:
                print(dump_json(report).rstrip())
                return 0
            print("工作区包：%s" % args.bundle)
            if manifest:
                workspace = manifest.get("workspace") or {}
                print("  来自：%s（导出于 %s）"
                      % (workspace.get("path") or workspace.get("name") or "?",
                         manifest.get("exported_at") or "?"))
                counts = manifest.get("counts") or {}
                print("  %s 个主题、%s 条轨迹、%s 张图片、%s 条记忆"
                      % (counts.get("sessions", 0), counts.get("trajectories", 0),
                         counts.get("images", 0), counts.get("notes", 0)))
                for topic in manifest.get("sessions") or []:
                    print("  - %s  %s" % (topic.get("id"), topic.get("title") or ""))
            print("  %d 个条目" % len(report["entries"]))
            return 0
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except (IOError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
