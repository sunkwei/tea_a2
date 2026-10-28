#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""merge_work_area.py -- merge one workspace's runtime store into another.

`tea_agent` keeps everything it knows about a workspace inside
`<workspace>/.tea_agent_run/`:

    meta.json                 the identity of the workspace
    sessions/<sid>.json       the transcript of one conversation
    sessions/index.json       the sidebar index (title / time / counts)
    memory/memory.json        the long-term notes
    trajectories/<sid>.jsonl  one line per agent turn (the audit trail)
    agent.md                  the experience the agent accumulated
    artifacts/                the images and files the sessions point at
    audit/audit-<date>.jsonl  the hash-chained tool-call audit
    logs/agent.log            the workspace log
    scripts/                  reusable scripts

When a workspace moves to another machine - or when two machines worked on the
same project and their histories should end up in one place - the store has to
be *merged*, not copied: a plain copy overwrites the target's newer sessions
and drops its memory.

This tool merges `from_run_path` into `to_run_path`:

  * sessions      - the transcripts are copied and `sessions/index.json` is
                    rebuilt from both sides. A session id that the target
                    already uses is renamed (or skipped / overwritten, see
                    --conflict), and the lineage of forked sessions is
                    rewritten to the new ids so a branch keeps pointing at its
                    parent.
  * memory        - the notes are appended; a note that is already there (same
                    id or same text) is not duplicated.
  * trajectories  - the per-turn records are copied, or merged turn by turn
                    when both sides have a file for the same session.
  * agent.md      - the sections are merged and a line that is already there is
                    not duplicated. The 32 KB cap of the file is respected.
  * artifacts     - the images the merged sessions point at are copied, so the
                    conversations still show them.
  * audit         - the hash-chained files are copied under a new name: every
                    file is a chain of its own, and splicing one chain into
                    another would break it.
  * logs          - the workspace log is appended, behind a marker that makes
                    a second run a no-op.
  * scripts       - the scripts the target does not have yet are copied.

The source is never written to, a second run changes nothing (it reports
"already there" instead of copying again), and `--dry-run` prints the plan
without touching a file.

Usage:
    python tools/merge_work_area.py FROM_RUN_PATH TO_RUN_PATH [options]

    FROM_RUN_PATH / TO_RUN_PATH are `.tea_agent_run` directories; a workspace
    root that holds one is accepted as well.
