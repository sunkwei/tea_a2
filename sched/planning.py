#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The planning core: a model splits a job into steps, a model puts the
results back together.

Two halves, and both are one model call:

  make_plan()  - the goal becomes a plan: a handful of steps, each one
                 marked read-only ("ro") or read-write ("rw");
  aggregate()  - the steps' results become one answer, and every conclusion
                 says which step it came from.

Why a module of its own: the queue (sched/task_server.py) and the extension
layer (sched/a2_ext_py/modules/toolkit_plan.py) both need exactly this. Two
copies of a prompt drift apart within a week, and then the queue's plan and
the extension's plan are not the same thing any more.

The mode of a step is not a label - it is a promise the plan keeps:

  "ro"  the step only reads. It runs under the read-only role, which the
        tool layer enforces (the file tools refuse to write, create, move or
        delete). Read-only steps may run next to each other.
  "rw"  the step changes something. Two of them must not run at the same
        time in the same workspace: a file that is being rewritten is not a
        file another step may read (the write is not atomic).

Standard library only. Python 3.6 and up.
"""

import json
import os
import re
import tempfile
import time
import uuid

# How many steps a plan may have. The model is told to stay below this; the
# queue has its own limit too (a plan with twenty steps is not a plan, it is
# a wish).
MAX_STEPS = 6

# How many steps may run at the same time. The same number as the queue's
# --plan-max-concurrent.
MAX_SUBTASKS = 4

MODE_RO = "ro"
MODE_RW = "rw"
MODES = (MODE_RO, MODE_RW)

# The role a mode runs under. The read-only role is a workspace role file
# (see READ_ONLY_ROLE_YAML below): read_only: true and a tool list without
# toolkit_exec, toolkit_memory and toolkit_agent_doc. A read-write step runs
# under no role at all - the workspace's own rules apply.
ROLE_FOR_MODE = {MODE_RO: "readonly", MODE_RW: ""}

# The read-only role, as the workspace wants it (roles/<id>.yaml). The
# extension layer installs it into the workspace before the first read-only
# step starts; the queue has its own copy of the same idea.
#
# The tool list is what makes the promise true: no toolkit_exec (a command
# can write anything), no toolkit_memory and no toolkit_agent_doc (they write
# state that the other steps share).
READ_ONLY_ROLE_YAML = """name: 只读调研
description: 只查不改：搜网络、读日志、搜源码、读文档，产出结论。
system_prompt: |
  你在做一个只读调研的子任务。你的产出就是结论本身。
  你可以读文件、在工作区里搜东西、查这个工作区以前的结论、查外部资料。
  你不能改任何东西：文件工具的写入会被工具层拒绝，跑命令的工具不在你的清单里。
  回答里写清楚你的结论和依据（哪个文件、哪一行、哪个网址）。
checklist: |
  工具/能力（a2 里真实存在的工具）：
  - toolkit_file：读文件、列目录（写/建/删/移会被拒绝）
  - toolkit_workspace_search：在工作区里按字面量或正则找东西
  - toolkit_history_search：找这个工作区以前的讨论与结论
  - toolkit_search / toolkit_fetch：查外部资料（provider 声明了才有）
  - toolkit_load_image：看图
  - toolkit_todo：把「待确认问题」列成这一轮的清单
  - toolkit_task：这一轮是在做某个子任务时，汇报进度
  行为限制：
  - 不改任何文件，不跑会改东西的命令
  - 结论要么能追溯到你看到的材料，要么明确标注为假设
  - 没找到就说没找到，不要编
tools:
  - toolkit_file
  - toolkit_workspace_search
  - toolkit_history_search
  - toolkit_search
  - toolkit_fetch
  - toolkit_load_image
  - toolkit_todo
  - toolkit_task
