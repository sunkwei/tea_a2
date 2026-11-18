"""toolkit_plan - split a job into subtasks that run in parallel, and put
the results back together.

One tool, two operations:

  op="start"   the model splits the job, the subtasks start, and the answer
               comes back right away with a plan id
  op="status"  how far along is it, and what did the subtasks find

Why two operations and not one long call: an MCP tool call cannot be
cancelled by the agent. The kernel waits for the timeout, gives up on the
answer - and the subtasks keep running, with nobody holding their ids. A
call that returns right away leaves nothing running that the plan does not
know about: every subtask has a handle, a budget and a deadline in the plan
file, and a reaper stops what was left behind.

The plan keeps the promise the model makes. Every step is marked:

  "ro"  the step only reads. It runs under the read-only role: the file
        tools refuse to write, there is no command tool, and read-only
        steps run next to each other.
  "rw"  the step changes something. It runs alone: a file that is being
        rewritten is not a file another step may read.

The gates - the model cannot spend what it likes:

  * one plan per workspace per minute;
  * at most four steps;
  * every step gets a budget, and the wall clock is mandatory, so a step
    that was left behind dies by itself;
  * a read-only step cannot change anything (the tool layer enforces it,
    not a line in the prompt).

Install it like any other extension module:

    cp toolkit_plan.py ~/.tea_agent/ext/
    # and tell it where the agent is:
    #   env: {TEA_AGENT_URL: "http://127.0.0.1:8282"} in ~/.tea_agent/mcp.yaml
"""

import calendar
import json
import os
import os.path as osp
import sys
import threading
import time

# --- where the shared code lives ---------------------------------------------
# The module is copied into ~/.tea_agent/ext/, but the planning core and the
# client live in the source tree. The extension server puts <repo>/sched on
# sys.path when it starts; the candidates below cover the other ways this
# file can be loaded.
def _sched_dir():
    """Where the planning core may live.

    The module is copied into ~/.tea_agent/ext/, but the core lives in the
    source tree. The extension server puts <repo>/sched on sys.path when it
    starts; the candidates below cover the other ways this file is loaded
    (the loader runs it with a namespace of its own, so __file__ may not
    exist at all).
    """
    candidates = []
    env = os.environ.get("TEA_A2_SCHED")
    if env:
        candidates.append(env)
    anchors = []
    if sys.argv and sys.argv[0]:
        anchors.append(sys.argv[0])
    own = globals().get("__file__")
    if own:
        anchors.append(own)
    for anchor in anchors:
        # <repo>/sched/a2_ext_py/{toolkit,examples}/<file> -> <repo>/sched
        base = osp.dirname(osp.dirname(osp.dirname(osp.abspath(anchor))))
        candidates.append(base)
    for path in candidates:
        if path and osp.isfile(osp.join(path, "planning.py")):
            return path
    return None


_SCHED = _sched_dir()
if _SCHED and _SCHED not in sys.path:
    sys.path.insert(0, _SCHED)

try:
    import planning
    from a2_ext_py import Client
except ImportError as exc:  # the loader reports this as a broken module
    raise ImportError("the planning core (planning.py) was not found - set "
                      "TEA_A2_SCHED to the directory that holds it: %s" % exc)

DEFAULT_AGENT_URL = "http://127.0.0.1:8282"

# One plan per workspace per minute. The model asked for work; it does not
# get to ask for work every time it feels like it.
DEBOUNCE_SECONDS = 60

# The budget of one step. The wall clock is not optional: a step that was
# left behind has to die by itself.
DEFAULT_LIMITS = {
    "max_iterations": 30,
    "max_wall_ms": 300000,
    "max_tool_calls": 40,
}

POLL_SECONDS = 2.0
REAPER_SECONDS = 60.0
GRACE_SECONDS = 30.0

_LOCK = threading.Lock()          # guards _LAST_START and _PLANS
_PLAN_LOCK = threading.RLock()    # guards the plan files
_LAST_START = {}                  # workspace id -> timestamp
_PLANS = {}                       # plan id -> plan (the live copy)
_REAPER = None


# --- small helpers -----------------------------------------------------------
def _agent_base():
    return (os.environ.get("TEA_AGENT_URL") or os.environ.get("TEA_AGENT_BASE")
            or DEFAULT_AGENT_URL)


