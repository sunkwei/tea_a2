#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The planner: turn one complex job into a plan the queue can work.

One model call, one plan. The plan is written to a file so that a human can
look at it - and change it - before it is handed to the queue. A plan is a
proposal, not an order: the model is good at splitting a job into steps, but
it does not know what the machine can do today, and it should not be the one
who decides what runs.

    # 1. the plan (one model call)
    python3 sched/planner.py --goal "把 30 个文件分类并写一份报告" --out /tmp/plan.json

    # 2. look at it, change it if you want
    $EDITOR /tmp/plan.json

    # 3. hand it to the queue: the steps become tasks
    python3 sched/planner.py --submit /tmp/plan.json

The plan is a plain JSON file:

    {
      "title": "...",
      "goal": "...",
      "aggregate_prompt": "how the result should be put together",
      "steps": [
        {"key": "read",   "title": "...", "prompt": "...", "depends_on": []},
        {"key": "report", "title": "...", "prompt": "...", "depends_on": ["read"]}
      ]
    }

The keys are the steps' names inside the plan; `depends_on` names them. A
step that waits for another one only starts when that one is done.

Standard library only.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import plans  # noqa: E402
import planning  # noqa: E402

DEFAULT_TASKS = os.environ.get("TEA_TASK_URL", "http://127.0.0.1:8283")
DEFAULT_AGENT = os.environ.get("TEA_AGENT_URL", "http://127.0.0.1:8282")

# The wording of the plan is the shared one (planning.PLANNER_SYSTEM): the
# queue's plan and the extension layer's plan are asked for in the same
# words - including which step only reads ("ro") and which one changes
# something ("rw").
PLANNER_SYSTEM = planning.PLANNER_SYSTEM


def log(message):
    print(message, flush=True)


def http_json(url, method="GET", body=None, timeout=120, headers=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    head = {"Accept": "application/json"}
    if data is not None:
        head["Content-Type"] = "application/json"
    if headers:
        head.update(headers)
    req = urllib.request.Request(url, data=data, headers=head, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            status = resp.status
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        status = exc.code
    except Exception as exc:  # noqa: BLE001
        return 0, {"error": str(exc)}
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, {"error": raw[:500]}


def unwrap(body):
    if isinstance(body, dict) and "data" in body and "code" in body:
        if body.get("code"):
            return {"error": body.get("message") or "the queue refused"}
        return body.get("data") or {}
    return body


class _AgentClient(object):
    """The bit of the agent the planning core needs: one model call.

    The planning core talks to the agent through a client with an
    ``llm_chat`` method (the same one sched/a2_ext_py/client.py has). This
    is that method - the planner is a standalone script and does not pull
    the whole client in.
    """

    def __init__(self, agent_url, timeout=180):
        self.agent_url = agent_url
        self.timeout = timeout

    def llm_chat(self, prompt=None, system=None, temperature=None, max_tokens=None,
                 provider=None, model=None, stream=False, timeout=None):
        body = {"prompt": prompt, "system": system, "stream": bool(stream)}
        if temperature is not None:
            body["temperature"] = temperature
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        if provider:
            body["provider"] = provider
        if model:
            body["model"] = model
        status, answer = http_json(self.agent_url + "/api/llm/chat", "POST", body,
                                   timeout=timeout or self.timeout)
        if status == 0 or status >= 400:
            raise planning.PlanError("模型没有回答：%s" % (answer or {}).get("error"))
        return answer if isinstance(answer, dict) else {}


def make_plan(agent_url, goal, title="", max_steps=6, timeout=180):
    """One model call: the goal becomes a plan.

    The plan comes from the shared core (sched/planning.py): the same
    prompt, the same checks - and every step says whether it only reads
    ("ro") or changes something ("rw").
    """
    client = _AgentClient(agent_url, timeout=timeout)
    try:
        return planning.make_plan(client, goal, title=title, max_steps=max_steps,
                                   timeout=timeout)
    except planning.PlanError as exc:
        raise SystemExit(str(exc))


def show(plan):
    log("计划：%s" % (plan.get("title") or ""))
    log("目标：%s" % (plan.get("goal") or ""))
    for step in plan.get("steps") or []:
        waits = ("（等 %s）" % "、".join(step.get("depends_on") or [])) if step.get("depends_on") else ""
        log("  - %-10s [%-2s] %s%s" % (step.get("key"), step.get("mode") or "rw",
                                       step.get("title"), waits))


def submit(tasks_url, plan, timeout=60):
    """Hand the plan to the queue: it becomes a parent and some steps."""
    status, body = http_json(tasks_url + "/api/plans", "POST", {"plan": plan}, timeout=timeout)
    if status == 0 or status >= 400:
        raise SystemExit("队列没有收下这个计划：%s" % (body or {}).get("error"))
    data = unwrap(body)
    if not isinstance(data, dict):
        raise SystemExit("队列的回答看不懂：%r" % (body,))
    plan_id = (data.get("plan") or {}).get("id") or "?"
    parent = (data.get("parent") or {}).get("id") or "?"
    log("计划 %s 收下了（父任务 %s）" % (plan_id, parent))
    for step in data.get("steps") or []:
        task = step.get("task") or {}
        log("  - %-10s %s  任务 %s" % (step.get("key"), step.get("title"), task.get("id")))
    return data


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Split one complex job into steps the queue can work.")
    ap.add_argument("--goal", default="", help="what the whole thing is about")
    ap.add_argument("--title", default="", help="a name for the job")
    ap.add_argument("--out", default="", help="write the plan here (a JSON file)")
    ap.add_argument("--submit", default="", metavar="FILE",
                    help="hand this plan file to the queue")
    ap.add_argument("--tasks", default=DEFAULT_TASKS, help="the queue's HTTP API")
    ap.add_argument("--agent", default=DEFAULT_AGENT, help="the agent's HTTP API")
    ap.add_argument("--max-steps", type=int, default=6, help="how many steps at most")
    ap.add_argument("--workspace", default="",
                    help="where the steps run (a workspace id or a path; "
                         "the queue's own workspace by default)")
    ap.add_argument("--timeout", type=float, default=180.0, help="seconds for the model call")
    args = ap.parse_args(argv)

    if args.submit:
        path = os.path.abspath(os.path.expanduser(args.submit))
        try:
            with open(path, encoding="utf-8") as f:
                plan = json.load(f)
        except (OSError, ValueError) as exc:
            raise SystemExit("读不了 %s：%s" % (path, exc))
        if not isinstance(plan, dict):
            raise SystemExit("%s 不是一个 JSON 对象" % path)
        try:
            plan["steps"] = plans.normalize_steps(plan.get("steps"), max_steps=args.max_steps)
        except plans.PlanError as exc:
            raise SystemExit("这个计划有问题：%s" % exc)
        if args.workspace:
            plan["workspace_id"] = args.workspace
        show(plan)
        submit(args.tasks, plan)
        return 0

    if not args.goal:
        raise SystemExit("--goal 说清楚要做什么，或者用 --submit 交一个计划文件")
    plan = make_plan(args.agent, args.goal, args.title, args.max_steps, args.timeout)
    if args.workspace:
        plan["workspace_id"] = args.workspace
    show(plan)
    if args.out:
        path = os.path.abspath(os.path.expanduser(args.out))
        with open(path, "w", encoding="utf-8") as f:
            json.dump(plan, f, ensure_ascii=False, indent=2)
        log("\n计划写在 %s（可以改了再 --submit）" % path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