read_only: true
"""

PLANNER_SYSTEM = (
    "你在把一个复杂任务拆成可以并行做的子任务。\n"
    "要求：\n"
    "1. 每个子任务要能独立做完，prompt 要写清楚（做什么、在哪儿做、做成什么样），"
    "不要依赖对话里的上下文；\n"
    "2. 每个子任务标 mode：\n"
    "   - \"ro\" = 只查不改（搜网络、读日志、搜源码、读文档）；\n"
    "   - \"rw\" = 要改文件、或跑会改东西的命令；\n"
    "   没把握就标 \"rw\"（保守：只读的活儿当成会写，最多是白跑一次；"
    "会写的活儿标成只读，则会写不成）；\n"
    "3. \"ro\" 的子任务可以放心并行；\"rw\" 的子任务一次只能跑一个，"
    "所以要改东西的子任务最好只留一个；\n"
    "4. 要改文件的子任务写 write_paths（相对工作区的目录）；只读的不要写；\n"
    "5. 子任务不超过 %d 个，宁少勿多；\n"
    "6. 互相依赖的用 depends_on 写对方的 key（key 是那一步的名字）。\n"
    "只输出一个 JSON 对象，不要解释，不要 markdown 代码块：\n"
    '{"title": "...", "goal": "...", "aggregate_prompt": "...", '
    '"steps": [{"key": "s1", "title": "...", "prompt": "...", "mode": "ro", '
    '"write_paths": [], "depends_on": []}]}' % MAX_STEPS)

AGGREGATE_SYSTEM = (
    "你在给一个复杂任务做最终汇总。下面是这个任务的目标和每个子任务的结果。\n"
    "请合成一份完整、可以直接交付的结果：\n"
    "1. 不要罗列步骤，要给出结论性的答案；\n"
    "2. 每条结论都要标明它来自哪个子任务（用子任务的名字）；\n"
    "3. 子任务之间有矛盾就指出来；\n"
    "4. 有子任务失败或者没跑起来，就写明是哪个、为什么，不要假装它完成了；\n"
    "5. 缺什么就说明缺什么。\n"
    "只输出汇总本身，不要解释，不要 markdown 代码块。")


class PlanError(Exception):
    """The plan is not usable. The reason says which rule it broke."""


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def extract_json(text):
    """The plan out of the model's answer.

    The model is asked for JSON only, but models like to talk: the first
    object in the text is the plan.
    """
    content = (text or "").strip()
    if not content:
        return None
    if content.startswith("```"):
        content = content.strip("`")
        if content.lower().startswith("json"):
            content = content[4:]
    start = content.find("{")
    end = content.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        loaded = json.loads(content[start:end + 1])
    except ValueError:
        return None
    return loaded if isinstance(loaded, dict) else None


def _topological_order(steps):
    """The steps in the order they may run, or None when there is a circle."""
    by_key = dict((step["key"], step) for step in steps)
    state = {}          # key -> 0 (visiting) / 1 (done)
    order = []

    def visit(key):
        if state.get(key) == 1:
            return True
        if state.get(key) == 0:
            return False
        state[key] = 0
        for dep in by_key[key].get("depends_on") or []:
            if dep in by_key and not visit(dep):
                return False
        state[key] = 1
        order.append(key)
        return True

    for step in steps:
        if not visit(step["key"]):
            return None
    return order


def check_modes(steps):
    """Check and fill in the mode of every step.

    The rules live here and not in the two callers (the queue and the
    extension layer), because a plan that means one thing in one place and
    another thing in the other is worse than no plan at all:

      * a step is "ro" (it only reads) or "rw" (it changes something). A
        step without a mode is "rw" - the safe answer;
      * a read-only step may not carry write paths: "read only" and "write
        here" in the same step is a contradiction, and it is caught here
        instead of halfway through the work;
      * a write path is a relative path inside the workspace;
      * two read-write steps may not write the same place - they would
        fight over the same files.

    The steps are changed in place and returned.
    """
    writers = {}
    for index, step in enumerate(steps, 1):
        mode = str(step.get("mode") or "").strip().lower()
        if not mode:
            mode = MODE_RW
        if mode not in MODES:
            raise PlanError("第 %d 个子任务的 mode 只能是 %s（写的是 %r）"
                            % (index, " 或 ".join(MODES), step.get("mode")))
        raw_paths = step.get("write_paths")
        if isinstance(raw_paths, str):
            raw_paths = [raw_paths]
        if not isinstance(raw_paths, (list, tuple)):
            raw_paths = []
        write_paths = []
        for one in raw_paths:
            one = str(one or "").strip().replace("\\", "/").strip("/")
            if not one:
                continue
            if one.startswith("..") or one.startswith("/"):
                raise PlanError("第 %d 个子任务的 write_paths 必须是工作区里的相对路径：%s"
                                % (index, one))
            if one not in write_paths:
                write_paths.append(one)
        if mode == MODE_RO and write_paths:
            raise PlanError("第 %d 个子任务标了只读（ro），却又写了 write_paths：%s"
                            % (index, "、".join(write_paths)))
        if mode == MODE_RW:
            key = str(step.get("key") or "?")
            for path in write_paths:
                other = writers.get(path)
                if other:
                    raise PlanError("子任务「%s」和「%s」都要写 %s：它们会互相覆盖"
                                    % (other, key, path))
                writers[path] = key
        step["mode"] = mode
        step["write_paths"] = write_paths
    return steps


def normalize_steps(steps, max_steps=MAX_STEPS):
    """The steps of a plan, checked and normalized.

    Every step needs something to do and a mode; the dependencies must exist
    and must not make a circle. Two rules are about the model telling the
    truth:

      * a step marked "ro" may not carry write paths - "read only" and
        "write here" in the same step is a contradiction, and it is caught
        here instead of halfway through the work;
      * two "rw" steps may not write the same directory - they would fight
        over the same files.

    Returns a list of steps like this:
        {"key": "s1", "title": "...", "prompt": "...", "mode": "ro",
         "write_paths": [], "depends_on": []}
    """
    if not isinstance(steps, (list, tuple)) or not steps:
        raise PlanError("一个计划至少要有一个子任务（steps）")
    if len(steps) > max_steps:
        raise PlanError("子任务太多了（%d > %d）：拆细了反而做不好" % (len(steps), max_steps))
    out = []
    seen = {}
    for index, raw in enumerate(steps, 1):
        if not isinstance(raw, dict):
            raise PlanError("第 %d 个子任务不是一个对象" % index)
        prompt = str(raw.get("prompt") or "").strip()
        if not prompt:
            raise PlanError("第 %d 个子任务没有内容（prompt 是空的）" % index)
        title = str(raw.get("title") or "").strip() or prompt.splitlines()[0][:60]
        key = str(raw.get("key") or "").strip() or ("s%d" % index)
        if key in seen:
            raise PlanError("两个子任务用了同一个 key：%s" % key)
        seen[key] = index
        depends = raw.get("depends_on")
        if isinstance(depends, str):
            depends = [depends]
        if not isinstance(depends, (list, tuple)):
            depends = []
        out.append({
            "key": key,
            "title": title[:120],
            "prompt": prompt[:8000],
            # The mode and the write paths are checked by check_modes() below.
            "mode": raw.get("mode") or "",
            "write_paths": raw.get("write_paths") or [],
            "depends_on": [str(d).strip() for d in depends if str(d).strip()],
        })
    # The dependencies must exist...
    for step in out:
        for dep in step["depends_on"]:
            if dep not in seen:
                raise PlanError("子任务「%s」等的是不存在的子任务：%s" % (step["key"], dep))
            if dep == step["key"]:
                raise PlanError("子任务「%s」等的是它自己" % step["key"])
    # ...and must not make a circle.
    if _topological_order(out) is None:
        raise PlanError("这些子任务互相对着等，永远不会开始")
    # The mode of every step, checked by the shared rules.
    return check_modes(out)


def make_plan(client, goal, title="", max_steps=MAX_STEPS, timeout=180,
              provider=None, model=None):
    """One model call: the goal becomes a plan.

    `client` is an a2_ext_py Client (or anything with the same
    `llm_chat(prompt=..., system=...)`). The answer is a plan dict:
    {"title", "goal", "aggregate_prompt", "steps": [...]}.
    """
    goal = (goal or "").strip()
    if not goal:
        raise PlanError("得说清楚要做什么")
    prompt = "任务：\n%s\n" % goal
    if title:
        prompt += "\n这件事的名字：%s\n" % title
    prompt += "\n拆成不超过 %d 个子任务。只输出 JSON。" % max_steps
    answer = client.llm_chat(prompt=prompt, system=PLANNER_SYSTEM, temperature=0.2,
                             max_tokens=2000, stream=False, provider=provider,
                             model=model, timeout=timeout)
    answer = answer if isinstance(answer, dict) else {}
    content = answer.get("content") or answer.get("reasoning") or ""
    plan = extract_json(content)
    if plan is None:
        raise PlanError("模型没有给出计划（它说的是）：\n%s" % str(content)[:600])
    plan.setdefault("title", title or goal.splitlines()[0][:60])
    plan.setdefault("goal", goal)
    plan["aggregate_prompt"] = str(plan.get("aggregate_prompt") or "").strip()
    plan["steps"] = normalize_steps(plan.get("steps"), max_steps=max_steps)
    return plan


def aggregate_prompt(plan, results):
    """What the model is asked to turn the steps' results into one answer."""
    parts = []
    for step in plan.get("steps") or []:
        text = (results or {}).get(step.get("key")) or ""
        title = step.get("title") or step.get("key") or ""
        parts.append("【%s】\n%s" % (title, text.strip() if isinstance(text, str) else str(text)
                                     or "（这个子任务没有留下结果）"))
    goal = plan.get("goal") or plan.get("title") or ""
    ask = plan.get("aggregate_prompt") or "把这些结果汇总成一份完整、可以直接交付的结果。"
    # The wording is the queue's own (it is what the queue's callers and
    # its tests know): the same prompt on both sides of the fork.
    return ("【任务目标】\n%s\n\n【要做的汇总】\n%s\n\n【各步骤的结果】\n%s"
            % (goal, ask, "\n\n".join(parts)))