def _client():
    return Client(_agent_base())


def _now():
    return time.time()


def _stamp():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _plan_of(plan_id):
    """The live plan, or the one on disk."""
    with _LOCK:
        plan = _PLANS.get(plan_id)
    if plan:
        return plan
    return planning.load_plan(plan_id)


def _save(plan):
    with _PLAN_LOCK:
        planning.save_plan(plan)


def _slot(plan, key):
    return (plan.get("subtasks") or {}).get(key) or {}


def _set_slot(plan, key, **fields):
    with _PLAN_LOCK:
        slot = (plan.get("subtasks") or {}).get(key)
        if not slot:
            return
        slot.update(fields)
        planning.save_plan(plan)


# --- the gates ---------------------------------------------------------------
def _ensure_readonly_role(client, workspace_id):
    """Put the read-only role into the workspace (if it is not there yet).

    The role is the promise: read_only is enforced by the tools, and the
    tool list is what keeps the promise true.
    """
    name = planning.read_only_role_name()
    try:
        listing = client.roles(workspace_id) or {}
    except Exception as exc:  # noqa: BLE001
        return False, "读不到这个工作区的角色：%s" % exc
    for role in listing.get("roles") or []:
        if role.get("id") == name:
            return True, ""
    try:
        client.put_role(workspace_id, name, planning.read_only_role_yaml())
    except Exception as exc:  # noqa: BLE001
        return False, "装不上只读角色：%s" % exc
    return True, ""


# --- one step ----------------------------------------------------------------
def _run_subtask(plan_id, key):
    """Run one step to the end and write down what it found."""
    plan = _plan_of(plan_id)
    if not plan:
        return
    step = None
    for one in plan.get("steps") or []:
        if one.get("key") == key:
            step = one
            break
    if not step:
        return
    client = _client()
    limits = dict(plan.get("limits") or DEFAULT_LIMITS)
    role = planning.role_for(step.get("mode"))
    started = _now()
    try:
        handle = client.submit(plan.get("workspace_id") or "", step.get("prompt") or "",
                               role=role or None, limits=limits,
                               idempotency_key="plan:%s:%s" % (plan_id, key))
    except Exception as exc:  # noqa: BLE001
        _set_slot(plan, key, state="failed", error=str(exc),
                  finished_at=_stamp(), elapsed_ms=int((_now() - started) * 1000))
        return
    turn_id = (handle or {}).get("turn_id") or ""
    _set_slot(plan, key, state="running", turn_id=turn_id,
              session_id=(handle or {}).get("session_id") or "",
              started_at=_stamp())
    if not turn_id:
        _set_slot(plan, key, state="failed", error="这一轮没有拿到句柄")
        return

    deadline = started + (limits.get("max_wall_ms") or 300000) / 1000.0 + GRACE_SECONDS
    status = {}
    while True:
        try:
            status = client.turn_status(turn_id) or {}
        except Exception as exc:  # noqa: BLE001
            status = {"state": "failed", "error": str(exc)}
        state = str(status.get("state") or "")
        if state in ("done", "failed", "stopped", "budget", "interrupted"):
            break
        if _now() > deadline:
            # The step overran its budget: stop it and say so.
            try:
                client.stop_turn(turn_id)
            except Exception:  # noqa: BLE001
                pass
            status = {"state": "budget", "error": "超过时间上限，被收掉了"}
            break
        time.sleep(POLL_SECONDS)

    tools = []
    try:
        trajectory = client.turn_trajectory(turn_id) or {}
        for step_info in trajectory.get("steps") or []:
            if step_info.get("kind") != "tool":
                continue
            tools.append({"name": step_info.get("name") or "",
                          "ok": bool(step_info.get("ok")),
                          "elapsed_ms": step_info.get("elapsed_ms") or 0})
    except Exception:  # noqa: BLE001
        tools = []

    state = str(status.get("state") or "")
    result = str(status.get("result") or "")
    error = str(status.get("error") or "")
    if state == "done" and not result:
        result = ""
    _set_slot(plan, key,
              state=state or "failed",
              result=result,
              error=error,
              tools=tools,
              iterations=status.get("iterations") or 0,
              usage=status.get("usage") or {},
              finished_at=_stamp(),
              elapsed_ms=int((_now() - started) * 1000))