"""

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import sys
import time

RUN_DIR_NAME = ".tea_agent_run"
CATEGORIES = ("sessions", "memory", "trajectories", "agent-doc", "artifacts",
              "audit", "logs", "scripts")

# The agent refuses to write an agent.md bigger than this (it is injected into
# the prompt of every turn), so the merge respects the same cap.
AGENT_DOC_MAX_BYTES = 32 * 1024

# The id of a session may only hold these characters (the C++ side enforces
# the same, see trajectory_id_is_safe()).
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


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


def read_text(path):
    try:
        with io.open(path, "r", encoding="utf-8", newline="") as handle:
            return handle.read()
    except (IOError, OSError):
        return None


def file_digest(path):
    """sha256 of a file - used to recognise a session that is already there."""
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            while True:
                chunk = handle.read(1 << 16)
                if not chunk:
                    break
                digest.update(chunk)
    except (IOError, OSError):
        return ""
    return digest.hexdigest()


def bytes_digest(data):
    return hashlib.sha256(data).hexdigest()


def canonical_digest(obj):
    """The digest of a JSON value, independent of how the file spells it."""
    text = json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def session_identity_digest(data):
    """The digest of a session without its id and its lineage.

    A second run of the merge has to recognise a conversation that is already
    in the target - also when the first run had to rename it because the id
    was taken.
    """
    if not isinstance(data, dict):
        return ""
    stripped = dict(data)
    stripped.pop("id", None)
    stripped.pop("forked_from", None)
    return canonical_digest(stripped)


def new_session_id(taken):
    """A session id in the shape the agent makes: `s_` + 12 hex digits."""
    for _ in range(1000):
        candidate = "s_" + os.urandom(6).hex()
        if candidate not in taken:
            return candidate
    raise RuntimeError("cannot find a free session id")


def safe_label(text):
    """A file-name fragment that is safe on every file system."""
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", (text or "").strip())
    return cleaned.strip("-.") or "src"


def resolve_run_dir(path):
    """Accept a `.tea_agent_run` directory or the workspace root that holds it."""
    full = os.path.abspath(os.path.expanduser(path))
    if os.path.basename(full) == RUN_DIR_NAME:
        return full
    inner = os.path.join(full, RUN_DIR_NAME)
    if os.path.isdir(inner):
        return inner
    return full


# ---------------------------------------------------------------------------
# the merge itself
# ---------------------------------------------------------------------------
class Merger(object):
    def __init__(self, src, dst, options):
        self.src = src
        self.dst = dst
        self.options = options
        self.actions = []
        self.errors = []
        self.counts = {}
        self.backup_root = None
        self.session_map = {}      # source session id -> target session id
        self.merged_artifacts = []

    # -- bookkeeping ------------------------------------------------------
    def act(self, category, action, path, detail=""):
        self.actions.append({
            "category": category,
            "action": action,
            "path": path,
            "detail": detail,
        })
        bucket = self.counts.setdefault(category, {})
        bucket[action] = bucket.get(action, 0) + 1

    def fail(self, category, path, detail):
        self.errors.append({
            "category": category,
            "path": path,
            "detail": detail,
        })

    def relative(self, path):
        """The path as it should be shown to a human."""
        try:
            rel = os.path.relpath(path, self.dst)
        except ValueError:
            rel = path
        return rel.replace("\\", "/")

    # -- writing ----------------------------------------------------------
    def ensure_dir(self, path):
        if os.path.isdir(path):
            return True
        if self.options.dry_run:
            return True
        try:
            os.makedirs(path)
            return True
        except OSError as exc:
            self.fail("write", path, str(exc))
            return False

    def backup(self, path):
        """Keep a copy of a file we are about to replace."""
        if self.options.no_backup or self.options.dry_run:
            return
        if not os.path.isfile(path):
            return
        if self.backup_root is None:
            stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
            self.backup_root = os.path.join(self.dst, ".merge-backups", stamp)
        target = os.path.join(self.backup_root, self.relative(path))
        try:
            parent = os.path.dirname(target)
            if parent and not os.path.isdir(parent):
                os.makedirs(parent)
            shutil.copy2(path, target)
        except (IOError, OSError) as exc:
            self.fail("backup", path, str(exc))

    def write_text(self, path, text):
        """Write a file the way the agent does: a temp file next to it, then a
        rename - a reader sees either the old file or the new one, never half
        of one."""
        if self.options.dry_run:
            return True
        if not self.ensure_dir(os.path.dirname(path)):
            return False
        self.backup(path)
        tmp = "%s.tmp-merge-%d" % (path, os.getpid())
        try:
            with io.open(tmp, "w", encoding="utf-8", newline="") as handle:
                handle.write(text)
            os.replace(tmp, path)
        except (IOError, OSError) as exc:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
            self.fail("write", path, str(exc))
            return False
        return True

    def write_json(self, path, obj):
        return self.write_text(path, dump_json(obj))

    def copy_file(self, src, dst, category, detail=""):
        """Copy a file that the target does not have yet."""
        if os.path.exists(dst):
            if os.path.getsize(src) == os.path.getsize(dst):
                self.act(category, "skipped", self.relative(dst),
                         "the target already has this file")
                return False
            self.act(category, "skipped", self.relative(dst),
                     "the target has a different file of that name")
            return False
        if self.options.dry_run:
            self.act(category, "copied", self.relative(dst), detail)
            return True
        if not self.ensure_dir(os.path.dirname(dst)):
            return False
        try:
            shutil.copy2(src, dst)
        except (IOError, OSError) as exc:
            self.fail(category, src, str(exc))
            return False
        self.act(category, "copied", self.relative(dst), detail)
        return True

    # -- sessions ---------------------------------------------------------
    def scan_sessions(self, sessions_dir):
        """Build index entries from the session files themselves."""
        entries = []
        if not os.path.isdir(sessions_dir):
            return entries
        for name in sorted(os.listdir(sessions_dir)):
            if not name.endswith(".json") or name == "index.json":
                continue
            data = read_json(os.path.join(sessions_dir, name))
            if not isinstance(data, dict):
                continue
            entries.append(self.entry_from_session(name[:-5], data))
        return entries

    def entry_from_session(self, session_id, data):
        """The sidebar entry of a session - the same fields the C++ side writes."""
        todos = data.get("todos")
        total = 0
        done = 0
        if isinstance(todos, list):
            total = len(todos)
            for item in todos:
                if isinstance(item, dict) and item.get("status") == "done":
                    done += 1
        messages = data.get("messages")
        entry = {
            "id": session_id,
            "title": data.get("title", "") or "",
            "created_at": data.get("created_at", "") or "",
            "updated_at": data.get("updated_at", "") or "",
            "message_count": len(messages) if isinstance(messages, list) else 0,
        }
        if total > 0:
            entry["todo_total"] = total
            entry["todo_done"] = done
        if data.get("forked_from"):
            entry["forked_from"] = data["forked_from"]
        if data.get("fork_turn"):
            entry["fork_turn"] = data["fork_turn"]
        return entry

    def read_index(self, sessions_dir):
        """The sidebar index. A missing or broken index.json must not make the
        target's own sessions disappear from the sidebar, so it is rebuilt from
        the session files in that case."""
        data = read_json(os.path.join(sessions_dir, "index.json"))
        if isinstance(data, list):
            return [e for e in data if isinstance(e, dict)], "index"
        return self.scan_sessions(sessions_dir), "scan"

    def merge_sessions(self):
        src_dir = os.path.join(self.src, "sessions")
        dst_dir = os.path.join(self.dst, "sessions")
        if not os.path.isdir(src_dir):
            return
        self.ensure_dir(dst_dir)

        src_files = sorted(name for name in os.listdir(src_dir)
                           if name.endswith(".json") and name != "index.json")
        if not src_files:
            return

        dst_files = {}
        if os.path.isdir(dst_dir):
            for name in os.listdir(dst_dir):
                if name.endswith(".json") and name != "index.json":
                    dst_files[name[:-5]] = os.path.join(dst_dir, name)

        # The content of every session the target has: a session that is
        # already there (a previous merge, a copy) must not be copied twice.
        dst_digests = {}
        for session_id, path in dst_files.items():
            digest = session_identity_digest(read_json(path))
            if not digest:
                continue
            dst_digests.setdefault(digest, []).append(session_id)

        src_index, _ = self.read_index(src_dir)
        src_index_by_id = {}
        for entry in src_index:
            if entry.get("id"):
                src_index_by_id[entry["id"]] = entry

        # Pass 1: where does every source session go?
        plan = []
        used = {}
        taken = set(dst_files.keys())
        for name in src_files:
            src_id = name[:-5]
            src_path = os.path.join(src_dir, name)
            digest = session_identity_digest(read_json(src_path))
            same = dst_digests.get(digest, [])
            if len(same) > used.get(digest, 0):
                used[digest] = used.get(digest, 0) + 1
                dst_id = same[used[digest] - 1]
                self.session_map[src_id] = dst_id
                plan.append((src_id, src_path, dst_id, "skipped",
                             "the target already has this conversation"))
                continue
            if src_id in dst_files:
                if self.options.conflict == "skip":
                    self.session_map[src_id] = src_id
                    plan.append((src_id, src_path, src_id, "skipped",
                                 "the target already has a session with this id"))
                    continue
                if self.options.conflict == "overwrite":
                    self.session_map[src_id] = src_id
                    plan.append((src_id, src_path, src_id, "overwritten",
                                 "the target's copy is replaced"))
                    continue
                dst_id = new_session_id(taken)
                taken.add(dst_id)
                self.session_map[src_id] = dst_id
                plan.append((src_id, src_path, dst_id, "renamed",
                             "the target already uses %s" % src_id))
                continue
            self.session_map[src_id] = src_id
            plan.append((src_id, src_path, src_id, "copied", ""))

        # Pass 2: write the transcripts.
        written = []
        for src_id, src_path, dst_id, action, detail in plan:
            if action == "skipped":
                self.act("sessions", "skipped", "sessions/%s.json" % dst_id, detail)
                continue
            data = read_json(src_path)
            if not isinstance(data, dict):
                self.fail("sessions", src_path, "the session file is not a JSON object")
                continue
            data["id"] = dst_id
            parent = data.get("forked_from")
            if isinstance(parent, str) and parent in self.session_map:
                data["forked_from"] = self.session_map[parent]
            dst_path = os.path.join(dst_dir, dst_id + ".json")
            if self.write_json(dst_path, data):
                written.append((src_id, dst_id, data))
                self.act("sessions", action, "sessions/%s.json" % dst_id, detail)

        # The sidebar index: the target's entries plus one per merged session.
        dst_entries, index_source = self.read_index(dst_dir)
        entries = [dict(e) for e in dst_entries]
        position = {}
        for i, entry in enumerate(entries):
            if entry.get("id"):
                position[entry["id"]] = i
        for src_id, dst_id, data in written:
            entry = src_index_by_id.get(src_id)
            if isinstance(entry, dict):
                entry = dict(entry)
            else:
                entry = self.entry_from_session(dst_id, data)
            entry["id"] = dst_id
            parent = entry.get("forked_from")
            if isinstance(parent, str) and parent in self.session_map:
                entry["forked_from"] = self.session_map[parent]
            if dst_id in position:
                entries[position[dst_id]] = entry
            else:
                position[dst_id] = len(entries)
                entries.append(entry)
        if entries or index_source == "index":
            self.write_json(os.path.join(dst_dir, "index.json"), entries)

    # -- memory -----------------------------------------------------------
    def merge_memory(self):
        src_path = os.path.join(self.src, "memory", "memory.json")
        dst_path = os.path.join(self.dst, "memory", "memory.json")
        if not os.path.isfile(src_path):
            return
        src_mem = read_json(src_path)
        if not isinstance(src_mem, dict):
            self.fail("memory", src_path, "the memory file is not a JSON object")
            return
        src_notes = src_mem.get("notes")
        if not isinstance(src_notes, list):
            return

        dst_mem = read_json(dst_path)
        dst_notes = []
        if isinstance(dst_mem, dict) and isinstance(dst_mem.get("notes"), list):
            dst_notes = [n for n in dst_mem["notes"] if isinstance(n, dict)]

        seen_ids = set()
        seen_texts = set()
        for note in dst_notes:
            if note.get("id"):
                seen_ids.add(note["id"])
            if note.get("text"):
                seen_texts.add(note["text"])

        added = 0
        for note in src_notes:
            if not isinstance(note, dict):
                continue
            note_id = note.get("id")
            text = note.get("text")
            if note_id and note_id in seen_ids:
                self.act("memory", "skipped", "memory/memory.json",
                         "the note %s is already there" % note_id)
                continue
            if text and text in seen_texts:
                self.act("memory", "skipped", "memory/memory.json",
                         "the same note is already there")
                continue
            dst_notes.append(note)
            if note_id:
                seen_ids.add(note_id)
            if text:
                seen_texts.add(text)
            added += 1
        if added:
            payload = {"notes": dst_notes, "updated_at": now_iso8601()}
            if self.write_json(dst_path, payload):
                self.act("memory", "copied", "memory/memory.json",
                         "%d note(s) added" % added)

    # -- trajectories -----------------------------------------------------
    def target_workspace_id(self):
        meta = read_json(os.path.join(self.dst, "meta.json"))
        if isinstance(meta, dict) and meta.get("id"):
            return meta["id"]
        return ""

    def merge_trajectories(self):
        src_dir = os.path.join(self.src, "trajectories")
        dst_dir = os.path.join(self.dst, "trajectories")
        if not os.path.isdir(src_dir):
            return
        self.ensure_dir(dst_dir)
        workspace_id = self.target_workspace_id()
        for name in sorted(os.listdir(src_dir)):
            if not name.endswith(".jsonl"):
                continue
            src_path = os.path.join(src_dir, name)
            src_sid = name[:-len(".jsonl")]
            dst_sid = self.session_map.get(src_sid, src_sid)
            dst_path = os.path.join(dst_dir, dst_sid + ".jsonl")
            existed = os.path.exists(dst_path)
            merged = self.merge_trajectory_files(dst_path, src_path, dst_sid,
                                                 workspace_id)
            if merged is None:
                continue
            if existed and merged == (read_text(dst_path) or ""):
                self.act("trajectories", "skipped",
                         "trajectories/%s.jsonl" % dst_sid,
                         "the target already has these turns")
                continue
            if self.write_text(dst_path, merged):
                self.act("trajectories", "merged" if existed else "copied",
                         "trajectories/%s.jsonl" % dst_sid,
                         "the turns of both sides are in one file" if existed else "")

    def parse_record(self, line):
        try:
            record = json.loads(line)
        except ValueError:
            return None
        if not isinstance(record, dict):
            return None
        return record

    def merge_trajectory_files(self, dst_path, src_path, session_id, workspace_id):
        """One file per session on both sides: put the turns of both into one,
        newest snapshot of a turn wins (the agent rewrites the last line of a
        turn in place)."""
        records = []       # (sort key, line)
        seen = {}          # turn id -> position in `records`
        order = 0
        for path in (dst_path, src_path):
            text = read_text(path)
            if text is None:
                continue
            for line in text.split("\n"):
                if not line.strip():
                    continue
                record = self.parse_record(line)
                if record is None:
                    # A torn line: the agent tolerates it, so keep it (at the
                    # end) instead of dropping somebody's record.
                    records.append(((u"9999", order), line))
                    order += 1
                    continue
                record["session_id"] = session_id
                if workspace_id:
                    record["workspace_id"] = workspace_id
                line = json.dumps(record, ensure_ascii=False, sort_keys=True)
                turn_id = record.get("turn_id")
                started = record.get("started_at") or ""
                if turn_id and turn_id in seen:
                    records[seen[turn_id]] = ((started, order), line)
                else:
                    key = ((started, order), line)
                    if turn_id:
                        seen[turn_id] = len(records)
                    records.append(key)
                order += 1
        if not records:
            return None
        records.sort(key=lambda item: item[0])
        return "\n".join(line for _key, line in records) + "\n"

    # -- agent.md ---------------------------------------------------------
    def split_sections(self, text):
        """The document as (preamble, [(heading, [lines])])."""
        preamble = []
        sections = []
        current = None
        for line in text.split("\n"):
            if line.startswith("## "):
                current = (line.rstrip(), [])
                sections.append(current)
                continue
            if current is None:
                preamble.append(line)
            else:
                current[1].append(line)
        return preamble, sections

    def merge_agent_doc(self):
        src_path = os.path.join(self.src, "agent.md")
        dst_path = os.path.join(self.dst, "agent.md")
        if not os.path.isfile(src_path):
            return
        src_text = read_text(src_path)
        if src_text is None:
            self.fail("agent-doc", src_path, "cannot read the file")
            return
        if not os.path.exists(dst_path):
            if self.write_text(dst_path, src_text):
                self.act("agent-doc", "copied", "agent.md", "the document is new here")
            return

        dst_text = read_text(dst_path) or ""
        _preamble, dst_sections = self.split_sections(dst_text)
        _src_preamble, src_sections = self.split_sections(src_text)

        added = 0
        for heading, lines in src_sections:
            body = [l for l in lines if l.strip()]
            if not body:
                continue
            target = None
            for h, l in dst_sections:
                if h == heading:
                    target = l
                    break
            if target is None:
                # A section the target does not have: take it over as it is.
                dst_sections.append((heading, list(lines)))
                added += len(body)
                continue
            known = set(l.strip() for l in target)
            for line in body:
                if line.strip() in known:
                    continue
                target.append(line)
                known.add(line.strip())
                added += 1

        out = []
        for heading, lines in dst_sections:
            out.append(heading)
            out.extend(lines)
        text = "\n".join(out).rstrip("\n")
        if text:
            text += "\n"
        if len(text.encode("utf-8")) > AGENT_DOC_MAX_BYTES:
            # The agent refuses to write a bigger file; keep the target's
            # document and say so instead of writing something it will reject.
            self.fail("agent-doc", dst_path,
                      "the merged document would be %d bytes; the limit is %d - "
                      "the target's document was left as it is"
                      % (len(text.encode("utf-8")), AGENT_DOC_MAX_BYTES))
            return
        if added:
            if self.write_text(dst_path, text):
                self.act("agent-doc", "merged", "agent.md",
                         "%d line(s) added" % added)

    # -- artifacts --------------------------------------------------------
    def referenced_artifacts(self, src_dir):
        """The run-dir files the sessions point at (the images of a
        conversation)."""
        found = set()
        if not os.path.isdir(src_dir):
            return found
        for name in sorted(os.listdir(src_dir)):
            if not name.endswith(".json") or name == "index.json":
                continue
            data = read_json(os.path.join(src_dir, name))
            if not isinstance(data, dict):
                continue
            messages = data.get("messages")
            if isinstance(messages, list):
                for message in messages:
                    if not isinstance(message, dict):
                        continue
                    images = message.get("images")
                    if not isinstance(images, list):
                        continue
                    for image in images:
                        if isinstance(image, dict) and isinstance(image.get("path"), str):
                            found.add(image["path"])
        return found

    def merge_artifacts(self):
        src_dir = os.path.join(self.src, "artifacts")
        dst_dir = os.path.join(self.dst, "artifacts")
        if not os.path.isdir(src_dir):
            return
        self.ensure_dir(dst_dir)
        if self.options.copy_all_artifacts:
            for root, _dirs, files in os.walk(src_dir):
                for name in sorted(files):
                    src_path = os.path.join(root, name)
                    rel = os.path.relpath(src_path, src_dir)
                    self.copy_file(src_path, os.path.join(dst_dir, rel),
                                   "artifacts", "every artifact was taken")
            return
        for path in sorted(self.referenced_artifacts(os.path.join(self.src, "sessions"))):
            rel = path.replace("\\", "/")
            prefix = RUN_DIR_NAME + "/"
            if not rel.startswith(prefix):
                # A file outside the run dir (a project file): it belongs to
                # the workspace, not to the store, so it is not copied.
                continue
            rel = rel[len(prefix):]
            src_path = os.path.join(self.src, rel.replace("/", os.sep))
            if not os.path.isfile(src_path):
                continue
            # `rel` is relative to the run directory, so the target is
            # <run dir>/<rel> - not <run dir>/artifacts/<rel>.
            self.copy_file(src_path, os.path.join(self.dst, rel.replace("/", os.sep)),
                           "artifacts", "an image of a merged conversation")

    # -- audit ------------------------------------------------------------
    def merge_audit(self):
        src_dir = os.path.join(self.src, "audit")
        dst_dir = os.path.join(self.dst, "audit")
        if not os.path.isdir(src_dir):
            return
        self.ensure_dir(dst_dir)
        label = safe_label(os.path.basename(os.path.dirname(self.src)))
        for name in sorted(os.listdir(src_dir)):
            if not name.endswith(".jsonl"):
                continue
            src_path = os.path.join(src_dir, name)
            stem = name[:-len(".jsonl")]
            # Every audit file is a hash chain of its own: splicing one chain
            # into another would break it, so the file is copied under a new
            # name and both chains stay intact.
            base = "%s-from-%s" % (stem, label)
            dst_path = os.path.join(dst_dir, base + ".jsonl")
            counter = 2
            while os.path.exists(dst_path) and file_digest(dst_path) != file_digest(src_path):
                dst_path = os.path.join(dst_dir, "%s-%d.jsonl" % (base, counter))
                counter += 1
            if os.path.exists(dst_path):
                self.act("audit", "skipped", self.relative(dst_path),
                         "the target already has this audit file")
                continue
            self.copy_file(src_path, dst_path, "audit", "the chain is kept as it is")

    # -- logs -------------------------------------------------------------
    def merge_logs(self):
        src_path = os.path.join(self.src, "logs", "agent.log")
        dst_path = os.path.join(self.dst, "logs", "agent.log")
        if not os.path.isfile(src_path):
            return
        src_text = read_text(src_path)
        if not src_text:
            return
        self.ensure_dir(os.path.dirname(dst_path))
        marker = "# ---- merged from: %s ----" % os.path.abspath(src_path)
        dst_text = read_text(dst_path) or ""
        if marker in dst_text:
            self.act("logs", "skipped", "logs/agent.log",
                     "this log was appended before")
            return
        block = dst_text
        if block and not block.endswith("\n"):
            block += "\n"
        block += marker + "\n" + src_text
        if not block.endswith("\n"):
            block += "\n"
        if self.write_text(dst_path, block):
            self.act("logs", "copied", "logs/agent.log",
                     "%d line(s) appended" % len(src_text.split("\n")))

    # -- scripts ----------------------------------------------------------
    def merge_scripts(self):
        src_dir = os.path.join(self.src, "scripts")
        dst_dir = os.path.join(self.dst, "scripts")
        if not os.path.isdir(src_dir):
            return
        self.ensure_dir(dst_dir)
        for root, _dirs, files in os.walk(src_dir):
            for name in sorted(files):
                src_path = os.path.join(root, name)
                rel = os.path.relpath(src_path, src_dir)
                self.copy_file(src_path, os.path.join(dst_dir, rel),
                               "scripts", "a script the target does not have")

    # -- the run ----------------------------------------------------------
    def run(self):
        if not os.path.isdir(self.src):
            raise SystemExit("the source does not exist: %s" % self.src)
        if not os.path.isdir(self.dst):
            if self.options.dry_run:
                pass
            else:
                try:
                    os.makedirs(self.dst)
                except OSError as exc:
                    raise SystemExit("cannot create the target: %s" % exc)
        if os.path.abspath(self.src) == os.path.abspath(self.dst):
            raise SystemExit("the source and the target are the same directory")

        for category in CATEGORIES:
            if category not in self.options.only:
                continue
            if category in self.options.skip:
                continue
            getattr(self, "merge_" + category.replace("-", "_"))()

        self.write_provenance()
        return self.report()

    def write_provenance(self):
        """A line in `<to>/merges.jsonl` saying where the history came from."""
        if self.options.dry_run:
            return
        record = {
            "at": now_iso8601(),
            "from": self.src,
            "to": self.dst,
            "categories": list(self.options.only),
            "sessions": len(self.session_map),
            "actions": len(self.actions),
            "errors": len(self.errors),
        }
        path = os.path.join(self.dst, "merges.jsonl")
        try:
            with io.open(path, "a", encoding="utf-8", newline="") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        except (IOError, OSError) as exc:
            self.fail("write", path, str(exc))

    def report(self):
        return {
            "from": self.src,
            "to": self.dst,
            "dry_run": bool(self.options.dry_run),
            "counts": self.counts,
            "actions": self.actions,
            "errors": self.errors,
            "session_map": self.session_map,
            "backup": self.relative(self.backup_root) if self.backup_root else "",
        }


# ---------------------------------------------------------------------------
# command line
# ---------------------------------------------------------------------------
def parse_args(argv):
    parser = argparse.ArgumentParser(
        description="Merge one workspace's .tea_agent_run into another.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  # see what would happen
  python tools/merge_work_area.py /old/project/.tea_agent_run ./.tea_agent_run --dry-run

  # merge, keeping the target's session ids (rename the ones that clash)
  python tools/merge_work_area.py /old/project/.tea_agent_run ./.tea_agent_run
""")
    parser.add_argument("from_run_path", help="the .tea_agent_run to merge from")
    parser.add_argument("to_run_path", help="the .tea_agent_run to merge into")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the plan, change nothing")
    parser.add_argument("--conflict", choices=("rename", "skip", "overwrite"),
                        default="rename",
                        help="what to do when both sides have the same session id "
                             "(default: rename)")
    parser.add_argument("--only", default="",
                        help="only these categories (%s)" % ", ".join(CATEGORIES))
    parser.add_argument("--skip", default="",
                        help="everything but these categories")
    parser.add_argument("--copy-all-artifacts", action="store_true",
                        help="copy the whole artifacts directory, not only the "
                             "files the merged conversations point at")
    parser.add_argument("--no-backup", action="store_true",
                        help="do not keep a copy of the files that are replaced")
    parser.add_argument("--json", action="store_true", dest="as_json",
                        help="print the report as JSON")
    parser.add_argument("--quiet", action="store_true",
                        help="only print the summary")
    options = parser.parse_args(argv)

    only = [c.strip() for c in options.only.split(",") if c.strip()]
    skip = [c.strip() for c in options.skip.split(",") if c.strip()]
    for name in only + skip:
        if name not in CATEGORIES:
            parser.error("unknown category: %s (known: %s)" % (name, ", ".join(CATEGORIES)))
    if only:
        options.only = only
    else:
        options.only = [c for c in CATEGORIES if c not in skip]
    options.skip = skip
    return options


def print_report(report, options):
    if options.as_json:
        sys.stdout.write(json.dumps(report, ensure_ascii=False, indent=2,
                                    sort_keys=True) + "\n")
        return
    if options.quiet:
        return
    print("merge %s -> %s%s" % (report["from"], report["to"],
                                "   (dry run)" if report["dry_run"] else ""))
    for category in CATEGORIES:
        counts = report["counts"].get(category)
        if not counts:
            continue
        parts = []
        for action in sorted(counts):
            parts.append("%d %s" % (counts[action], action))
        print("  %-12s: %s" % (category, ", ".join(parts)))
    if report["backup"]:
        print("  %-12s: %s" % ("backup", report["backup"]))
    for error in report["errors"]:
        print("  ERROR %s: %s (%s)" % (error["category"], error["detail"],
                                       error["path"]))


def main(argv=None):
    options = parse_args(sys.argv[1:] if argv is None else argv)
    src = resolve_run_dir(options.from_run_path)
    dst = resolve_run_dir(options.to_run_path)
    options.from_run_path = src
    options.to_run_path = dst
    merger = Merger(src, dst, options)
    report = merger.run()
    print_report(report, options)
    if report["errors"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