def aggregate(client, plan, results, timeout=180, provider=None, model=None):
    """One model call: the steps' results become one answer."""
    prompt = aggregate_prompt(plan, results)
    answer = client.llm_chat(prompt=prompt, system=AGGREGATE_SYSTEM, temperature=0.1,
                             max_tokens=4000, stream=False, provider=provider,
                             model=model, timeout=timeout)
    answer = answer if isinstance(answer, dict) else {}
    return str(answer.get("content") or "").strip()


def role_for(mode):
    """The role a step of this mode runs under ("" = no role)."""
    return ROLE_FOR_MODE.get(str(mode or "").strip().lower(), "")


def plan_dir():
    """Where the plans live: <tea home>/plans.

    Not in the workspace: the plan is the orchestrator's bookkeeping, and
    the model's file tools must not be able to edit it.
    """
    home = os.environ.get("TEA_HOME") or os.path.join("~", ".tea_agent")
    return os.path.join(os.path.expanduser(home), "plans")


def new_plan_id():
    return "plan_" + uuid.uuid4().hex[:12]


def save_plan(plan):
    """Write the plan down (atomically).

    A plan nobody wrote down is a plan nobody can clean up after: the
    subtask handles in it are the only way to stop a run that was left
    behind.
    """
    directory = plan_dir()
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, str(plan.get("id") or new_plan_id()) + ".json")
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".plan-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(plan, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def load_plan(plan_id):
    """The plan with this id, or None."""
    if not plan_id:
        return None
    path = os.path.join(plan_dir(), str(plan_id) + ".json")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def list_plans():
    """Every plan on disk, newest first."""
    directory = plan_dir()
    out = []
    try:
        names = os.listdir(directory)
    except OSError:
        return out
    for name in names:
        if not name.endswith(".json") or name.startswith("."):
            continue
        plan = load_plan(name[:-5])
        if plan:
            out.append(plan)
    out.sort(key=lambda p: str(p.get("created_at") or ""), reverse=True)
    return out