def _deps_done(step, finished):
    for dep in step.get("depends_on") or []:
        if dep not in finished:
            return False
    return True


def _run_plan(plan_id):
    """The scheduler: read-only steps side by side, write steps alone."""
    client = _client()
    plan = _plan_of(plan_id)
    if not plan:
        return
    steps = plan.get("steps") or []
    limits = plan.get("limits") or DEFAULT_LIMITS
    limit_ms = int(limits.get("max_wall_ms") or 300000)
    deadline = _now() + (limit_ms * max(1, len(steps))) / 1000.0 + GRACE_SECONDS

    running = {}
    finished = set()
    failed = set()
    while len(finished) + len(failed) < len(steps):
        if _now() > deadline:
            for key in list(running):
                _stop_subtask(client, plan, key)
            break
        rw_running = [k for k in running
                      if _mode_of(plan, k) == planning.MODE_RW]
        if not rw_running:
            for step in steps:
                key = step.get("key")
                if key in running or key in finished or key in failed:
                    continue
                if not _deps_done(step, finished):
                    continue
                if step.get("mode") == planning.MODE_RW:
                    # A write step runs alone: while it runs, nobody reads.
                    if running:
                        break
                    _launch(plan, key, running)
                    break
                if len(running) >= planning.MAX_SUBTASKS:
                    break
                _launch(plan, key, running)
        for key in list(running):
            slot = _slot(plan, key)
            state = str(slot.get("state") or "")
            if state in ("done", "failed", "stopped", "budget", "interrupted"):
                del running[key]
                if state == "done":
                    finished.add(key)
                else:
                    failed.add(key)
        if running:
            time.sleep(POLL_SECONDS)

    # Everything is done (or was stopped): the model puts it together.
    results = {}
    for step in steps:
        key = step.get("key")
        slot = _slot(plan, key)
        text = str(slot.get("result") or "")
        if not text and slot.get("error"):
            text = "（这个子任务没有完成：%s）" % slot.get("error")
        results[key] = text
    try:
        summary = planning.aggregate(client, plan, results)
    except Exception as exc:  # noqa: BLE001
        summary = ""
        _set_slot(plan, "__aggregate__", state="failed", error=str(exc))
    with _PLAN_LOCK:
        plan["status"] = "done"
        plan["result"] = summary
        plan["finished_at"] = _stamp()
        planning.save_plan(plan)


def _mode_of(plan, key):
    for step in plan.get("steps") or []:
        if step.get("key") == key:
            return step.get("mode")
    return planning.MODE_RW


def _launch(plan, key, running):
    thread = threading.Thread(target=_run_subtask, args=(plan.get("id"), key), daemon=True)
    thread.start()
    running[key] = thread


def _stop_subtask(client, plan, key):
    slot = _slot(plan, key)
    turn_id = str(slot.get("turn_id") or "")
    if not turn_id:
        return
    try:
        client.stop_turn(turn_id)
    except Exception:  # noqa: BLE001
        pass


# --- the reaper --------------------------------------------------------------
def _reaper_loop():
    """Stop the steps that were left behind.

    A step that overran its deadline is stopped here. The budget stops most
    of them on its own; this is the net for the ones the budget did not
    reach (the agent was restarted, the handle was lost).
    """
    while True:
        time.sleep(REAPER_SECONDS)
        try:
            _reap_once()
        except Exception:  # noqa: BLE001
            pass


def _reap_once():
    client = _client()
    now = _now()
    for plan in planning.list_plans():
        limits = plan.get("limits") or DEFAULT_LIMITS
        limit_ms = int(limits.get("max_wall_ms") or 300000)
        for key, slot in (plan.get("subtasks") or {}).items():
            if not isinstance(slot, dict):
                continue
            if str(slot.get("state") or "") != "running":
                continue
            started = str(slot.get("started_at") or "")
            if not started:
                continue
            try:
                begin = calendar.timegm(time.strptime(started, "%Y-%m-%dT%H:%M:%SZ"))
            except Exception:  # noqa: BLE001
                continue
            if now - begin <= limit_ms / 1000.0 + GRACE_SECONDS:
                continue
            turn_id = str(slot.get("turn_id") or "")
            if not turn_id:
                continue
            try:
                client.stop_turn(turn_id)
            except Exception:  # noqa: BLE001
                continue
            slot["state"] = "stopped"
            slot["error"] = "超时，被回收掉了"
            slot["finished_at"] = _stamp()
            try:
                planning.save_plan(plan)
            except Exception:  # noqa: BLE001
                pass


