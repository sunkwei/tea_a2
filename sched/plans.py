#!/usr/bin/env python3
"""The plans: one complex job, split into steps that may wait for each other.

A plan is two things:

  * a **record** - a file under <data dir>/plans/: the goal, the steps, which
    task every step became, how far the plan is;
  * the **tasks** - one parent (the container: it holds the final result) and
    one child per step. The children may depend on each other: that is what
    makes it a plan and not a list.

Nothing here talks to the network. The plan is validated, stored and read
here; the queue (sched/task_server.py) does the rest: it creates the tasks,
starts a child when what it waits for is done, and writes the summary into
the parent when they are all done.

The vocabulary:

    step       one piece of the work ("把 12 个文件分类")
    key        the step's name inside the plan ("classify")
    depends_on the keys of the steps that must be done first
    parent     the task that holds the final result
    child      the task one step became
"""

import json
import os
import tempfile
import threading
import time

MAX_STEPS = 32
MAX_PROMPT = 4000


class PlanError(Exception):
    """The plan cannot be made: the message says what is wrong."""


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def normalize_steps(steps, max_steps=MAX_STEPS):
    """The steps of a plan, checked and normalized.

    Every step needs something to do; every step may name the steps it waits
    for; and the waiting must not make a circle - a plan whose steps wait for
    each other for ever is a plan that never runs.

    Returns a list of steps like this:
        {"key": "classify", "title": "...", "prompt": "...",
         "depends_on": ["read"], "workspace_id": "", "provider": ""}
    """
    if not isinstance(steps, (list, tuple)) or not steps:
        raise PlanError("一个计划至少要有一个步骤（steps）")
    if len(steps) > max_steps:
        raise PlanError("一个计划的步骤太多了（%d > %d）：拆细了反而做不好"
                        % (len(steps), max_steps))
    out = []
    seen = {}
    for index, raw in enumerate(steps, 1):
        if not isinstance(raw, dict):
            raise PlanError("第 %d 步不是一个对象" % index)
        prompt = str(raw.get("prompt") or "").strip()
        if not prompt:
            raise PlanError("第 %d 步没有内容（prompt 是空的）" % index)
        title = str(raw.get("title") or "").strip() or prompt.splitlines()[0][:60]
        key = str(raw.get("key") or "").strip() or ("s%d" % index)
        if key in seen:
            raise PlanError("两步用了同一个 key：%s" % key)
        seen[key] = index
        depends = raw.get("depends_on")
        if isinstance(depends, str):
            depends = [depends]
        if not isinstance(depends, (list, tuple)):
            depends = []
        out.append({
            "key": key,
            "title": title[:120],
            "prompt": prompt[:MAX_PROMPT],
            "depends_on": [str(d).strip() for d in depends if str(d).strip()],
            "workspace_id": str(raw.get("workspace_id") or "").strip(),
            "provider": str(raw.get("provider") or "").strip(),
            "model": str(raw.get("model") or "").strip(),
        })
    # The dependencies must exist...
    for step in out:
        for dep in step["depends_on"]:
            if dep not in seen:
                raise PlanError("步骤「%s」等的是不存在的步骤：%s" % (step["key"], dep))
            if dep == step["key"]:
                raise PlanError("步骤「%s」等的是它自己" % step["key"])
    # ...and must not make a circle.
    if _topological_order(out) is None:
        raise PlanError("这些步骤互相对着等，永远不会开始")
    return out


def _topological_order(steps):
    """The steps in the order they may run, or None when there is a circle."""
    by_key = dict((step["key"], step) for step in steps)
    state = {}          # key -> 0 (visiting) / 1 (done)
    order = []

    def visit(key):
        if state.get(key) == 1:
            return True
        if state.get(key) == 0:
            return False       # a circle
        state[key] = 0
        for dep in by_key[key].get("depends_on") or []:
            if not visit(dep):
                return False
        state[key] = 1
        order.append(key)
        return True

    for step in steps:
        if not visit(step["key"]):
            return None
    return order


class PlanStore(object):
    """The plan records: one file per plan, in their own directory.

    They live next to the task records but not among them: the queue's task
    list reads every *.json of its data directory, and a plan is not a task.
    """

    def __init__(self, root):
        self.root = os.path.abspath(os.path.expanduser(root))
        self.plans_dir = os.path.join(self.root, "plans")
        self.lock = threading.RLock()
        try:
            os.makedirs(self.plans_dir, exist_ok=True)
        except OSError:
            pass

    def _path(self, plan_id):
        return os.path.join(self.plans_dir, plan_id + ".json")

    def new_plan_id(self):
        return "pl_" + os.urandom(6).hex()

    def save(self, plan):
        with self.lock:
            try:
                os.makedirs(self.plans_dir, exist_ok=True)
                plan["updated_at"] = now_iso()
                fd, tmp = tempfile.mkstemp(dir=self.plans_dir, prefix=".plan-")
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(plan, f, ensure_ascii=False, indent=2)
                os.replace(tmp, self._path(plan.get("id") or ""))
            except OSError:
                pass
        return plan

    def load(self, plan_id):
        if not plan_id or not isinstance(plan_id, str):
            return None
        try:
            with open(self._path(plan_id), encoding="utf-8") as f:
                loaded = json.load(f)
        except (OSError, ValueError):
            return None
        if not isinstance(loaded, dict) or not loaded.get("id"):
            return None
        return loaded

    def list_plans(self, limit=100):
        """The plans, newest first.

        The ids are random, so the order cannot come from the file names:
        it comes from when the plan was made.
        """
        out = []
        try:
            names = os.listdir(self.plans_dir)
        except OSError:
            return []
        for name in names:
            if not name.endswith(".json"):
                continue
            plan = self.load(name[:-5])
            if plan:
                out.append(plan)
        out.sort(key=lambda p: (p.get("created_at") or "", p.get("id") or ""), reverse=True)
        return out[:limit] if limit else out

    def find_by_key(self, key):
        """The plan a repeated submission with the same key must return."""
        if not key:
            return None
        for plan in self.list_plans(limit=200):
            if plan.get("idempotency_key") == key:
                return plan
        return None
