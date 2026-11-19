#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Smoke test for the planning core's rules (sched/planning.py).

No agent, no network, no model: the rules are plain functions, and they are
the part the orchestrator (the extension layer) and the queue both stand on.
A plan that lies about its steps is worse than no plan, so the promises are
pinned here:

  * a write path is a relative path inside the workspace - an absolute one and
    one that climbs out with ".." are refused (not quietly rewritten);
  * a step that only reads may not name a write path;
  * two write steps may not fight over the same place;
  * the dependencies exist and do not make a circle;
  * a step that left no result says so in the summary prompt (an empty
    section reads like "found nothing", which is not the same thing);
  * a long result is cut before it eats the summary's whole context;
  * the read-only role is checked for what makes it read-only (read_only +
    a tool list without the tools that can change something).

Usage: python3 tests/smoke_planning_rules.py [path-to-tea_agent-binary]
(the binary is not used - the suite hands it to every test).
"""

import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "sched"))

import planning  # noqa: E402

failures = []
checks = []


def check(name, ok, detail=""):
    checks.append(name)
    print(("PASS " if ok else "FAIL ") + name + (("  " + str(detail)) if not ok else ""))
    if not ok:
        failures.append(name)


def refused(steps):
    try:
        planning.normalize_steps(steps)
    except planning.PlanError as exc:
        return str(exc)
    return ""


def step(**fields):
    one = {"key": "s1", "title": "t", "prompt": "do it"}
    one.update(fields)
    return one


def role_from_yaml(text):
    """The two things role_is_read_only() looks at, out of the YAML text.

    No YAML parser on purpose: the file is ours, and the two fields are all
    that matters here.
    """
    role = {"read_only": False, "tools": []}
    in_tools = False
    for line in text.splitlines():
        if line.startswith("read_only:"):
            role["read_only"] = line.split(":", 1)[1].strip().lower() in (
                "true", "yes", "on", "1")
            in_tools = False
        elif line.startswith("tools:"):
            in_tools = True
        elif in_tools and line.strip().startswith("- "):
            role["tools"].append(line.strip()[2:].strip().strip('"'))
    return role


def main():
    # ---- a write path has to be inside the workspace --------------------
    for bad in ("/etc/passwd", "a/../../etc", "../outside", "..", "C:/Windows"):
        why = refused([step(mode="rw", write_paths=[bad])])
        check("a write path outside the workspace is refused: %s" % bad, bool(why), bad)

    try:
        steps = planning.normalize_steps([step(mode="rw", write_paths=["src/", "./docs"])])
        check("a relative write path is accepted", steps[0]["write_paths"] == ["src", "./docs"],
              steps[0]["write_paths"])
    except planning.PlanError as exc:
        check("a relative write path is accepted", False, exc)

    # ---- the modes -------------------------------------------------------
    check("a read-only step may not name write paths",
          bool(refused([step(mode="ro", write_paths=["src"])])))
    try:
        steps = planning.normalize_steps([step()])
        check("a step without a mode is read-write (the safe answer)",
              steps[0]["mode"] == "rw", steps[0]["mode"])
    except planning.PlanError as exc:
        check("a step without a mode is read-write (the safe answer)", False, exc)
    check("an unknown mode is refused", bool(refused([step(mode="maybe")])))
    check("two write steps may not write the same place",
          bool(refused([step(key="s1", mode="rw", write_paths=["out"]),
                        step(key="s2", mode="rw", write_paths=["out"])])))

    # ---- the dependencies ------------------------------------------------
    check("a dependency on a step that is not there is refused",
          bool(refused([step(key="s1", mode="ro", depends_on=["ghost"])])))
    check("a step that waits for itself is refused",
          bool(refused([step(key="s1", mode="ro", depends_on=["s1"])])))
    check("a circle is refused",
          bool(refused([step(key="s1", mode="ro", depends_on=["s2"]),
                        step(key="s2", mode="ro", depends_on=["s1"])])))
    try:
        steps = planning.normalize_steps([step(key="s1", mode="ro"),
                                          step(key="s2", mode="rw", depends_on=["s1"],
                                               write_paths=["out"])])
        check("a step that waits for another one is accepted", len(steps) == 2, steps)
    except planning.PlanError as exc:
        check("a step that waits for another one is accepted", False, exc)

    # ---- what the summary is told ---------------------------------------
    plan = {"goal": "g",
            "steps": [{"key": "s1", "title": "查鉴权", "mode": "ro"},
                      {"key": "s2", "title": "查配置", "mode": "ro"}]}
    text = planning.aggregate_prompt(plan, {"s1": "结论 A", "s2": ""})
    check("a step that left nothing says so in the summary prompt",
          planning.MISSING_RESULT in text, text[-200:])
    check("the step that did answer is quoted", "结论 A" in text, text[-200:])

    long_text = "x" * (planning.MAX_RESULT_CHARS + 500)
    text = planning.aggregate_prompt(plan, {"s1": long_text, "s2": "ok"})
    check("a long result is cut before the summary is asked for",
          len(text) < len(long_text), len(text))
    check("the cut says it was cut", "太长了" in text, text[-200:])
    check("the summary prompt does not carry the whole long result",
          long_text not in text)

    short = planning.aggregate_prompt(plan, {"s1": "结论 A", "s2": "结论 B"})
    check("a short result is left alone", "太长了" not in short and "结论 B" in short, short[-200:])

    # ---- the read-only role ---------------------------------------------
    ok, why = planning.role_is_read_only(role_from_yaml(planning.READ_ONLY_ROLE_YAML))
    check("the read-only role we ship passes the check", ok, why)
    shipped = role_from_yaml(planning.READ_ONLY_ROLE_YAML)
    check("the shipped role really lists tools", len(shipped["tools"]) >= 5, shipped["tools"])

    check("a role that is not read_only is refused",
          not planning.role_is_read_only({"read_only": False,
                                          "tools": ["toolkit_file"]})[0])
    check("a role with no tool list is refused (an empty list means every tool)",
          not planning.role_is_read_only({"read_only": True, "tools": []})[0])
    for bad_tool in ("toolkit_exec", "toolkit_memory", "toolkit_agent_doc",
                     "mcp__a2ext__toolkit_plan"):
        check("a read-only role may not have %s" % bad_tool,
              not planning.role_is_read_only({"read_only": True,
                                              "tools": ["toolkit_file", bad_tool]})[0])

    print()
    print("%d/%d checks passed" % (len(checks) - len(failures), len(checks)))
    if failures:
        print("FAILED:", ", ".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