def _ensure_reaper():
    global _REAPER
    with _LOCK:
        if _REAPER is not None:
            return
        thread = threading.Thread(target=_reaper_loop, daemon=True)
        thread.start()
        _REAPER = thread


# --- the tool ----------------------------------------------------------------
def toolkit_plan(op="start", goal="", plan_id="", subtask="", workspace_id="",
                 title="", max_subtasks=planning.MAX_SUBTASKS):
    """Split a job into subtasks that run in parallel, and put the results
    back together.

    op="start": the model splits the goal into subtasks (each one marked
    read-only "ro" or read-write "rw"), the subtasks start, and the answer
    comes back right away with a plan id. The subtasks keep running in the
    background - ask for op="status" to see how they are doing.

    op="status": how far along the plan is, what the subtasks found, and
    (once they are done) the summary the model wrote. With subtask="s1" it
    answers about that one subtask only.

    Read-only subtasks run side by side; a read-write subtask runs alone.
    """
    op = (op or "start").strip().lower()
    if op in ("start", "run", "begin"):
        return _op_start(goal, workspace_id=workspace_id, title=title,
                          max_subtasks=max_subtasks)
    if op in ("status", "state", "check"):
        return _op_status(plan_id, subtask=subtask)
    return "op 只能是 start 或 status（写的是 %r）" % op


def _op_start(goal, workspace_id="", title="", max_subtasks=planning.MAX_SUBTASKS):
    goal = (goal or "").strip()
    if not goal:
        return "得说清楚要做什么（goal 是空的）。"
    try:
        max_subtasks = int(max_subtasks)
    except (TypeError, ValueError):
        max_subtasks = planning.MAX_SUBTASKS
    if max_subtasks < 1:
        max_subtasks = 1
    if max_subtasks > planning.MAX_SUBTASKS:
        max_subtasks = planning.MAX_SUBTASKS

    client = _client()
    if not workspace_id:
        try:
            workspaces = client.workspaces() or []
        except Exception as exc:  # noqa: BLE001
            return "连不上这台机器的 agent：%s" % exc
        if not workspaces:
            return "这台机器还没有工作区，编排不了。"
        workspace_id = workspaces[0].get("id") or ""

    with _LOCK:
        last = float(_LAST_START.get(workspace_id) or 0)
        wait = DEBOUNCE_SECONDS - (_now() - last)
        if last and wait > 0:
            return ("这个工作区刚刚起过一次编排（还有 %d 秒冷却）：一次只做一件事，"
                    "先把上一个做完。" % int(wait))
        # The cooldown starts now, not when the plan is done: two starts in a
        # row must not both get through.
        _LAST_START[workspace_id] = _now()

    try:
        plan = planning.make_plan(client, goal, title=title)
    except planning.PlanError as exc:
        return "模型给出的计划不能用：%s" % exc
    except Exception as exc:  # noqa: BLE001
        return "模型没有给出计划：%s" % exc

    steps = plan.get("steps") or []
    if len(steps) > max_subtasks:
        return ("计划有 %d 个子任务，超过上限 %d：请把目标说小一点，或者分两次做。"
                % (len(steps), max_subtasks))

    if any(step.get("mode") == planning.MODE_RO for step in steps):
        ok, why = _ensure_readonly_role(client, workspace_id)
        if not ok:
            return "只读角色装不上：%s" % why

    plan["id"] = planning.new_plan_id()
    plan["workspace_id"] = workspace_id
    plan["created_at"] = _stamp()
    plan["status"] = "running"
    plan["limits"] = dict(DEFAULT_LIMITS)
    plan["subtasks"] = {}
    for step in steps:
        plan["subtasks"][step["key"]] = {
            "key": step["key"],
            "title": step.get("title") or step["key"],
            "mode": step.get("mode"),
            "state": "pending",
            "turn_id": "",
            "session_id": "",
            "started_at": "",
            "finished_at": "",
            "elapsed_ms": 0,
            "result": "",
            "error": "",
            "tools": [],
        }
    planning.save_plan(plan)
    with _LOCK:
        _PLANS[plan["id"]] = plan

    thread = threading.Thread(target=_run_plan, args=(plan["id"],), daemon=True)
    thread.start()
    _ensure_reaper()

    lines = ["编排已经开始了（计划 %s）：" % plan["id"],
             planning.summarize(plan),
             "",
             "子任务在后台跑。用 toolkit_plan(op=\"status\", plan_id=\"%s\") 看进展；"
             "都跑完之后那里会有汇总。" % plan["id"]]
    return "\n".join(lines)


