#!/usr/bin/env python3
"""The trigger entry: one door for every outside event.

Everything that happens outside the agent - a webhook, a CI result, a mail,
a message in a chat - arrives as a **trigger**: one event with a source, a
text and a key that says "this is the same event as last time".

The queue turns a trigger into work, but not always: a trigger may be a
greeting, an advertisement or a notification that needs nobody. So the
decision goes through three steps, in this order, because the order is
about money and about who has the last word:

  1. **Dedup.** The same key means the same event: a webhook that retries
     (or a person who taps send twice) must not queue the work twice. The
     trigger that came first is simply returned again.
  2. **The rules.** The operator's own words: "ignore greetings", "always
     take a failed build". Cheap, and it beats the model.
  3. **The model.** Only when the rules did not settle it: one model call
     that answers "is this worth doing?". The judge is a callback, so this
     module does not need to know how a model is called.

Only an accepted trigger becomes a task. Everything else is written down
and can be looked at later - "what did it decide, and why" is the question
an operator asks the day after.

The thread is the other thing that comes out of here: triggers that belong
to the same conversation (the same chat, the same build pipeline) end up in
the same thread, so the context of one is available to the next.

Standard library only: this runs on the device next to the agent, where
nothing is installed.
"""

import hashlib
import json
import os
import re
import time

# The vocabulary of a decision. It is the model's own wording ("accept" /
# "ignore"), so the answer can be taken as it is.
ACCEPT = "accept"
IGNORE = "ignore"
PENDING = "pending"

DEFAULT_RULES_PATH = os.path.expanduser(os.path.join(
    "~", ".tea_agent", "triggers.conf"))

# The rules file is small and its shape is fixed, so it is parsed here
# instead of pulling in a yaml library (there is none on the device).
#
#   [wechat]
#   require = 修复, 报错
#   ignore  = 早上好, 谢谢
#
# A pattern is a substring (case insensitive) unless it is written as
# /regex/flags.


class TriggerError(Exception):
    """The trigger cannot be used as it is (missing fields, and the like)."""


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def split_list(value):
    """The comma separated list in a rules file (Chinese commas too)."""
    out = []
    for piece in re.split(r"[,，]", value or ""):
        piece = piece.strip()
        if piece:
            out.append(piece)
    return out


def parse_rules(text):
    """The rules file -> {source: {"require": [...], "ignore": [...]}}."""
    out = {}
    source = ""
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            source = line[1:-1].strip()
            out.setdefault(source, {"require": [], "ignore": []})
            continue
        key, sep, value = line.partition("=")
        if not sep:
            key, sep, value = line.partition(":")
        if not sep:
            continue
        key = key.strip().lower()
        if key not in ("require", "ignore"):
            continue
        bucket = out.setdefault(source, {"require": [], "ignore": []})
        bucket[key].extend(split_list(value))
    return out


def match(pattern, haystack):
    """Does the pattern match? A substring, or a /regex/."""
    text = (pattern or "").strip()
    if not text:
        return False
    if text.startswith("/"):
        end = text.rfind("/")
        if end > 0:
            body = text[1:end]
            flags = text[end + 1:].lower()
            if "i" in flags:
                body = "(?i)" + body
            try:
                return re.search(body, haystack) is not None
            except re.error:
                return False
    return text.lower() in haystack


class Rules(object):
    """The operator's own words about what is worth doing.

    The file is read again whenever it changed, so the rules can be tuned
    without restarting the queue.
    """

    def __init__(self, path=None):
        self.path = path or DEFAULT_RULES_PATH
        self.sources = {}
        self.mtime = 0.0
        self.load()

    def load(self):
        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            self.sources = {}
            self.mtime = 0.0
            return
        if mtime == self.mtime:
            return                      # unchanged: do not read it again
        try:
            with open(self.path, encoding="utf-8") as f:
                text = f.read()
        except OSError:
            return
        self.mtime = mtime
        self.sources = parse_rules(text)

    def decide(self, source, text):
        """("accept"|"ignore"|None, reason).

        None means "the rules do not say" - the caller has to ask somebody
        else (the model).
        """
        self.load()
        rules = self.sources.get(source) or {}
        haystack = (text or "").lower()
        # "ignore" wins over "require": a rule that says "this is not work"
        # is a deliberate decision, and the cost of dropping a greeting is
        # nothing while the cost of working on one is a task.
        for pattern in rules.get("ignore") or []:
            if match(pattern, haystack):
                return IGNORE, "命中忽略规则：%s" % pattern
        for pattern in rules.get("require") or []:
            if match(pattern, haystack):
                return ACCEPT, "命中受理规则：%s" % pattern
        return None, ""

    def describe(self):
        """The rules as they are now (for the API and the log)."""
        self.load()
        return {source: dict(rules) for source, rules in self.sources.items()}