def read_only_role_name():
    """The id of the read-only role."""
    return "readonly"


def read_only_role_yaml():
    """The read-only role, as the workspace wants it."""
    return READ_ONLY_ROLE_YAML


def summarize(plan):
    """A short, human-readable summary of a plan (for the tool's answer)."""
    lines = []
    for step in plan.get("steps") or []:
        waits = ("（等 %s）" % "、".join(step.get("depends_on") or [])) if step.get("depends_on") else ""
        lines.append("  - %-6s [%s] %s%s" % (step.get("key"), step.get("mode"),
                                             step.get("title") or "", waits))
    return "\n".join(lines)


if __name__ == "__main__":
    # A tiny smoke of the checker: the rules are the interesting part, and
    # they are cheap to run.
    good = [{"key": "s1", "title": "查", "prompt": "找找", "mode": "ro"},
            {"key": "s2", "title": "改", "prompt": "改改", "mode": "rw",
             "write_paths": ["src/"]}]
    print(normalize_steps(good))
    for bad in ([{"key": "s1", "prompt": "x", "mode": "ro", "write_paths": ["a/"]}],
                [{"key": "s1", "prompt": "x", "mode": "rw", "write_paths": ["a/"]},
                 {"key": "s2", "prompt": "y", "mode": "rw", "write_paths": ["a/"]}],
                [{"key": "s1", "prompt": "x", "mode": "ro", "depends_on": ["s2"]},
                 {"key": "s2", "prompt": "y", "mode": "ro", "depends_on": ["s1"]}]):
        try:
            normalize_steps(bad)
            print("NOT CAUGHT:", bad)
        except PlanError as exc:
            print("caught:", exc)