def _op_status(plan_id, subtask=""):
    plan_id = (plan_id or "").strip()
    if not plan_id:
        return "得给一个计划号（plan_id）。"
    plan = _plan_of(plan_id)
    if not plan:
        return "没有这个计划：%s" % plan_id
    subtask = (subtask or "").strip()
    if subtask:
        slot = _slot(plan, subtask)
        if not slot:
            keys = "、".join(sorted((plan.get("subtasks") or {}).keys()))
            return "这个计划里没有子任务 %s（有：%s）" % (subtask, keys or "无")
        lines = ["子任务 %s（%s，%s）：" % (subtask, slot.get("title") or "",
                                          slot.get("mode") or ""),
                 "  状态：%s" % (slot.get("state") or "unknown")]
        if slot.get("turn_id"):
            lines.append("  回合句柄：%s" % slot.get("turn_id"))
            lines.append("  过程：%s/api/turns/%s/events" % (_agent_base(), slot.get("turn_id")))
        if slot.get("elapsed_ms"):
            lines.append("  用时：%.1f 秒" % (float(slot.get("elapsed_ms") or 0) / 1000.0))
        tools = slot.get("tools") or []
        if tools:
            names = []
            for tool in tools:
                names.append("%s%s" % (tool.get("name") or "?",
                                       "" if tool.get("ok") else "（失败）"))
            lines.append("  跑过的工具：%s" % "、".join(names))
        if slot.get("error"):
            lines.append("  出错：%s" % slot.get("error"))
        if slot.get("result"):
            lines.append("  结果：")
            lines.append(str(slot.get("result")))
        return "\n".join(lines)

    lines = ["计划 %s：%s" % (plan["id"], plan.get("title") or ""),
             "  目标：%s" % (plan.get("goal") or ""),
             "  状态：%s" % (plan.get("status") or "unknown")]
    for step in plan.get("steps") or []:
        slot = _slot(plan, step.get("key"))
        lines.append("  - %-6s [%s] %s：%s" % (step.get("key"), step.get("mode"),
                                              step.get("title") or "",
                                              slot.get("state") or "pending"))
    if plan.get("result"):
        lines.append("")
        lines.append("汇总：")
        lines.append(str(plan.get("result")))
    elif plan.get("status") == "running":
        lines.append("")
        lines.append("还没跑完。跑完了这里会有汇总。")
    return "\n".join(lines)


def meta_toolkit_plan():
    return {
        "name": "toolkit_plan",
        "description": (
            "Split a job into subtasks that run in parallel, and put the results back "
            "together. op=\"start\" (the default): the model splits the goal into subtasks - "
            "each one marked read-only (\"ro\") or read-write (\"rw\") - the subtasks start, "
            "and the answer comes back right away with a plan id. op=\"status\": how far along "
            "the plan is, what the subtasks found, and the summary once they are done "
            "(with subtask=\"s1\" it answers about that subtask only). Read-only subtasks run "
            "side by side; a read-write subtask runs alone. "
            "把一个任务拆成并行子任务，模型分解、模型汇总。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "op": {
                    "type": "string",
                    "enum": ["start", "status"],
                    "description": "start: split the goal and start the subtasks. "
                                   "status: ask how the plan is doing.",
                },
                "goal": {
                    "type": "string",
                    "description": "What the whole thing is about (op=start).",
                },
                "plan_id": {
                    "type": "string",
                    "description": "The plan to look at (op=status).",
                },
                "subtask": {
                    "type": "string",
                    "description": "One subtask of the plan (op=status). All of them by default.",
                },
                "workspace_id": {
                    "type": "string",
                    "description": "Where the subtasks run (the only workspace by default).",
                },
                "title": {
                    "type": "string",
                    "description": "A name for the plan.",
                },
                "max_subtasks": {
                    "type": "integer",
                    "description": "How many subtasks at most (default 4).",
                },
            },
        },
    }