def default_dedup_key(source, text):
    """The key of a trigger that did not bring one.

    The same text from the same source is the same event. A caller that
    wants two different tasks from the same text (the same words twice)
    brings its own key.
    """
    digest = hashlib.sha1((text or "").encode("utf-8")).hexdigest()[:12]
    return "%s:%s" % (source or "trigger", digest)


def thread_key_of(dedup_key, source):
    """The thread a trigger belongs to: the conversation it is part of.

    "wechat:u42:1730000000" -> "wechat:u42": the source and the person, not
    the moment.
    """
    key = (dedup_key or "").strip()
    if ":" in key:
        head = key.rsplit(":", 1)[0]
        if head:
            return head
    return source or "trigger"


class TriggerStore(object):
    """The trigger and thread records.

    They live in their own directories under the queue's data directory,
    not among the task records: the task list reads every *.json there, and
    a trigger is not a task.
    """

    def __init__(self, root):
        self.root = os.path.abspath(os.path.expanduser(root))
        self.triggers_dir = os.path.join(self.root, "triggers")
        self.threads_dir = os.path.join(self.root, "threads")
        for path in (self.triggers_dir, self.threads_dir):
            try:
                os.makedirs(path, exist_ok=True)
            except OSError:
                pass

    # ---- the files ------------------------------------------------------
    def _trigger_path(self, trigger_id):
        return os.path.join(self.triggers_dir, trigger_id + ".json")

    def _thread_path(self, thread_id):
        return os.path.join(self.threads_dir, thread_id + ".json")

    @staticmethod
    def _write(path, record):
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(record, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
            return True
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            return False

    @staticmethod
    def _read(path):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    # ---- triggers -------------------------------------------------------
    def new_trigger_id(self):
        # The time is in front, so the files sort in the order they arrived.
        return "tr_%013x%s" % (int(time.time() * 1000), os.urandom(3).hex())

    def save_trigger(self, trigger):
        self._write(self._trigger_path(trigger.get("id") or ""), trigger)
        return trigger

    def load_trigger(self, trigger_id):
        if not trigger_id:
            return None
        return self._read(self._trigger_path(trigger_id))

    def find_by_dedup(self, dedup_key):
        """The trigger with this key, if it came before."""
        if not dedup_key:
            return None
        try:
            names = sorted(os.listdir(self.triggers_dir))
        except OSError:
            return None
        for name in names:
            if not name.endswith(".json"):
                continue
            trigger = self._read(os.path.join(self.triggers_dir, name))
            if trigger and trigger.get("dedup_key") == dedup_key:
                return trigger
        return None

    def list_triggers(self, source="", decision="", workspace_id="", limit=100):
        try:
            names = sorted(os.listdir(self.triggers_dir), reverse=True)
        except OSError:
            return []
        out = []
        for name in names:
            if not name.endswith(".json"):
                continue
            trigger = self._read(os.path.join(self.triggers_dir, name))
            if not trigger:
                continue
            if source and trigger.get("source") != source:
                continue
            if decision and trigger.get("decision") != decision:
                continue
            if workspace_id and trigger.get("workspace_id") != workspace_id:
                continue
            out.append(trigger)
            if len(out) >= limit:
                break
        return out

    # ---- threads --------------------------------------------------------
    def thread_id(self, workspace_id, key):
        """The same conversation always gets the same thread."""
        digest = hashlib.sha1(
            ("%s|%s" % (workspace_id or "", key or "")).encode("utf-8")).hexdigest()
        return "th_" + digest[:10]

    def load_thread(self, thread_id):
        if not thread_id:
            return None
        return self._read(self._thread_path(thread_id))

    def attach(self, thread_id, workspace_id, key, task_id="", trigger_id=""):
        """Put a task (and its trigger) into a thread."""
        thread = self.load_thread(thread_id) or {}
        if not thread:
            thread = {"id": thread_id, "key": key or "", "title": key or "",
                      "workspace_id": workspace_id or "",
                      "task_ids": [], "trigger_ids": [],
                      "created_at": now_iso()}
        if task_id and task_id not in thread["task_ids"]:
            thread["task_ids"].append(task_id)
        if trigger_id and trigger_id not in thread["trigger_ids"]:
            thread["trigger_ids"].append(trigger_id)
        thread["updated_at"] = now_iso()
        thread["workspace_id"] = thread.get("workspace_id") or (workspace_id or "")
        thread["key"] = thread.get("key") or (key or "")
        self._write(self._thread_path(thread_id), thread)
        return thread

    def list_threads(self, workspace_id="", limit=100):
        try:
            names = sorted(os.listdir(self.threads_dir), reverse=True)
        except OSError:
            return []
        out = []
        for name in names:
            if not name.endswith(".json"):
                continue
            thread = self._read(os.path.join(self.threads_dir, name))
            if not thread:
                continue
            if workspace_id and thread.get("workspace_id") != workspace_id:
                continue
            out.append(thread)
            if len(out) >= limit:
                break
        return out


def normalize_decision(value):
    """One spelling of a decision: the model may say accept or accepted."""
    word = str(value or "").strip().lower()
    if word in ("accept", "accepted", "yes", "do", "true"):
        return ACCEPT
    if word in ("ignore", "ignored", "no", "skip", "false"):
        return IGNORE
    return word


def parse_decision(text):
    """What the model answered: (decision, reason).

    As forgiving as possible: the model may answer with JSON, with a word,
    or with a sentence that starts with the word. What must not happen is a
    decision that is not there - then the caller decides (and says so).
    """
    content = (text or "").strip()
    if not content:
        return "", ""
    low = content.lower()
    decision = ""
    # The explicit form first: {"decision": "accept", "reason": "..."}
    found = re.search(r'"decision"\s*:\s*"(accept|ignore)"', low)
    if found:
        decision = found.group(1)
    else:
        head = low[:120]
        if "ignore" in head:
            decision = IGNORE
        elif "accept" in head:
            decision = ACCEPT
    reason = content
    try:
        data = json.loads(content)
        if isinstance(data, dict):
            decision = normalize_decision(data.get("decision") or decision)
            reason = str(data.get("reason") or content).strip()
    except ValueError:
        # Not JSON: the answer is the reason, with the decision word in it.
        reason = re.sub(r"\b(accept|ignore)\b", "", content, flags=re.IGNORECASE).strip(" ：:，,.")
        if not reason:
            reason = content
    decision = normalize_decision(decision)
    if decision not in (ACCEPT, IGNORE):
        decision = ""
    return decision, reason[:300]


class TriggerService(object):
    """Submit an outside event and find out whether it becomes work."""

    def __init__(self, store, rules, judge=None, make_task=None):
        self.store = store
        self.rules = rules
        self.judge = judge
        self.make_task = make_task

    def submit(self, body):
        """(trigger, duplicate).

        `duplicate` is True when this key was seen before: the trigger that
        came first is returned and nothing is queued a second time.
        """
        if not isinstance(body, dict):
            raise TriggerError("the body is not a JSON object")
        source = str(body.get("source") or "").strip()
        if not source:
            raise TriggerError("'source' is required (wechat, mail, webhook, ...)")
        text = str(body.get("text") or body.get("prompt") or "").strip()
        if not text:
            raise TriggerError("'text' is required")
        workspace_id = str(body.get("workspace_id") or "").strip()
        dedup_key = str(body.get("dedup_key") or "").strip()
        if not dedup_key:
            dedup_key = default_dedup_key(source, text)
        existing = self.store.find_by_dedup(dedup_key)
        if existing is not None:
            return existing, True

        thread_key = str(body.get("thread_key") or "").strip()
        if not thread_key:
            thread_key = thread_key_of(dedup_key, source)
        trigger = {
            "id": self.store.new_trigger_id(),
            "source": source,
            "dedup_key": dedup_key,
            "text": text[:4000],
            "workspace_id": workspace_id,
            "thread_key": thread_key,
            "thread_id": self.store.thread_id(workspace_id, thread_key),
            "decision": PENDING,
            "reason": "",
            "task_id": "",
            "created_at": now_iso(),
            "updated_at": now_iso(),
        }
        self.store.save_trigger(trigger)

        decision, reason = self.rules.decide(source, text)
        if decision is None and self.judge is not None:
            decision, reason = self.judge(trigger)
        if decision not in (ACCEPT, IGNORE):
            # No answer at all: keep the work rather than lose it. A task
            # that should not have been queued can be cancelled, and the
            # reason says why it was queued anyway.
            decision = ACCEPT
            reason = reason or "没有人来判断这件事，先收下（不想要就取消掉）"
        trigger["decision"] = decision
        trigger["reason"] = reason
        trigger["updated_at"] = now_iso()

        if decision == ACCEPT and self.make_task is not None:
            task = self.make_task(trigger, body)
            if task:
                trigger["task_id"] = task.get("id") or ""
                if task.get("thread_id"):
                    trigger["thread_id"] = task["thread_id"]
        self.store.save_trigger(trigger)
        self.store.attach(trigger["thread_id"], trigger["workspace_id"],
                          trigger["thread_key"], trigger["task_id"], trigger["id"])
        return trigger, False
