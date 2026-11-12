#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The task queue: enqueue, run, watch - on top of the agent's HTTP API.

The agent (tea_agent) is the **workspace channel**: it runs the turns, holds the
sessions, touches the files. This server is the **queue**: work that does not
have to happen right now is put in a list and worked through when the provider
can afford it - for MiniMax M Plan that means the subscription has quota left.

  tea_agent -- the channel: POST /api/chat (SSE), GET /api/providers/{n}/usage,
               POST /api/chat/stop, GET /api/workspaces
  this file  -- the queue:   the records, the scheduler, the task API

The agent knows nothing about the queue: it is told "this turn works on task X"
and the agent reports on it (toolkit_task); the report is read back from the
turn's event stream. So the agent runs perfectly well without this server, and
this server is optional.

Usage:

  # serve the task API (default: agent on 8282, this server on 8283)
  python3 sched/task_server.py

  # put a task in the queue (no server needed: it writes straight into the store)
  python3 sched/task_server.py --submit "把 README 的例子跑一遍"

  # what is in the queue
  curl http://127.0.0.1:8283/api/tasks

Standard library only: this runs on the device, next to the agent, where a pip
install is not an option.
"""

import argparse
import base64
import calendar
import hashlib
import json
import os
import re
import select
import shutil
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler


if sys.version_info[0] < 3:
    # 这台机器的 `python` 是 2.7（`python3` 才是 3.6）。这个脚本是 Python 3 的，
    # 在 2 里连解析都过不去 —— 与其让用户看 "Non-ASCII character"，
    # 不如直接说该用哪个解释器。
    sys.stderr.write("这个脚本要 Python 3：请用 python3 跑"
                     "（这台机器的 python 是 2.7）。\n")
    sys.exit(2)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import plans  # noqa: E402
import task_order  # noqa: E402
import triggers  # noqa: E402
from a2_ext_py import ApiError, Client  # noqa: E402

try:
    # Python 3.7+
    from http.server import ThreadingHTTPServer
except ImportError:  # Python 3.6
    from http.server import HTTPServer
    from socketserver import ThreadingMixIn

    class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
        """The 3.6 spelling of http.server.ThreadingHTTPServer."""
        daemon_threads = True

DEFAULT_AGENT = os.environ.get("TEA_AGENT_URL", "http://127.0.0.1:8282")
DEFAULT_PORT = int(os.environ.get("TEA_TASK_PORT", "8283"))
DEFAULT_WORKSPACE_PATH = os.path.expanduser(os.environ.get(
    "TEA_TASK_WORKSPACE", os.path.join("~", ".tea_agent", "task-workspace")))
# The ledger lives in the queue's own workspace, next to the work: one directory
# holds everything about the queue.
DEFAULT_DATA_DIR = os.path.expanduser(os.environ.get(
    "TEA_TASK_DATA", os.path.join(DEFAULT_WORKSPACE_PATH, "tasks")))
# The provider whose quota gates the work. MiniMax M Plan reports one; a
# provider without a quota endpoint is worked on anyway (with a warning).
DEFAULT_PROVIDER = os.environ.get("TEA_TASK_PROVIDER", "minimax")

# The business codes of the unified envelope (the requirement's appendix).
CODE_OK = 0
CODE_BAD_REQUEST = 40001
CODE_UNAUTHENTICATED = 40101
CODE_FORBIDDEN = 40301
CODE_NOT_FOUND = 40401
CODE_CONFLICT = 40901
CODE_DUPLICATE = 40902
CODE_RATE_LIMIT = 42901
CODE_INTERNAL = 50001
CODE_STORAGE = 50002
CODE_EXPORT = 50003

# The string code the handlers use -> the number the envelope carries.
ERROR_CODES = {
    "bad_request": CODE_BAD_REQUEST,
    "unauthenticated": CODE_UNAUTHENTICATED,
    "forbidden": CODE_FORBIDDEN,
    "not_found": CODE_NOT_FOUND,
    "not_waiting": CODE_CONFLICT,
    "not_failed": CODE_CONFLICT,
    "already_finished": CODE_CONFLICT,
    "no_quota": CODE_CONFLICT,
    "task_running": CODE_CONFLICT,
    "duplicate": CODE_DUPLICATE,
    "rate_limited": CODE_RATE_LIMIT,
    "io_error": CODE_STORAGE,
    "export_failed": CODE_EXPORT,
    "internal": CODE_INTERNAL,
}

AGGREGATE_SYSTEM = (
    "你在给一个复杂任务做最终汇总。下面是这个任务的目标和每个步骤的结果。\n"
    "请合成一份完整、可以直接交付的结果：不要罗列步骤，要给出结论性的答案；"
    "步骤之间有矛盾就指出来；缺什么就说明缺什么。只输出汇总本身，"
    "不要解释，不要 markdown 代码块。")

# Which queue this is. The client asks before it speaks (see the
# client's _tasks_prefix): the version segment arrived in 0.21.0.
QUEUE_VERSION = "1.2.12"

QUEUED = "queued"
RUNNING = "running"
WAITING = "waiting_confirmation"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"
TERMINAL = (DONE, FAILED, CANCELLED)


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def split_task_model(raw):
    """'provider+model' -> ('provider', 'model').

    The plus is the separator the operator writes in provider.yaml. The
    provider is the part before the first plus; the model may contain one
    itself (a name is longer than a provider name).
    """
    text = (raw or "").strip().strip("'\"")
    if not text:
        return "", ""
    if "+" not in text:
        return text, ""
    provider, _, model = text.partition("+")
    return provider.strip(), model.strip()


def read_task_model_from_file(path):
    """The `task_model:` line of provider.yaml, read without a YAML parser.

    The agent owns provider.yaml and normally tells the queue what it says
    (GET /api/health). This is the fallback for when the agent is down or is
    an old build that does not know the key: it is one top-level scalar, so a
    line scan is enough - the queue must not need a YAML parser (the devices
    have no pyyaml).
    """
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                if not line or line[0] in " \t#-":
                    continue          # indented, a comment or a list item
                if ":" not in line:
                    continue
                key, _, value = line.partition(":")
                if key.strip() != "task_model":
                    continue
                return split_task_model(value)
    except OSError:
        return "", ""
    return "", ""


def iso_from_epoch(epoch):
    """An ISO-8601 UTC stamp from epoch seconds."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(epoch)))


def parse_iso(text):
    """The epoch seconds of a stamp this module wrote ('' -> None)."""
    if not text:
        return None
    try:
        return calendar.timegm(time.strptime(text, "%Y-%m-%dT%H:%M:%SZ"))
    except ValueError:
        return None


def log(message):
    print("%s  %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), message), flush=True)


def iso_from_epoch(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def parse_not_before(text, now=None):
    """When a task may start at least.

      '+5'      five hours from now ('+30m', '+2d' work too)
      '23:30'   today at 23:30 (local time); if that hour has already passed
                today, the task may start as soon as possible
      '2026-10-24T09:00:00Z'   an absolute time (UTC, like every other stamp)

    Returns (iso_utc, the text as it was written) - (None, "") when the text
    means nothing.
    """
    raw = (text or "").strip()
    if not raw:
        return None, ""
    now = time.time() if now is None else now

    match = re.match(r"^\+(\d+)\s*([hmd]?)$", raw)
    if match:
        amount = int(match.group(1))
        unit = match.group(2) or "h"
        seconds = amount * {"h": 3600, "m": 60, "d": 86400}[unit]
        return iso_from_epoch(now + seconds), raw

    match = re.match(r"^(\d{1,2}):(\d{2})$", raw)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        if hour > 23 or minute > 59:
            return None, ""
        local = time.localtime(now)
        today = time.mktime((local.tm_year, local.tm_mon, local.tm_mday,
                              hour, minute, 0, 0, 0, -1))
        if today <= now:
            # The hour has passed today: start as soon as possible (not
            # tomorrow - the user meant today). Rounded to the minute, so the
            # stamp reads like the other times.
            return iso_from_epoch(int(now // 60) * 60), raw
        return iso_from_epoch(today), raw

    # A local time without a zone: "2026-11-04T09:30" (what a browser's
    # datetime picker hands over) or "2026-11-04 09:30". It is local time,
    # so it is converted to the UTC every other stamp uses.
    match = re.match(r"^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{1,2}):(\d{2})(?::(\d{2}))?)?$", raw)
    if match:
        try:
            stamp = time.mktime((int(match.group(1)), int(match.group(2)),
                                 int(match.group(3)), int(match.group(4) or 0),
                                 int(match.group(5) or 0), int(match.group(6) or 0),
                                 0, 0, -1))
        except (OverflowError, ValueError):
            return None, ""
        return iso_from_epoch(stamp), raw

    stamp = parse_iso(raw)
    if stamp is not None:
        return raw, raw
    return None, ""


# The model knobs a task may carry - the same ones the agent's chat API takes
# (see apply_knob_overrides in src/llm.cpp). A knob nobody set stays out of
# the request; a knob nobody knows is an error, never a silent no-op.
MODEL_KNOBS = ("temperature", "top_p", "top_k", "frequency_penalty",
               "presence_penalty", "seed", "max_tokens", "reasoning_effort")

# The number knobs and the range each of them lives in.
KNOB_RANGES = {
    "temperature": (0.0, 2.0),
    "top_p": (0.0, 1.0),
    "frequency_penalty": (-2.0, 2.0),
    "presence_penalty": (-2.0, 2.0),
}
# The whole-number knobs and their upper bound.
KNOB_LIMITS = {"top_k": 1000000, "seed": 2147483647, "max_tokens": 100000000}


def _knob_number(value):
    """A number from a JSON value or from a text field ("" -> None)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    text = str(value if value is not None else "").strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return None


def parse_model_params(body):
    """The model knobs of a task: (params, error message).

    The knobs may come as `model_params` (a dict), as `params` (the same dict
    under another name) or as the top-level fields of the request - the
    agent's own chat API takes them that way, so a caller that knows one API
    knows both. Values may be numbers or strings (a web form sends strings).

    A knob nobody knows is an error with its name: "I set temperature and
    nothing happened" is the worst possible answer.
    """
    body = body if isinstance(body, dict) else {}
    wanted = {}
    for name in ("model_params", "params"):
        value = body.get(name)
        if value is None:
            continue
        if not isinstance(value, dict):
            return {}, "'%s' must be an object (knob: value pairs)" % name
        for key, raw in value.items():
            if key not in MODEL_KNOBS:
                return {}, ("不认识的模型参数 %r（认识的：%s）"
                            % (key, "、".join(MODEL_KNOBS)))
            wanted[key] = raw
    # The top level: only the knobs are read - the rest of the body is the
    # task itself (prompt, title, workspace, ...).
    for key in MODEL_KNOBS:
        if key in body:
            wanted[key] = body[key]

    params = {}
    for key, raw in wanted.items():
        if raw is None:
            continue                       # "not set"
        if key == "reasoning_effort":
            text = str(raw).strip()
            if not text:
                continue
            params[key] = text
            continue
        number = _knob_number(raw)
        if number is None:
            return {}, "'%s' must be a number" % key
        if key in KNOB_RANGES:
            low, high = KNOB_RANGES[key]
            if number < low or number > high:
                return {}, "'%s' must be between %g and %g" % (key, low, high)
            params[key] = number
        elif key in KNOB_LIMITS:
            if isinstance(number, float) and number != int(number):
                return {}, "'%s' must be a whole number" % key
            number = int(number)
            limit = KNOB_LIMITS[key]
            if number < 0 or number > limit:
                return {}, "'%s' must be between 0 and %d" % (key, limit)
            params[key] = number
        else:
            params[key] = number
    return params, ""


# The budget of one turn: the ceilings the agent enforces. 0 => no limit for
# that dimension. The agent's own settings are the hard ceiling (a request may
# only tighten them); the queue hands each task its own budget inside it.
LIMIT_KEYS = ("max_iterations", "max_wall_ms", "max_tool_calls", "max_total_tokens")


def parse_limits(body):
    """The budget of a task: (limits, error message).

    The same shape the agent's /api/chat takes (`limits`). A key nobody knows
    is an error, never a silent no-op: a task that believes it has a budget
    and does not is the worst kind of surprise.
    """
    body = body if isinstance(body, dict) else {}
    raw = body.get("limits")
    if raw in (None, ""):
        return {}, ""
    if not isinstance(raw, dict):
        return {}, "'limits' must be an object (max_iterations, max_wall_ms, " \
                   "max_tool_calls, max_total_tokens)"
    limits = {}
    for key, value in raw.items():
        if key not in LIMIT_KEYS:
            return {}, ("不认识的预算项 %r（认识的：%s）" % (key, "、".join(LIMIT_KEYS)))
        if isinstance(value, bool):
            return {}, "预算项 %r 必须是一个整数" % key
        if isinstance(value, str):
            # A task order file (and a web form) write numbers as text.
            try:
                number = int(value.strip())
            except ValueError:
                return {}, "预算项 %r 必须是一个整数" % key
        elif isinstance(value, (int, float)):
            number = int(value)
        else:
            return {}, "预算项 %r 必须是一个整数" % key
        if number < 0:
            return {}, "预算项 %r 不能是负数" % key
        limits[key] = number
    return limits, ""


def parse_policy(body):
    """The permissions of a task: (policy, error message).

    `policy.tools` is the list of tools the model may use while this task runs
    (empty => the usual set); the directories the task may touch are its own
    `allow_paths` (the agent's boundary is the workspace, a task may ask for
    more). The queue decides, the agent enforces.
    """
    body = body if isinstance(body, dict) else {}
    raw = body.get("policy")
    if raw in (None, ""):
        return {}, ""
    if not isinstance(raw, dict):
        return {}, "'policy' must be an object"
    for key in raw:
        if key != "tools":
            return {}, ("不认识的权限项 %r（认识的：tools）" % key)
    policy = {}
    tools = raw.get("tools")
    if tools not in (None, ""):
        if isinstance(tools, str):
            tools = [t.strip() for t in tools.replace(";", ",").split(",")]
        if not isinstance(tools, list):
            return {}, "'policy.tools' must be a list of tool names"
        names = []
        for one in tools:
            if not isinstance(one, str):
                return {}, "'policy.tools' must be a list of tool names"
            one = one.strip()
            if one and one not in names:
                names.append(one)
        policy["tools"] = names
    return policy, ""


# The agent's HTTP client and its error type live in the extension layer
# (`a2_ext_py`): there used to be a third copy of the same client here, and
# three copies of one interface drift.
Agent = Client
AgentError = ApiError


def _doc_page(title, html):
    """A small web page around the rendered documentation."""
    return """<!doctype html>
<html lang="zh"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>%s</title>
<style>
  :root { color-scheme: light dark; }
  body { margin: 0 auto; max-width: 52rem; padding: 2rem 1.2rem 4rem;
         font: 16px/1.7 -apple-system, "Segoe UI", "Noto Sans CJK SC", sans-serif; }
  h1, h2, h3 { line-height: 1.3; }
  h2 { margin-top: 2.4em; border-bottom: 1px solid #8884; padding-bottom: .2em; }
  code { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
         font-size: .92em; background: #8881; padding: .1em .35em; border-radius: 4px; }
  pre { background: #8881; padding: .9rem 1rem; border-radius: 8px; overflow-x: auto; }
  pre code { background: none; padding: 0; }
  table { border-collapse: collapse; width: 100%%; margin: 1em 0; font-size: .95em; }
  th, td { border: 1px solid #8884; padding: .45em .6em; text-align: left;
           vertical-align: top; }
  th { background: #8881; }
  blockquote { margin: 1em 0; padding: .2em 1em; border-left: 3px solid #8886;
               color: #8889; }
  a { color: #3b82f6; }
</style>
</head><body>
%s
</body></html>
""" % (_html_escape(title), html)


def _html_escape(text):
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def render_markdown(text):
    """Markdown to HTML, without any dependency.

    The queue must not need a pip install to show its own documentation (the
    release package carries no third-party modules), so this renders the
    subset that docs/tasks.md actually uses: headings, fenced code, tables,
    lists, quotes, rules, and the inline marks (code, bold, italic, links).
    """
    import html as _html
    import re as _re

    out = []
    lines = (text or "").split("\n")
    i = 0
    in_list = None          # "ul" / "ol"

    def close_list():
        nonlocal in_list
        if in_list:
            out.append("</%s>" % in_list)
            in_list = None

    def inline(chunk):
        chunk = _html.escape(chunk, quote=False)
        # `code`
        chunk = _re.sub(r"`([^`]+)`", r"<code>\1</code>", chunk)
        # [text](url) - a relative .md link points at the document the
        # queue serves, not at a file next to the page.
        def _link(m):
            label, href = m.group(1), m.group(2)
            if href.endswith(".md") and "://" not in href:
                href = "/api/doc/" + href.rsplit("/", 1)[-1]
            return '<a href="%s">%s</a>' % (href, label)
        chunk = _re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", _link, chunk)
        # **bold** / *italic*
        chunk = _re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", chunk)
        chunk = _re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<em>\1</em>", chunk)
        return chunk

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # a fenced code block
        if stripped.startswith("```"):
            close_list()
            lang = stripped[3:].strip()
            i += 1
            code = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code.append(lines[i])
                i += 1
            i += 1
            cls = ' class="lang-%s"' % _html.escape(lang, quote=True) if lang else ""
            out.append("<pre><code%s>%s</code></pre>"
                       % (cls, _html.escape("\n".join(code), quote=False)))
            continue

        # a heading
        m = _re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if m:
            close_list()
            level = len(m.group(1))
            out.append("<h%d>%s</h%d>" % (level, inline(m.group(2).strip()), level))
            i += 1
            continue

        # a horizontal rule
        if _re.match(r"^(-{3,}|\*{3,}|_{3,})$", stripped):
            close_list()
            out.append("<hr>")
            i += 1
            continue

        # a table: a header row, a separator row, then the body
        if (stripped.startswith("|") and i + 1 < len(lines)
                and _re.match(r"^\|[\s:|-]+\|$", lines[i + 1].strip())):
            close_list()
            head = [c.strip() for c in stripped.strip("|").split("|")]
            i += 2
            body = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                body.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            out.append("<table><thead><tr>%s</tr></thead><tbody>%s</tbody></table>"
                       % ("".join("<th>%s</th>" % inline(c) for c in head),
                          "".join("<tr>%s</tr>" % "".join("<td>%s</td>" % inline(c)
                                                        for c in row)
                                  for row in body)))
            continue

        # a list
        m = _re.match(r"^(\s*)([-*+])\s+(.*)$", line)
        if m:
            want = "ul"
        else:
            m = _re.match(r"^(\s*)(\d+)[.)]\s+(.*)$", line)
            want = "ol" if m else None
        if want:
            close_list()
            in_list = want
            out.append("<%s><li>%s</li>" % (want, inline(m.group(3).strip())))
            i += 1
            continue
        if in_list and stripped:
            # another item of the open list
            m = _re.match(r"^\s*(?:[-*+]|\d+[.)])\s+(.*)$", line)
            if m:
                out[-1] = out[-1][:-len("</li>")] + inline(m.group(1).strip()) + "</li>"
                i += 1
                continue

        # a quote
        if stripped.startswith(">"):
            close_list()
            quote = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                quote.append(lines[i].strip().lstrip(">").strip())
                i += 1
            out.append("<blockquote>%s</blockquote>"
                       % inline(" ".join(q for q in quote if q)))
            continue

        # an empty line
        if not stripped:
            close_list()
            i += 1
            continue

        # a paragraph
        close_list()
        para = [stripped]
        i += 1
        while i < len(lines) and lines[i].strip() and not _re.match(
                r"^(#{1,6}\s|```|>|\||-{3,}$)", lines[i].strip()):
            para.append(lines[i].strip())
            i += 1
        out.append("<p>%s</p>" % inline(" ".join(para)))
    close_list()
    return "\n".join(out)


def artifact_id(path):
    """A stable id for a file the work produced.

    The path may change its name later; the id does not (it is the hash of
    the path), so a download link stays valid as long as the file does.
    """
    return "art_" + hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:12]


def new_task(body, workspace_id, allow_paths=None, priority=0, not_before="",
             not_before_text="", key="", meta=None, model_params=None,
             limits=None, policy=None):
    """A task record, from the fields a task order (or the API) carries."""
    return {
        "id": "t_" + os.urandom(6).hex(),
        # The trace id follows the task through its whole life: it is in the
        # events, in the audit log and in the answers, so one string ties
        # everything about this task together.
        "trace_id": "tr_" + os.urandom(8).hex(),
        # The tenant this task belongs to and who submitted it. Empty when
        # the submitter is the local operator (no key).
        "tenant_id": "",
        "identity": "",
        "workspace_id": workspace_id,
        "prompt": str(body.get("prompt") or "").strip(),
        "title": str(body.get("title") or "").strip(),
        "provider": str(body.get("provider") or "").strip(),
        "model": str(body.get("model") or "").strip(),
        # The model knobs of this task (temperature, max_tokens, ...). Empty
        # means "the defaults of the model" - nothing is sent to the wire.
        "model_params": dict(model_params or {}),
        "allow_paths": list(allow_paths or []),
        # The budget of this task (the ceilings the agent enforces) and the
        # permissions it runs with (which tools it may use). Both are handed to
        # the agent with the turn.
        "limits": dict(limits or {}),
        "policy": dict(policy or {}),
        "session_id": "",
        # The checkpoint of this task: the turn it runs in, the session and the
        # trajectory. It is what a restarted queue picks the work up from.
        "checkpoint": {},
        "status": QUEUED,
        "progress": 0,
        "note": "",
        "steps": [],
        "needs_confirmation": False,
        "question": "",
        "error": "",
        "result": "",
        "meta": meta if isinstance(meta, dict) else {},
        "priority": priority,
        "attempt": 1,
        "not_before": not_before,
        "not_before_text": not_before_text,
        "idempotency_key": key,
        "pending_message": "",
        # The trigger this task came from (empty when it was submitted
        # directly) and the thread it belongs to (the conversation).
        "trigger_id": "",
        "thread_id": "",
        # The plan this task belongs to ("" = a standalone task). The parent
        # holds the final result; the children are the steps. A task that
        # waits for others says which ones in depends_on.
        "parent_id": "",
        "plan_id": "",
        "depends_on": [],
        "kind": "",            # "" = a normal task, "plan" = the container
        "children": [],
        # Where the work runs: "" = the local agent, "fleet" = the devices'
        # own agents (each device answers with its own provider and context).
        "target": "",
        "fleet": {},
        # How the turn ended and what it cost: "stop_reason" is "" (the model
        # finished), "budget" (a ceiling was reached), "cancelled" or "error".
        "stop_reason": "",
        "budget_used": {},
        "usage": {},
        "iterations": 0,
        "trajectory_id": "",
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "started_at": "",
        "finished_at": "",
    }


def resolve_workspace_id(agent, wanted):
    """A path or a workspace id -> the id. "" = the queue's own workspace."""
    text = (wanted or "").strip()
    if not text:
        return ""
    workspaces = agent.workspaces() or []
    for ws in workspaces:
        if ws.get("id") == text:
            return text
    path = os.path.abspath(os.path.expanduser(text))
    for ws in workspaces:
        if os.path.abspath(ws.get("path") or "") == path:
            return ws.get("id", "")
    known = ", ".join("%s (%s)" % (w.get("id", "?"), w.get("path", ""))
                      for w in workspaces) or "(agent 没有工作区)"
    hint = ""
    # The usual mistake: the order names a POSIX path, but the agent answered
    # for another machine (a Windows one, another box) - its workspaces are
    # all "C:\\...", so no path here will ever match.
    if text.startswith("/") and workspaces and all(":\\" in (w.get("path") or "")
                                                  for w in workspaces):
        hint = ("\n（这个 agent 报的都是 Windows 路径，像是另一台机器上的；"
                "要么用 --agent 指向本机的 agent，要么在任务单里写工作区 id。）")
    raise task_order.OrderError(
        "agent（%s）里没有这个工作区：%s；已知的：%s%s"
        % (getattr(agent, "base", "?") or "?", text, known, hint))


class Store:
    """The queue's own records: one file per task, one directory per workspace.

    The records live here and not in the workspace: the workspace belongs to the
    agent, this is the queue's own bookkeeping.
    """

    def __init__(self, root):
        self.root = os.path.abspath(os.path.expanduser(root))
        self.lock = threading.RLock()
        # task id -> the event_id the next event of that task gets
        self._event_seq = {}
        os.makedirs(self.root, exist_ok=True)

    def _path(self, task_id):
        return os.path.join(self.root, task_id + ".json")

    def _events_path(self, task_id):
        return os.path.join(self.root, task_id + ".events.jsonl")

    def record_path(self, workspace_id, task_id):
        """Where a task's record lives - the page shows it."""
        return self._path(task_id)

    def events_path(self, task_id):
        return self._events_path(task_id)

    # ---- the event log -------------------------------------------------
    def record_event(self, task_id, event_type, payload):
        """Append one event to the task's log. Returns the event.

        The events used to be a broadcast and nothing else: whoever was
        listening saw them, and after a restart there was no way to find out
        what had happened to a task. Now every state change is written down,
        with a number the client can ask "everything after N".

        The number is counted in memory and only the *last* line of the log is
        read (when the process does not know the task yet). Reading the whole
        log for every event made a long task quadratic in its own events: the
        tenthousandth event cost ten thousand lines of parsing.
        """
        with self.lock:
            number = self._next_event_id(task_id)
            event = {"event_id": number, "task_id": task_id, "type": event_type,
                     "payload": payload if isinstance(payload, dict) else {},
                     "created_at": now_iso()}
            try:
                with open(self._events_path(task_id), "a", encoding="utf-8") as f:
                    f.write(json.dumps(event, ensure_ascii=False) + "\n")
            except OSError:
                pass
            return event

    def _next_event_id(self, task_id):
        """The number the next event of this task gets."""
        known = self._event_seq.get(task_id)
        if known is not None:
            self._event_seq[task_id] = known + 1
            return known
        last = self._last_event_id(task_id)
        # last + 1 is this event's number, so the next one is last + 2.
        self._event_seq[task_id] = last + 2
        return last + 1

    def _last_event_id(self, task_id):
        """The event_id of the last line of the log (0 when there is none).

        Only the tail of the file is read: the log of a long task can have
        thousands of lines, and the number of the last one is all we need.
        """
        path = self._events_path(task_id)
        try:
            with open(path, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                if not size:
                    return 0
                pos = size
                tail = b""
                while pos > 0:
                    step = min(8192, pos)
                    pos -= step
                    f.seek(pos)
                    chunk = f.read(step)
                    tail = chunk + tail
                    # A newline inside the chunk (not at its very end) means
                    # the last line is complete.
                    if pos == 0 or b"\n" in chunk[:-1]:
                        break
                lines = [line for line in tail.split(b"\n") if line.strip()]
                if not lines:
                    return 0
                event = json.loads(lines[-1].decode("utf-8", "replace"))
                return int(event.get("event_id") or 0)
        except (OSError, ValueError, TypeError):
            return 0

    def events(self, task_id, since_event_id=0):
        """The task's events, newest last. `since_event_id` skips the old ones."""
        out = []
        try:
            with open(self._events_path(task_id), encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if event.get("event_id", 0) > since_event_id:
                        out.append(event)
        except OSError:
            pass
        return out

    # ---- the queue's own state ------------------------------------------
    def _state_path(self):
        return os.path.join(self.root, "queue_state.json")

    def state(self):
        """The queue's own switches. Right now: is the scheduler paused?

        The pause lives in a file, not in memory: it is an operator decision
        ("stop working until I say so") and it has to survive a restart of
        the service - a queue that starts working again by itself the moment
        it is restarted is a queue nobody can trust to stay quiet.
        """
        data = {}
        try:
            with open(self._state_path(), encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        return {
            "paused": bool(data.get("paused")),
            "reason": str(data.get("reason") or ""),
            "paused_at": str(data.get("paused_at") or ""),
        }

    def set_paused(self, paused, reason=""):
        state = self.state()
        state["paused"] = bool(paused)
        state["reason"] = str(reason or "") if paused else ""
        state["paused_at"] = now_iso() if paused else ""
        try:
            with open(self._state_path(), "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
        except OSError:
            pass
        return state

    # ---- the audit log -------------------------------------------------
    def audit(self, action, task_id="", detail=None, actor=""):
        """One line in the audit log: who did what to which task."""
        entry = {"at": now_iso(), "action": action, "task_id": task_id or "",
                 "actor": actor or "", "detail": detail if isinstance(detail, dict) else {}}
        try:
            with open(os.path.join(self.root, "audit.log"), "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass
        return entry

    def audit_log(self, task_id="", limit=200):
        out = []
        try:
            with open(os.path.join(self.root, "audit.log"), encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
                    if task_id and entry.get("task_id") != task_id:
                        continue
                    out.append(entry)
        except OSError:
            pass
        return out[-limit:]

    # ---- the export jobs --------------------------------------------------
    def _exports_dir(self):
        return os.path.join(self.root, "exports")

    def save_export(self, job):
        """Remember an export job (it may outlive the process)."""
        with self.lock:
            try:
                os.makedirs(self._exports_dir(), exist_ok=True)
                path = os.path.join(self._exports_dir(), str(job.get("id") or "") + ".json")
                tmp = path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(job, f, ensure_ascii=False, indent=2)
                os.replace(tmp, path)
            except OSError:
                pass

    def load_export(self, job_id):
        """An export job (None when there is none)."""
        if not job_id or "/" in job_id or "\\" in job_id or job_id.startswith("."):
            return None
        try:
            with open(os.path.join(self._exports_dir(), job_id + ".json"),
                      encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def export_file(self, job_id):
        """Where the bytes of an export job are."""
        return os.path.join(self._exports_dir(), str(job_id) + ".bin")

    # ---- the projects and their keys -------------------------------------
    def _projects_path(self):
        return os.path.join(self.root, "projects.json")

    def project_key(self, workspace_id, tenant_id="", identity=""):
        """The key of a project (a workspace) for a tenant. It is minted on
        first use.

        The key is the credential: a caller that presents it may work on that
        project - its tasks, its events, its files. A key of another project
        gets nothing (403). Without a key the caller is the local operator,
        which is what the web page on the same machine is.

        A project may have **several** keys: one per tenant (and per person
        inside that tenant). The key says **who** is asking; the tenant says
        **whose** work it is. The same key for the same tenant and the same
        person is handed out again - a caller that lost its key asks the
        operator, it does not mint a new one every time.
        """
        if not workspace_id:
            return ""
        tenant_id = str(tenant_id or "").strip()
        identity = str(identity or "").strip()
        with self.lock:
            projects = self._read_projects()
            entry = projects.setdefault(workspace_id, {"keys": []})
            for one in entry.get("keys") or []:
                if (one.get("tenant_id") or "") == tenant_id and \
                        (one.get("identity") or "") == identity:
                    return one.get("key") or ""
            key = base64.b32encode(os.urandom(20)).decode().strip("=")
            entry.setdefault("keys", []).append({
                "key": key,
                "tenant_id": tenant_id,
                "identity": identity,
                "created_at": now_iso(),
            })
            self._write_projects(projects)
            return key

    def project_of_key(self, key):
        """The project a key belongs to ("" when the key is unknown)."""
        return self._key_entry(key, "workspace_id")

    def tenant_of_key(self, key):
        """The tenant a key belongs to ("" when the key has none)."""
        return self._key_entry(key, "tenant_id")

    def identity_of_key(self, key):
        """Who is asking with this key ("" when the key says nothing)."""
        return self._key_entry(key, "identity")

    def _key_entry(self, key, field):
        if not key:
            return ""
        with self.lock:
            for workspace_id, entry in self._read_projects().items():
                for one in entry.get("keys") or []:
                    if one.get("key") != key:
                        continue
                    if field == "workspace_id":
                        return workspace_id
                    return str(one.get(field) or "")
        return ""

    def projects(self):
        with self.lock:
            return dict(self._read_projects())

    def _read_projects(self):
        try:
            with open(self._projects_path(), encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                return dict((workspace_id, self._normalize_project(entry))
                            for workspace_id, entry in loaded.items())
        except (OSError, ValueError):
            pass
        return {}

    @staticmethod
    def _normalize_project(entry):
        """A project entry, whatever shape it was written in.

        The old shape was one key per project; a project may now have several
        keys (one per tenant). An old file is read as one key without a
        tenant - nobody loses a key by upgrading.
        """
        if not isinstance(entry, dict):
            return {"keys": []}
        keys = entry.get("keys")
        if isinstance(keys, list):
            return {"keys": [k for k in keys if isinstance(k, dict) and k.get("key")]}
        key = entry.get("key")
        if key:
            return {"keys": [{"key": key, "tenant_id": "", "identity": ""}]}
        return {"keys": []}

    def _write_projects(self, projects):
        try:
            fd, tmp = tempfile.mkstemp(dir=self.root, prefix=".projects-")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(projects, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._projects_path())
        except OSError:
            pass

    # ---- idempotency ---------------------------------------------------
    def find_by_idempotency(self, key, workspace_id=None):
        """The task a repeated submission with the same key must return."""
        if not key:
            return None
        for task in self.list(workspace_id=workspace_id):
            if task.get("idempotency_key") == key:
                return task
        return None

    def create(self, task):
        with self.lock:
            self._write(task)
        return task

    def load(self, workspace_id, task_id):
        # One ledger, one directory: the task ids are unique, so the workspace a
        # task works in is a field of the record, not a directory.
        with self.lock:
            return self._load_unlocked(task_id)

    def _load_unlocked(self, task_id):
        """One record, without the lock.

        The records are written atomically (a temporary file that is renamed
        over the old one), so a reader never sees half a record: reading
        without the lock is safe, and it keeps a long scan of the directory
        from blocking every other thread of the server.
        """
        if not task_id or not isinstance(task_id, str):
            return None
        try:
            with open(self._path(task_id), encoding="utf-8") as f:
                loaded = json.load(f)
        except (OSError, ValueError):
            return None
        if not isinstance(loaded, dict) or not loaded.get("id"):
            # projects.json and friends live in the same directory; they are not
            # tasks.
            return None
        return loaded

    def save(self, task):
        with self.lock:
            self._write(task)

    def _write(self, task):
        os.makedirs(self.root, exist_ok=True)
        path = self._path(task.get("id"))
        # Atomic: a reader (the API, the scheduler) never sees half a record.
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".task-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(task, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def update(self, workspace_id, task_id, fn):
        """Read-modify-write one task under the lock."""
        with self.lock:
            task = self.load(workspace_id, task_id)
            if task is None:
                return None
            fn(task)
            task["updated_at"] = now_iso()
            self._write(task)
            return task

    def list(self, workspace_id=None, status=None, created_from="", created_to="",
             tag="", business_id="", page=0, page_size=0):
        """The tasks, newest first, with the filters the history view needs.

        The filters beyond status are the ones a business side asks for: a time
        range, a tag, a business id - the tag and the business id live in the
        task's meta, where the submitter put them.
        """
        out = []
        # The scan happens without the lock: it reads every record of the
        # directory, and holding the lock for that would keep the whole
        # server (the API, the scheduler, the event log) waiting for it.
        # The records are written atomically, so a reader is safe here.
        try:
            names = os.listdir(self.root)
        except OSError:
            names = []
        for name in names:
            if not name.endswith(".json"):
                continue
            task = self._load_unlocked(name[:-5])
            if task is None:
                continue
            if not isinstance(task, dict) or not task.get("id"):
                # Not a task record (projects.json lives here too).
                continue
            if workspace_id and task.get("workspace_id") != workspace_id:
                continue
            if status and task.get("status") != status:
                continue
            created = task.get("created_at") or ""
            if created_from and created < created_from:
                continue
            if created_to and created > created_to:
                continue
            meta = task.get("meta") or {}
            if tag and tag not in (meta.get("tags") or []):
                continue
            if business_id and str(meta.get("business_id") or "") != str(business_id):
                continue
            out.append(task)
        # Newest first: the history view is a list of what just happened.
        out.sort(key=lambda t: (t.get("created_at") or "", t.get("id") or ""), reverse=True)
        total = len(out)
        if page_size and page_size > 0:
            page = max(1, int(page or 1))
            start = (page - 1) * page_size
            out = out[start:start + page_size]
        return out

    def delete(self, workspace_id, task_id):
        """The record goes, and its log goes with it: a file nobody can reach
        any more is only litter."""
        with self.lock:
            try:
                os.unlink(self._path(task_id))
            except OSError:
                return False
            try:
                os.unlink(self._events_path(task_id))
            except OSError:
                pass
            return True

    def counts(self, workspace_id=None):
        counts = {QUEUED: 0, RUNNING: 0, WAITING: 0, DONE: 0, FAILED: 0, CANCELLED: 0}
        for task in self.list(workspace_id):
            status = task.get("status")
            if status in counts:
                counts[status] += 1
        return counts


class Events:
    """The queue's own event stream: who wants to know, gets told.

    Every event is also written to the task's log: a broadcast alone leaves
    nothing behind when the process restarts, and a task's history has to
    survive that.
    """

    def __init__(self, store=None):
        self.lock = threading.Lock()
        self.subscribers = []
        self.store = store

    def subscribe(self):
        queue = []
        state = {"alive": True}

        def listener(event):
            with self.lock:
                queue.append(event)

        with self.lock:
            self.subscribers.append(listener)
        return queue, state, listener

    def unsubscribe(self, listener):
        with self.lock:
            if listener in self.subscribers:
                self.subscribers.remove(listener)

    def publish(self, event):
        # Write it down first: the log is the history, the broadcast is only
        # for the clients that happen to be listening.
        if self.store is not None:
            task = event.get("task") or {}
            task_id = task.get("id") or ""
            if task_id:
                payload = {k: v for k, v in event.items() if k != "task"}
                self.store.record_event(task_id, event.get("type") or "event", payload)
        with self.lock:
            listeners = list(self.subscribers)
        for listener in listeners:
            try:
                listener(event)
            except Exception:
                pass


class Engine:
    """The scheduler: what to work on, and when."""

    def __init__(self, store, agent, events, args, trigger_store=None, thread_writer=None,
                 thread_file=None, decorate_event=None, plan_store=None):
        self.store = store
        self.agent = agent
        self.events = events
        self.args = args
        # The threads (the conversations a task belongs to). The scheduler
        # needs them: the next task of a thread has to know what happened
        # before it.
        self.triggers = trigger_store
        # Called after a thread changed: the queue writes the full record
        # into the workspace so the model can read the older part on demand.
        self.thread_writer = thread_writer
        # The path of the thread's record, relative to the workspace.
        self.thread_file = thread_file or (lambda workspace_id, thread_id: "")
        # Adds the thread (its title) to an event before it goes out: the
        # router says things about conversations, not only about task ids.
        self.decorate_event = decorate_event or (lambda event: event)
        # The plans (the complex jobs that were split into steps).
        self.plan_store = plan_store
        # The provider and model the queued work runs on: (provider, model,
        # read_at). It comes from provider.yaml (`task_model:`) and is read
        # again from time to time, so a changed file takes effect without
        # restarting the queue.
        self._task_model = ("", "", 0.0)
        # The budget the agent imposes on one turn: (limits, read_at). It comes
        # from the agent (the mechanism layer) - the queue does not invent it.
        self._machine_limits = ({}, 0.0)
        # The plans whose summary is being made right now: one plan is
        # summarized once, not once per round.
        self.summarizing = set()
        self.running = {}     # task id -> {"thread":..., "workspace_id":...}
        self._quota = (None, None, 0.0)   # (percent, window, read_at)
        # Does the agent on the other end know about **turn handles** (the
        # 0.19.0 interface: submit a turn, get an id, follow it)? The answer
        # is asked once and remembered: an agent that does not know the
        # interface still works, it just gets the old way (one stream per
        # task, held to the end).
        self._agent_handles = None

    # ---- the quota gate ---------------------------------------------------
    def quota_left(self, provider, fresh=False):
        """(percent_left, window_name) - (None, None) when there is no quota.

        The queue polls every second now (so a task waits at most that long),
        and the quota endpoint is a real HTTP call to the provider: asking it
        every second would be rude and slow. The answer is therefore cached
        for --quota-interval seconds.
        """
        now = time.time()
        if not fresh and self._quota[2] and now - self._quota[2] < self.args.quota_interval:
            return self._quota[0], self._quota[1]
        try:
            quota = self.agent.usage(provider)
        except AgentError as exc:
            if exc.status == 400:
                log("provider %r does not report a quota (%s)" % (provider, exc.message))
            else:
                log("cannot read the quota of %r: %s" % (provider, exc))
            self._quota = (None, None, now)
            return None, None
        window = self.args.window
        block = quota.get(window) or quota.get("weekly") or {}
        value = block.get("remaining_percent")
        if value is None:
            self._quota = (None, None, now)
            return None, None
        try:
            percent = float(value)
        except (TypeError, ValueError):
            self._quota = (None, None, now)
            return None, None
        self._quota = (percent, window, now)
        return percent, window

    # ---- which provider and model the work runs on -------------------------
    def task_model(self, refresh=False):
        """(provider, model) - what `task_model:` in provider.yaml says.

        The agent owns provider.yaml, so the queue asks the agent
        (GET /api/health). When the agent cannot answer - it is down, or it
        is an old build that does not know the key - the queue reads the one
        line itself.
        """
        now = time.time()
        if not refresh and self._task_model[2] and now - self._task_model[2] < 60.0:
            return self._task_model[0], self._task_model[1]
        provider, model = "", ""
        try:
            health = self.agent.health()
            provider, model = split_task_model(health.get("task_model") or "")
        except (AgentError, AttributeError):
            provider, model = "", ""
        if not provider:
            provider, model = read_task_model_from_file(self.provider_yaml_path())
        if (provider, model) != (self._task_model[0], self._task_model[1]):
            log("task_model: %s" % (provider + "+" + model if provider else "(none)"))
        self._task_model = (provider, model, now)
        return provider, model

    def machine_limits(self, refresh=False):
        """The budget the agent imposes on one turn (the mechanism layer's word).

        The agent decides what a turn may cost at most; the queue only hands
        out budgets that fit inside it. The answer is cached for a minute -
        it changes when the settings change, not every second.
        """
        now = time.time()
        if not refresh and self._machine_limits[1] and now - self._machine_limits[1] < 60.0:
            return dict(self._machine_limits[0])
        limits = {}
        try:
            limits = self.agent.limits()
        except (AgentError, AttributeError):
            limits = {}
        self._machine_limits = (dict(limits), now)
        return dict(limits)

    def effective_limits(self, task, provider=None):
        """The budget this task runs with.

        The task's own budget if it has one, otherwise the agent's. The agent
        keeps the final word either way: a request may only tighten a ceiling,
        never raise it.

        On top of that: what is left of the provider's quota. The quota is a
        percentage, and a percentage cannot be turned into tokens on its own -
        the operator says what a full quota is worth (`--quota-tokens`) and how
        much of what is left one task may take (`--quota-share`). Without that
        number nothing is converted, and the quota stays what it was: a gate
        in front of the work.
        """
        own = task.get("limits") or {}
        limits = dict(own) if isinstance(own, dict) and own else dict(self.machine_limits())
        quota_budget = self.quota_token_budget(provider)
        if quota_budget:
            current = limits.get("max_total_tokens")
            try:
                current = int(current or 0)
            except (TypeError, ValueError):
                current = 0
            if current <= 0 or quota_budget < current:
                limits["max_total_tokens"] = quota_budget
        return limits

    def quota_token_budget(self, provider=None):
        """How many tokens one task may spend, from what is left of the quota.

        0 when the operator did not say what the quota is worth (or the
        provider does not report one): then the quota is only the gate it
        always was.
        """
        total = float(getattr(self.args, "quota_tokens", 0) or 0)
        if total <= 0:
            return 0
        share = float(getattr(self.args, "quota_share", 0.5) or 0)
        if share <= 0:
            return 0
        percent, _ = self.quota_left(provider or self.effective_provider())
        if percent is None or percent < 0:
            return 0
        return int(total * (percent / 100.0) * share)

    def provider_yaml_path(self):
        """Where provider.yaml is (the agent's tea home)."""
        tea_home = os.environ.get("TEA_HOME") or os.path.expanduser("~/.tea_agent")
        try:
            health = self.agent.health()
            if health.get("tea_home"):
                tea_home = health["tea_home"]
        except (AgentError, AttributeError):
            pass
        return os.path.join(tea_home, "provider.yaml")

    def effective_provider(self):
        """The provider the queued work runs on.

        The command line wins (the operator who starts the service said so),
        then provider.yaml's `task_model:`, then the built-in default.
        """
        if self.args.provider:
            return self.args.provider
        provider, _ = self.task_model()
        return provider or DEFAULT_PROVIDER

    def effective_model(self):
        """The model the queued work uses (empty = the provider's own first)."""
        _, model = self.task_model()
        return model

    # ---- how many tasks may run at the same time --------------------------
    def running_tasks(self):
        """The tasks that are running right now - ours and everybody else's.

        Counted from the *records*, not from this process's threads: a task
        that is still running when the next round comes up must keep its slot.
        A record that says "running" but was not touched for a while is a
        leftover from a crashed process - it does not hold a slot for ever.

        The threads of THIS process count as well, even when their record
        already says "failed": the watchdog can mark a stuck task failed
        while its worker is still winding down, and the project's slot must
        not be handed out twice.
        """
        mine = set(self.running)
        out = []
        for task in self.store.list(status=RUNNING):
            if task.get("id") not in mine:
                stamp = parse_iso(task.get("updated_at") or task.get("started_at") or "")
                if stamp is not None and time.time() - stamp > self.args.stale_after:
                    continue
            out.append(task)
        for task_id, info in list(self.running.items()):
            if task_id in mine and any(t.get("id") == task_id for t in out):
                continue
            out.append({"id": task_id,
                        "workspace_id": (info or {}).get("workspace_id", ""),
                        "status": RUNNING,
                        "note": "the worker is still winding down"})
        return out

    def running_count(self, workspace_id=None):
        """How many tasks are running (in one project, or in all of them)."""
        tasks = self.running_tasks()
        if workspace_id:
            tasks = [t for t in tasks if t.get("workspace_id") == workspace_id]
        return len(tasks)

    @staticmethod
    def _running_in(running, workspace_id):
        """Is this project working on something right now?"""
        for one in running:
            if one.get("workspace_id") == workspace_id:
                return True
        return False

    # ---- one round --------------------------------------------------------
    def round(self):
        # A paused queue starts nothing. The pause is an operator decision
        # ("stop working until I say so"), so it also survives a restart of
        # this service - see Store.set_paused.
        state = self.store.state()
        if state.get("paused"):
            log("the queue is paused: %s" % (state.get("reason") or "no reason given"))
            return 0
        # The questions nobody answered in time, and the records that are too
        # old to keep. Both are housekeeping, both are cheap.
        self.expire_interactions()
        now = time.time()
        cleanup_every = float(getattr(self.args, "cleanup_interval", 3600) or 3600)
        if now - getattr(self, "_last_cleanup", 0.0) > cleanup_every:
            self._last_cleanup = now
            self.cleanup_old()
        provider = self.effective_provider()
        queued = self.store.list(status=QUEUED)
        if not queued:
            # Nothing to do: do not call the provider either. The quota is
            # only interesting when there is work waiting for it.
            log("nothing queued")
            return 0

        # The quota is read fresh whenever there is work: the queue polls
        # every --interval seconds, and a task that waits for a quota change
        # must not be kept waiting by a cached answer.
        left, window = self.quota_left(provider, fresh=True)
        if left is None:
            if not self.args.allow_without_quota:
                log("no quota to look at - not starting anything "
                    "(use --allow-without-quota to work anyway)")
                return 0
            log("working anyway (--allow-without-quota)")
        else:
            if left < self.args.min_remaining:
                log("quota of %r: %s%% left (need %d%%) - not starting anything"
                    % (provider, left, self.args.min_remaining))
                return 0
            log("quota of %r: %s%% left - going to work" % (provider, left))

        # The queue is serial *per project*: a project (a workspace, which is
        # what an "agent" is here) works on one task at a time, while other
        # projects may run in parallel. --max-concurrent caps the total; 0
        # means "only the per-project rule".
        if self.args.max_concurrent and self.running_count() >= self.args.max_concurrent:
            log("%d task(s) already running - waiting" % self.running_count())
            return 0

        # Highest priority first, then the queue order (FIFO). A task that
        # asked for a different provider is left alone.
        queued.sort(key=lambda t: (-int(t.get("priority") or 0),
                                   t.get("created_at") or "", t.get("id") or ""))

        # The tasks whose dependency will never be done are stopped before
        # anything is started: a task that waits for a step that failed would
        # wait for ever.
        self.sweep_dependencies()

        started = 0
        now = time.time()
        # The tasks that are running are counted ONCE for this round. Asking
        # for every queued task meant one full scan of the ledger per task:
        # a queue of a few hundred tasks took longer than one round, so the
        # scheduler never got to rest and the process burned a whole core.
        running = self.running_tasks()
        busy = 0
        for task in queued:
            if started >= self.args.max_per_round:
                break
            if self.args.max_concurrent and len(running) >= self.args.max_concurrent:
                break
            # "Not before": the task waits for its time - and it does not hold
            # the queue: the tasks behind it may go first.
            not_before = parse_iso(task.get("not_before") or "")
            if not_before is not None and not_before > now:
                continue
            # One task at a time per project.
            if self._running_in(running, task.get("workspace_id")):
                # The project is working on something else: the task keeps its
                # place and waits its turn.
                busy += 1
                continue
            if task.get("kind") == "plan":
                # A plan is a container: the queue does not run it, its
                # children are the work.
                continue
            state, reason = self.dependency_state(task)
            if state == "waiting":
                # It waits for a step that is not done yet: it keeps its
                # place in the queue.
                busy += 1
                continue
            if state == "dead":
                # The sweep stops it; starting it would be wrong.
                continue
            wanted = task.get("provider")
            if wanted and wanted != provider:
                log("  task %s wants provider %r, not %r - leaving it alone"
                    % (task.get("id"), wanted, provider))
                continue
            if self.start_task(task, provider):
                started += 1
                # It holds a slot from now on: the next task of the same
                # project has to wait for it.
                running.append(task)
        if busy:
            log("  %d task(s) wait: their project is busy" % busy)
        return started

    # ---- the tasks this process inherited --------------------------------
    def recover_stale_tasks(self, min_age=None):
        """A task that was running when this process started.

        Two cases, and they are very different:

        * the task has a **turn id** and the agent still knows that turn: the
          work did not die with this process - it is still running on the
          agent's side. The queue **takes it over** (follows the turn again)
          instead of declaring the task dead. This is the whole point of the
          turn handle: a queue that restarts no longer kills work that is
          still going on;
        * there is no turn id (a task from before the handles existed), or the
          agent does not know it any more: the work really is lost, so the
          task is marked failed with a reason that says what happened, and it
          can be queued again.

        Only tasks that have not been touched for a minute are recovered: another
        task server working on the same records would otherwise have its task
        pulled out from under it.
        """
        cutoff = time.time() - (self.args.stale_after if min_age is None else min_age)
        recovered = []
        for task in self.store.list(status=RUNNING):
            stamp = parse_iso(task.get("updated_at") or task.get("started_at") or "")
            if stamp is not None and stamp > cutoff:
                continue                    # somebody is still working on it
            workspace_id = task.get("workspace_id")
            task_id = task.get("id")
            turn_id = str(task.get("turn_id") or "")
            if turn_id:
                status = self._turn_state(turn_id)
                if status is not None:
                    name = str(status.get("state") or "")
                    if name == "running":
                        # The work is still going on: take it over.
                        log("  task %s was running when the server started - "
                            "turn %s is still running, taking it over" % (task_id, turn_id))
                        self._take_over(workspace_id, task_id, turn_id)
                        continue
                    # The turn is over. Its verdict is the task's verdict -
                    # and it publishes its own event, so it does not belong
                    # in the list of tasks that failed.
                    log("  task %s was running when the server started - turn %s is %s"
                        % (task_id, turn_id, name))
                    self._finalize_from_status(
                        workspace_id, task_id, status,
                        {"answer": "", "report": {"needs_confirmation": False,
                                                  "question": ""}, "progress": 0})
                    continue
            # The turn is gone. Is the **session** still there? Then the work
            # *could* go on from there: the conversation is the checkpoint, and
            # a new turn in the same session continues it.
            #
            # But only when the operator asked for it (--resume-on-restart).
            # The default is to let the work die with a clear reason: a turn
            # that was cut off half way may already have done half of its
            # work, and running it again without being told is worse than
            # stopping.
            checkpoint = task.get("checkpoint") or {}
            session_id = str(task.get("session_id") or checkpoint.get("session_id") or "")
            resume = bool(getattr(self.args, "resume_on_restart", False))
            if resume and session_id and checkpoint.get("turn_id"):
                self.store.update(workspace_id, task_id, lambda t: t.update({
                    "status": QUEUED,
                    "finished_at": "",
                    "error": "",
                    "needs_confirmation": False,
                    "question": "",
                    "pending_message": "继续上一个任务：%s" % (t.get("prompt") or ""),
                    "note": "resumed from the checkpoint",
                }))
                log("  task %s: the turn is gone but the session is - "
                    "the task goes back into the queue" % task_id)
                continue
            self.store.update(workspace_id, task_id, lambda t: t.update({
                "status": FAILED,
                "finished_at": now_iso(),
                "error": "the task server restarted while this task was running; "
                         "queue it again if the work still has to be done",
            }))
            log("  task %s was running when the server started - marked failed" % task_id)
            recovered.append(self.store.load(workspace_id, task_id))
        for task in recovered:
            if task is not None:
                self._publish({"type": "task_failed", "task": task})
        return recovered

    def _turn_state(self, turn_id):
        """How a turn is going, or None when the agent does not know it."""
        try:
            return self.agent.turn_status(turn_id)
        except AgentError as exc:
            if getattr(exc, "status", 0) == 404:
                return None
            log("  the state of turn %s could not be read (%s)" % (turn_id, exc))
            return None
        except Exception as exc:  # noqa: BLE001
            log("  the state of turn %s could not be read (%s)" % (turn_id, exc))
            return None

    def _take_over(self, workspace_id, task_id, turn_id):
        """Follow a turn that outlived the process that started it."""
        if task_id in self.running:
            return

        def work():
            state = {"answer": "", "progress": 0,
                     "report": {"needs_confirmation": False, "question": ""}}
            try:
                self._follow_turn(workspace_id, task_id, turn_id, state)
            except Exception as exc:  # noqa: BLE001 - the queue must not die
                log("  task %s: taking turn %s over failed (%s: %s)"
                    % (task_id, turn_id, type(exc).__name__, exc))
                self._finalize(workspace_id, task_id, FAILED, {
                    "error": "the task could not be taken over: %s" % exc,
                    "finished_at": now_iso()})
                self._publish({"type": "task_failed",
                               "task": self.store.load(workspace_id, task_id)})
            finally:
                self.running.pop(task_id, None)

        thread = threading.Thread(target=work, daemon=True)
        self.running[task_id] = {"thread": thread, "workspace_id": workspace_id}
        thread.start()

    def expire_interactions(self):
        """The questions nobody answered in time.

        A task that waits for a person waits with a deadline: after it, the
        task fails (nobody is going to do that work) and the record says why.
        """
        timeout = float(getattr(self.args, "interaction_timeout", 0) or 0)
        if timeout <= 0:
            return []
        expired = []
        for task in self.store.list(status=WAITING):
            interaction = task.get("interaction") or {}
            expires = parse_iso(interaction.get("expires_at") or "")
            if not expires or expires > time.time():
                continue
            workspace_id = task.get("workspace_id") or ""
            task_id = task.get("id") or ""
            written = self._finalize(workspace_id, task_id, FAILED, {
                "error": "nobody answered the question in time (%.0f s)" % timeout,
                "stop_reason": "interaction_timeout",
                "finished_at": now_iso(),
                "note": "the question timed out",
            })
            if written is not None:
                self._publish({"type": "task_failed", "task": written})
                expired.append(task_id)
                log("  task %s: nobody answered in time - failed" % task_id)
        return expired

    def cleanup_old(self, days=None):
        """The task records that are older than the retention time.

        A queue that runs for months fills up with finished tasks. The
        retention time says how long they are kept; 0 = keep everything.
        """
        keep_days = float(days if days is not None
                          else (getattr(self.args, "retention_days", 0) or 0))
        if keep_days <= 0:
            return []
        cutoff = time.time() - keep_days * 86400.0
        removed = []
        for task in self.store.list():
            if task.get("status") == RUNNING:
                continue
            created = parse_iso(task.get("created_at") or "")
            if created is None or created > cutoff:
                continue
            task_id = task.get("id") or ""
            if self.store.delete(task.get("workspace_id") or "", task_id):
                removed.append(task_id)
        if removed:
            log("  %d old task(s) removed (retention: %.0f days)" % (len(removed), keep_days))
        return removed

    def _publish(self, event):
        """Publish an event, with the thread it belongs to attached."""
        self.events.publish(self.decorate_event(event))

    # ---- the plans ---------------------------------------------------------
    def check_plans(self):
        """Drive the plans: count the steps, and summarize when they are done.

        The summary is made here, in the queue, and not in the router: the
        queue is what knows when the last step is done, and a plan must not
        hang because the router happens to be down. The router still speaks
        the result - the task_done event is what it turns into words.
        """
        if self.plan_store is None:
            return
        for plan in self.plan_store.list_plans():
            try:
                self.check_one_plan(plan)
            except Exception as exc:  # noqa: BLE001 - one bad plan must not stop the rest
                log("cannot check the plan %s: %s: %s"
                    % (plan.get("id"), type(exc).__name__, exc))

    def check_one_plan(self, plan):
        parent_id = plan.get("parent_id") or ""
        workspace_id = plan.get("workspace_id") or ""
        if not parent_id:
            return
        parent = self.store.load(workspace_id, parent_id)
        if parent is None or parent.get("status") in TERMINAL:
            return
        children = []
        for step in plan.get("steps") or []:
            child = self.store.load(workspace_id, step.get("task_id") or "")
            if child is None:
                return          # a step that is not there yet: wait
            children.append((step, child))
        if not children:
            return
        done = [c for _, c in children if c.get("status") == DONE]
        failed = [c for _, c in children if c.get("status") in (FAILED, CANCELLED)]
        busy = [c for _, c in children if c.get("status") not in TERMINAL]
        total = len(children)
        if busy:
            # Still working: the parent shows how far the plan is.
            progress = int(100 * len(done) / total)
            note = "计划：%d/%d 步完成" % (len(done), total)
            if parent.get("progress") != progress or parent.get("note") != note:
                written = self.store.update(workspace_id, parent_id, lambda t: t.update({
                    "progress": progress, "note": note}))
                if written is not None:
                    self._publish({"type": "task_updated", "task": written})
            return
        if failed:
            titles = "、".join((c.get("title") or c.get("id") or "") for c in failed)
            reason = "有 %d 步没成功：%s" % (len(failed), titles)
            written = self._finalize(workspace_id, parent_id, FAILED, {
                "error": reason,
                "progress": 100,
                "finished_at": now_iso(),
                "note": reason[:80],
            })
            if written is not None:
                self._publish({"type": "task_failed", "task": written})
                self.note_thread(written, "error", reason)
            plan["status"] = "failed"
            self.plan_store.save(plan)
            return
        # Every step is done: the plan is summarized - once.
        if parent_id in self.summarizing:
            return
        self.summarizing.add(parent_id)
        thread = threading.Thread(target=self._summarize_plan, args=(plan,), daemon=True)
        thread.start()

    def aggregate_prompt(self, plan):
        """What the model is asked to turn the steps into one result."""
        workspace_id = plan.get("workspace_id") or ""
        parts = []
        for step in plan.get("steps") or []:
            child = self.store.load(workspace_id, step.get("task_id") or "") or {}
            title = step.get("title") or step.get("key") or ""
            text = str(child.get("result") or child.get("error") or "").strip()
            parts.append("【%s】\n%s" % (title, text or "（这一步没有留下结果）"))
        goal = plan.get("goal") or plan.get("title") or ""
        ask = plan.get("aggregate_prompt") or "把这些结果汇总成一份完整、可以直接交付的结果。"
        return ("【任务目标】\n%s\n\n【要做的汇总】\n%s\n\n【各步骤的结果】\n%s"
                % (goal, ask, "\n\n".join(parts)))

    def _summarize_plan(self, plan):
        """One model call turns the steps into the plan's result."""
        parent_id = plan.get("parent_id") or ""
        workspace_id = plan.get("workspace_id") or ""
        try:
            parent = self.store.load(workspace_id, parent_id)
            if parent is None or parent.get("status") in TERMINAL:
                return
            prompt = self.aggregate_prompt(plan)
            budget = int(getattr(self.args, "aggregate_max_tokens", 4000) or 4000)
            answer = {}
            text = ""
            for _ in range(3):
                try:
                    answer = self.agent.llm_chat(
                        prompt=prompt, system=AGGREGATE_SYSTEM,
                        provider=plan.get("provider") or self.effective_provider() or None,
                        model=plan.get("model") or None,
                        temperature=0.1, max_tokens=budget,
                        timeout=getattr(self.args, "aggregate_timeout", 180))
                except AgentError as exc:
                    log("the model did not summarize the plan %s: %s" % (plan.get("id"), exc))
                    answer = {}
                # Only the answer is the answer: the reasoning is the model
                # thinking out loud, and it must never become the result.
                text = str((answer or {}).get("content") or "").strip()
                if text:
                    break
                if (answer or {}).get("finish_reason") == "length":
                    # The answer was cut off: the model thinks first, and the
                    # thinking ate the budget. Give it more.
                    budget = min(budget * 2, 16000)
                    log("the summary of the plan %s was cut off - retrying with %d tokens"
                        % (plan.get("id"), budget))
                    continue
                break
            if not text:
                # No answer: try again in the next round. After a few tries
                # the plan is failed - a plan that cannot be summarized is
                # not a plan that hangs for ever.
                attempts = int(plan.get("attempts") or 0) + 1
                plan["attempts"] = attempts
                self.plan_store.save(plan)
                if attempts >= 3:
                    written = self._finalize(workspace_id, parent_id, FAILED, {
                        "error": "汇总没成功（试了 %d 次）" % attempts,
                        "progress": 100,
                        "finished_at": now_iso(),
                    })
                    if written is not None:
                        self._publish({"type": "task_failed", "task": written})
                    plan["status"] = "failed"
                    self.plan_store.save(plan)
                return
            steps = plan.get("steps") or []
            written = self._finalize(workspace_id, parent_id, DONE, {
                "result": text,
                "progress": 100,
                "finished_at": now_iso(),
                "note": "计划完成：%d 步" % len(steps),
            })
            if written is not None:
                self._publish({"type": "task_done", "task": written})
                self.note_thread(written, "result", text[:1000])
            plan["status"] = "done"
            plan["attempts"] = 0
            self.plan_store.save(plan)
            log("plan %s is done: %d step(s)" % (plan.get("id"), len(steps)))
        finally:
            self.summarizing.discard(parent_id)

    # ---- the final word about a task --------------------------------------
    def _finalize(self, workspace_id, task_id, status, fields=None):
        """Write the final status of a task - unless somebody else already did.

        A watchdog can mark a stuck task failed while its worker is still
        streaming; when that worker's stream ends it would overwrite the
        verdict with "done" and the record would claim a success that never
        happened. The first verdict wins: a task that is already finished
        stays finished.

        Returns the written record, or None when the task was already
        finished (then the caller must not publish anything either).
        """
        task = self.store.load(workspace_id, task_id)
        if task is None:
            return None
        if task.get("status") in TERMINAL:
            return None

        def apply(t):
            if callable(fields):
                fields(t)
            elif isinstance(fields, dict):
                t.update(fields)
            t["status"] = status

        self.store.update(workspace_id, task_id, apply)
        return self.store.load(workspace_id, task_id)

    def fail_task(self, workspace_id, task_id, reason, actor="watchdog"):
        """Mark a running task failed - for the watchdog.

        The worker of a stuck task may still be streaming: the verdict is
        written first (so the worker's late "done" cannot overwrite it) and
        the turn is stopped afterwards, so the slot is really free and the
        provider does not keep burning tokens on a task nobody watches.
        """
        task = self.store.load(workspace_id, task_id)
        if task is None:
            return None
        if task.get("status") in TERMINAL:
            return None
        reason = str(reason or "").strip() or "marked failed"
        written = self._finalize(workspace_id, task_id, FAILED, {
            "error": reason,
            "finished_at": now_iso(),
            "note": "failed: " + reason[:80],
        })
        if written is None:
            return None
        # Stop **this** turn: the handle says exactly which round to stop, so
        # another round on the same session (another task, another window) is
        # not hit by accident. Without a handle (an old record) the session
        # is stopped as before.
        turn_id = str(task.get("turn_id") or "")
        session_id = task.get("session_id")
        if turn_id or session_id:
            try:
                if turn_id:
                    self.agent.stop_turn(turn_id)
                else:
                    self.agent.stop(session_id)
            except AgentError as exc:
                log("could not stop the turn of %s: %s" % (task_id, exc))
        self._publish({"type": "task_failed", "task": written})
        self.store.audit("fail", task_id, {"reason": reason, "actor": actor})
        return written

    # ---- the steps that wait for each other --------------------------------
    def dependency_state(self, task):
        """What the dependencies of a task say.

        ("ready", "")          - it may start now
        ("waiting", reason)    - it waits for steps that are not done yet
        ("dead", reason)       - a step it waits for failed: it will never run

        The reasons are for the record: they say why a task is still in the
        queue, or why it was stopped.
        """
        deps = task.get("depends_on") or []
        if not deps:
            return "ready", ""
        waiting = []
        dead = []
        for dep_id in deps:
            dep = self.store.load(task.get("workspace_id"), dep_id)
            if dep is None:
                dead.append("%s（记录不见了）" % dep_id)
                continue
            status = dep.get("status")
            if status == DONE:
                continue
            if status in TERMINAL:
                dead.append("%s（%s）" % (dep.get("title") or dep_id, status))
            else:
                waiting.append(dep.get("title") or dep_id)
        if dead:
            return "dead", "等的那一步没能完成：%s" % "、".join(dead)
        if waiting:
            return "waiting", "等 %d 个前一步：%s" % (len(waiting), "、".join(waiting))
        return "ready", ""

    def sweep_dependencies(self):
        """Stop the tasks whose dependency will never be done."""
        for task in self.store.list(status=QUEUED):
            if not (task.get("depends_on") or []):
                continue
            state, reason = self.dependency_state(task)
            if state != "dead":
                continue
            written = self._finalize(task.get("workspace_id"), task.get("id"), CANCELLED, {
                "error": reason,
                "finished_at": now_iso(),
                "note": "停下了：%s" % reason[:80],
            })
            if written is not None:
                self._publish({"type": "task_cancelled", "task": written})
                log("  task %s cancelled: %s" % (task.get("id"), reason))

    # ---- running one task -------------------------------------------------
    def start_task(self, task, provider=None):
        """Run a task now: the turn goes through the agent, the record follows."""
        task_id = task.get("id")
        workspace_id = task.get("workspace_id")
        if task_id in self.running:
            log("  task %s is already running here" % task_id)
            return False
        if task.get("kind") == "plan":
            # A plan is a container: its steps are the work. Running the
            # container would ask the model to do the whole thing at once.
            log("  task %s is a plan, not work: it is not run" % task_id)
            return False

        provider = provider or task.get("provider") or self.effective_provider()
        context = self.context_for(task)
        # The budget of this task: its own, or the agent's. The record says
        # which one it ran under, so a task that ran out of room can be
        # answered with "how much did it get?".
        limits = self.effective_limits(task, provider)
        # A task that was waiting for an answer carries it with it: the answer
        # is the next thing said in the task's session.
        pending = task.get("pending_message") or ""
        self.store.update(workspace_id, task_id, lambda t: t.update({
            "status": RUNNING,
            "provider": provider,
            "started_at": now_iso(),
            "finished_at": "",
            "error": "",
            "needs_confirmation": False,
            "question": "",
            "note": "started",
            "pending_message": "",
            "limits": limits,
            # The turn of the previous run: it is over, and its handle must
            # not be inherited - a restart has to take over *this* run.
            "turn_id": "",
            # Which turn of this task this is. The idempotency key is built
            # from it, so a new turn (after an answer, after a retry) is new
            # work while a retry of the same turn is the same work.
            "turn_seq": int(task.get("turn_seq") or 0) + 1,
        }))

        thread = threading.Thread(target=self._run,
                                  args=(workspace_id, task_id, provider, context, pending or None),
                                  daemon=True)
        self.running[task_id] = {"thread": thread, "workspace_id": workspace_id}
        thread.start()
        log("  started task %s (%s)" % (task_id, task.get("title") or task.get("prompt", "")[:40]))
        return True

    def context_for(self, task):
        """What the agent is told about the task: id, title, request.

        And the thread it belongs to: the previous steps of the same
        conversation. Without it every task starts from nothing, and the
        model cannot tell that it has already done half of the work.
        """
        out = task.get("id") or ""
        if task.get("title"):
            out += " (%s)" % task["title"]
        if task.get("prompt"):
            out += ": " + task["prompt"]
        plan_id = task.get("plan_id") or ""
        if plan_id and self.plan_store is not None:
            plan = self.plan_store.load(plan_id)
            if plan:
                out += "\n\n【这是个大计划里的一步】%s\n总目标：%s" % (
                    plan.get("title") or "", plan.get("goal") or "")
        thread_id = task.get("thread_id") or ""
        if thread_id and self.triggers is not None:
            before = self.triggers.context_of(thread_id)
            if before:
                out += "\n\n[这条线索之前的事]\n" + before
            # The full record is a file in the workspace: the model can read
            # the older part of this conversation when it needs it, with the
            # file tool it already has.
            rel = self.thread_file(task.get("workspace_id") or "", thread_id)
            if rel:
                out += ("\n这条线索的完整记录在 %s（相对工作区）；要看更早的事，"
                        "用 toolkit_file 读它。" % rel)
        return out

    def note_thread(self, task, kind, text):
        """Put one step of a task into its thread (the cross-session memory).

        The queue does this itself: it sees every event of every task, and
        the thread belongs to the task records anyway. The router
        (sched/router.py) only compresses what falls out of the window.
        """
        thread_id = (task or {}).get("thread_id") or ""
        if not thread_id or not text or self.triggers is None:
            return
        try:
            self.triggers.add_context(thread_id, kind, text, (task or {}).get("id") or "")
        except OSError:
            return
        if self.thread_writer is not None:
            try:
                self.thread_writer(thread_id)
            except OSError:
                pass

    def _run(self, workspace_id, task_id, provider, context, message=None):
        try:
            task = self.store.load(workspace_id, task_id) or {}
            if (task.get("target") or "") == "fleet":
                self._consume_fleet(workspace_id, task_id, provider, context, message)
            else:
                self._consume(workspace_id, task_id, provider, context, message)
        except AgentError as exc:
            self.store.update(workspace_id, task_id, lambda t: t.update({
                "status": FAILED, "error": str(exc), "finished_at": now_iso()}))
            self._publish({"type": "task_failed", "task": self.store.load(workspace_id, task_id)})
        except Exception as exc:  # noqa: BLE001 - the queue must not die on one task
            self.store.update(workspace_id, task_id, lambda t: t.update({
                "status": FAILED, "error": "%s: %s" % (type(exc).__name__, exc),
                "finished_at": now_iso()}))
            self._publish({"type": "task_failed", "task": self.store.load(workspace_id, task_id)})
        finally:
            self.running.pop(task_id, None)

    # ---- the turn handle: submit now, follow it later ---------------------
    #
    # The old way: one SSE connection per task, held until the turn ends. The
    # connection **was** the task's life: break it and the task was dead,
    # even though the agent went on working on it. The new way: the queue
    # submits the turn and gets an **id** back; the id is what it keeps.

    def agent_supports_handles(self):
        """Does the agent know the turn-handle interface?

        The agent says so itself: `GET /api/capabilities` reports
        `features.async_turns`. An agent that is too old has no such endpoint,
        so its version is the next best thing (0.19.0 is where the handles
        came in). The answer is asked once - the agent does not change its
        mind while the queue runs.
        """
        if self._agent_handles is None:
            handles = False
            version = ""
            try:
                caps = self.agent.capabilities()
                features = caps.get("features") or {}
                handles = bool(features.get("async_turns"))
                version = str(caps.get("version") or "")
            except AgentError as exc:
                if getattr(exc, "status", 0) != 404:
                    log("the agent did not answer with capabilities (%s); "
                        "falling back to its version" % exc)
                version, handles = self._agent_version_handles()
            except Exception as exc:  # noqa: BLE001 - an agent that cannot be asked
                log("the agent could not be asked about its capabilities (%s)" % exc)
                version, handles = self._agent_version_handles()
            self._agent_handles = handles
            log("the agent %s turn handles (version %s)"
                % ("knows" if handles else "does not know", version or "unknown"))
        return self._agent_handles

    def _agent_version_handles(self):
        """(version, has_handles) from `GET /api/health` - the old way."""
        try:
            health = self.agent.health()
        except Exception:  # noqa: BLE001 - an agent that cannot be reached
            return ("", False)
        version = str(health.get("version") or "")
        parts = []
        for piece in version.split(".")[:3]:
            digits = "".join(ch for ch in piece if ch.isdigit())
            parts.append(int(digits) if digits else 0)
        while len(parts) < 3:
            parts.append(0)
        return (version, tuple(parts) >= (0, 19, 0))

    def _follow_turn(self, workspace_id, task_id, turn_id, state):
        """Follow one turn to its verdict.

        The turn lives on the agent's side; this is a subscription to it. When
        the subscription breaks - a proxy, a network hiccup, the agent
        restarting - the turn is **not** lost: we ask the agent how it went
        and pick the stream up again from the last frame we saw. Only a turn
        that really ended ends the task.

        Returns the verdict ("done" / "failed" / "cancelled" / "waiting").
        """
        since = 0
        budget = float(getattr(self.args, "chat_timeout", 3600) or 3600)
        deadline = time.time() + budget
        touched = time.time()
        # How often the record is touched, so another queue instance does not
        # take the task away while it is being worked on. It has to be well
        # inside that instance's window.
        stale_after = float(getattr(self.args, "stale_after", 300) or 300)
        heartbeat = max(5.0, min(30.0, stale_after / 3.0))
        while True:
            try:
                for event in self.agent.turn_events(turn_id, since=since):
                    seq = event.get("seq")
                    if isinstance(seq, int) and seq > since:
                        since = seq
                    # The agent's own view of the turn, on the queue's bus -
                    # before the queue's verdict, so the order is fixed.
                    self._bridge_event(workspace_id, task_id, event)
                    verdict = self._on_event(workspace_id, task_id, event, state)
                    if verdict:
                        return verdict
            except AgentError as exc:
                if getattr(exc, "status", 0) == 404:
                    # The agent does not know this turn any more: it is gone
                    # (the server restarted, or the handle was dropped).
                    log("  the agent does not know turn %s any more" % turn_id)
                    return "failed"
                log("  the turn %s could not be followed (%s); asking again" % (turn_id, exc))
            except Exception as exc:  # noqa: BLE001 - the network breaks
                log("  the stream of turn %s broke (%s: %s); asking again"
                    % (turn_id, type(exc).__name__, exc))
            # The stream ended without a verdict. Ask the agent how it went:
            # the work may well be done already.
            try:
                status = self.agent.turn_status(turn_id)
            except AgentError as exc:
                if getattr(exc, "status", 0) == 404:
                    log("  the agent does not know turn %s any more" % turn_id)
                    return "failed"
                status = None
            except Exception as exc:  # noqa: BLE001
                log("  the state of turn %s could not be read (%s)" % (turn_id, exc))
                status = None
            if status is not None:
                name = str(status.get("state") or "")
                if name == "running":
                    pass                      # still working: keep waiting
                else:
                    return self._finalize_from_status(workspace_id, task_id, status, state)
            if time.time() > deadline:
                # Out of time. The turn is stopped **first**: a task that
                # nobody watches must not go on burning the provider's
                # tokens - that is the orphan this whole change is about.
                try:
                    self.agent.stop_turn(turn_id)
                    log("  turn %s was stopped: the task ran out of time" % turn_id)
                except Exception as exc:  # noqa: BLE001
                    log("  turn %s could not be stopped (%s)" % (turn_id, exc))
                self._finalize(workspace_id, task_id, FAILED, {
                    "error": "the task ran out of time (%d s); the turn was stopped"
                             % int(budget),
                    "stop_reason": "timeout",
                    "finished_at": now_iso()})
                self._publish({"type": "task_failed",
                               "task": self.store.load(workspace_id, task_id)})
                return "failed"
            # Keep the record warm: another queue instance leaves a task
            # alone while it is being touched. The heartbeat has to be well
            # inside the window the other instance uses to decide "nobody is
            # working on this any more".
            if time.time() - touched > heartbeat:
                self.store.update(workspace_id, task_id, lambda t: t.update({}))
                touched = time.time()
            time.sleep(1.0)

    def _finalize_from_status(self, workspace_id, task_id, status, state):
        """Write the verdict of a turn the queue did not see the end of.

        The agent knows how the turn ended; the queue missed the frames (the
        connection broke). The status carries the answer, the usage and the
        reason - enough to write the record honestly.
        """
        name = str(status.get("state") or "")
        answer = str(status.get("result") or "")
        report = state.get("report") or {}
        usage = status.get("usage")
        usage = dict(usage) if isinstance(usage, dict) else {}
        stop_reason = str(status.get("stop_reason") or "")
        if name == "stopped":
            written = self._finalize(workspace_id, task_id, CANCELLED, {
                "stop_reason": stop_reason or "cancelled",
                "finished_at": now_iso()})
            if written is not None:
                self._publish({"type": "task_cancelled", "task": written})
            return "cancelled"
        if name == "failed":
            written = self._finalize(workspace_id, task_id, FAILED, {
                "error": str(status.get("error") or "the turn failed"),
                "stop_reason": "error",
                "finished_at": now_iso()})
            if written is not None:
                self._publish({"type": "task_failed", "task": written})
                self.note_thread(written, "error", str(status.get("error") or "这一轮失败了"))
            return "failed"
        # "done" or "budget": the work is done, whether or not it fits in the
        # budget. A budget stop is not a failure.
        task_status = WAITING if report.get("needs_confirmation") else DONE
        fields = {
            "progress": 100 if task_status == DONE else state.get("progress", 0),
            "result": answer,
            "finished_at": now_iso(),
            "error": "",
            "stop_reason": stop_reason,
            "usage": usage,
            "iterations": int(status.get("iterations") or 0),
            "trajectory_id": str(status.get("trajectory_id") or ""),
        }
        if stop_reason == "budget":
            fields["note"] = "预算用完了（budget）"
        written = self._finalize(workspace_id, task_id, task_status, fields)
        if written is not None:
            self._publish({"type": "task_done" if task_status == DONE
                           else "task_waiting_confirmation", "task": written})
            if task_status == DONE:
                self.note_thread(written, "result", answer)
            else:
                self.note_thread(written, "question", report.get("question") or "")
        return "done" if task_status == DONE else "waiting"

    def _consume(self, workspace_id, task_id, provider, context, message=None):
        task = self.store.load(workspace_id, task_id)
        if task is None:
            return
        # The answer to a question continues the work in the same session; the
        # task's own prompt is what the *first* turn says.
        message = message if message is not None else task.get("prompt", "")
        session_id = task.get("session_id") or ""
        report = {"progress": None, "note": "", "needs_confirmation": False, "question": ""}
        answer = ""
        allow_paths = task.get("allow_paths") or []
        # The model this task asked for; without one the queue's default
        # applies (`task_model:` in provider.yaml).
        model = task.get("model") or self.effective_model()
        # The knobs the task asked for (temperature, max_tokens, ...).
        params = task.get("model_params") or {}
        limits = task.get("limits") or {}
        policy = task.get("policy") or {}
        # What the queue knows about this turn while it runs. It is passed
        # around instead of being a local variable, because the turn can be
        # picked up again by another process (a restarted queue).
        state = {"answer": "", "report": report, "progress": 0}

        if self.agent_supports_handles():
            # ---- submit and follow ----------------------------------------
            # The turn is handed over and gets an id. The queue keeps the id
            # on the record: that is what makes the work recoverable (a
            # restarted queue picks the turn up again instead of declaring
            # the task dead).
            # The idempotency key: this turn of this task, asked for once. If
            # the answer is lost (a timeout, a proxy), the retry below asks
            # again with the same key and gets **the same turn** back - the
            # work is not done twice.
            #
            # The number in the key is the **turn**, not the attempt: a task
            # that waits for an answer and then goes on (the confirmation
            # flow) starts a *new* turn, and that turn is new work.
            idem_key = "task:%s:%s" % (task_id, str(task.get("turn_seq") or 1))
            handle = None
            for tries in range(3):
                try:
                    handle = self.agent.submit(
                        workspace_id, message, provider=provider,
                        session_id=session_id, task_id=task_id,
                        task_context=context, allow_paths=allow_paths,
                        model=model or None, params=params,
                        limits=limits, policy=policy, idempotency_key=idem_key)
                    break
                except AgentError as exc:
                    log("  task %s: the turn could not be submitted (%s)", task_id, exc)
                    if tries < 2:
                        time.sleep(1.0)
            if handle is None:
                self._finalize(workspace_id, task_id, FAILED, {
                    "error": "the turn could not be submitted",
                    "stop_reason": "submit_failed",
                    "finished_at": now_iso()})
                self._publish({"type": "task_failed",
                               "task": self.store.load(workspace_id, task_id)})
                return
            turn_id = str(handle.get("turn_id") or "")
            session_id = str(handle.get("session_id") or session_id)
            # The checkpoint: where this task is. A queue that comes back
            # after a crash reads it and picks the work up from there.
            self.store.update(workspace_id, task_id, lambda t: t.update({
                "turn_id": turn_id,
                "session_id": session_id,
                "checkpoint": {
                    "turn_id": turn_id,
                    "session_id": session_id,
                    "trajectory_id": "",
                    "at": now_iso(),
                },
            }))
            if not turn_id:
                # The agent answered without a handle. The turn may be running
                # with nobody watching, so this is a failure, not something to
                # paper over: the record says so.
                log("  the agent did not hand out a turn id - the task cannot be followed")
                self._finalize(workspace_id, task_id, FAILED, {
                    "error": "the agent did not hand out a turn id; this agent does not "
                             "support turn handles (0.19.0 or later is needed)",
                    "stop_reason": "no_handle",
                    "finished_at": now_iso()})
                self._publish({"type": "task_failed",
                               "task": self.store.load(workspace_id, task_id)})
                return
            log("  task %s: turn %s submitted" % (task_id, turn_id))
            self._follow_turn(workspace_id, task_id, turn_id, state)
            return

        # ---- the old way: one stream, held to the end --------------------
        # For an agent that does not know the turn-handle interface (older
        # than 0.19.0). The connection is the task's life: if it breaks, the
        # task is lost - which is exactly why the new way exists.
        for event in self.agent.chat(workspace_id, message, provider=provider,
                                     session_id=session_id, task_id=task_id,
                                     task_context=context, allow_paths=allow_paths,
                                     model=model or None, params=params,
                                     limits=limits, policy=policy):
            verdict = self._on_event(workspace_id, task_id, event, state)
            if verdict:
                return
        # The stream ended without a verdict (the agent died, the connection broke).
        written = self._finalize(workspace_id, task_id, FAILED, {
            "error": "the turn ended without an answer",
            "finished_at": now_iso()})
        if written is not None:
            self._publish({"type": "task_failed", "task": written})

    # ---- the agent's events on the queue's own bus -----------------------
    #
    # The agent and the queue each have their own events, and the two do not
    # line up: the queue says task_done, the agent says done, and nobody
    # knows which came first. From now on the agent's events come here too
    # (with a `turn_` prefix), so the router, the watchdog and the page
    # subscribe to **one** stream.

    BRIDGED_EVENTS = {
        "done": "turn_done",
        "error": "turn_error",
        "stopped": "turn_stopped",
        "budget": "budget_exhausted",
    }

    def _bridge_event(self, workspace_id, task_id, event):
        """Put one event of the agent on the queue's bus."""
        kind = event.get("type") or ""
        name = self.BRIDGED_EVENTS.get(kind)
        if name is None and kind == "tool_result" and event.get("ok") is False:
            name = "tool_error"
        if name is None:
            return
        bridged = {
            "type": name,
            "turn_id": event.get("turn_id") or "",
            "request_id": event.get("request_id") or "",
            "detail": dict((k, v) for k, v in event.items() if k != "seq"),
        }
        bridged["task"] = self.store.load(workspace_id, task_id) or {}
        self._publish(bridged)

    def _on_event(self, workspace_id, task_id, event, state):
        """One event of a turn. Returns the verdict, or None to keep going.

        The verdict is "done" / "waiting" / "failed" / "cancelled"; the task
        is finished when it is not None.
        """
        report = state["report"]
        answer = state["answer"]
        kind = event.get("type")
        if kind == "start":
            session_id = event.get("session_id") or ""
            if session_id:
                self.store.update(workspace_id, task_id,
                                  lambda t: t.update({"session_id": session_id}))
        elif kind == "tool_call" and event.get("name") == "toolkit_task":
            self._apply_report(workspace_id, task_id, event, report)
        elif kind == "artifacts":
            # The files this turn produced: the page shows them under the
            # finished task.
            items = event.get("items") or []
            if items:
                # Every file gets an id of its own: the download link names
                # the file, not its path (a path may be renamed later).
                for one in items:
                    if isinstance(one, dict):
                        one["artifact_id"] = artifact_id(one.get("path") or "")
                self.store.update(workspace_id, task_id,
                                  lambda t: t.update({"artifacts": items}))
        elif kind == "content_delta":
            state["answer"] = answer + (event.get("text") or "")
        elif kind == "done":
            answer = event.get("content") or answer
            state["answer"] = answer
            status = WAITING if report["needs_confirmation"] else DONE
            # How the turn ended and what it cost. A turn that ran out of
            # budget is **not** a failure - it says so, and the numbers are
            # on the record.
            stop_reason = str(event.get("stop_reason") or "")
            budget_used = event.get("budget_used")
            budget_used = dict(budget_used) if isinstance(budget_used, dict) else {}
            usage = event.get("usage")
            usage = dict(usage) if isinstance(usage, dict) else {}

            def done_fields(t):
                t["progress"] = 100 if status == DONE else t.get("progress", 0)
                t["result"] = answer
                t["finished_at"] = now_iso()
                t["error"] = ""
                t["stop_reason"] = stop_reason
                t["budget_used"] = budget_used
                t["usage"] = usage
                t["iterations"] = int(event.get("iterations") or 0)
                t["trajectory_id"] = str(event.get("trajectory_id") or "")
                # The checkpoint points at the audit trail of this turn.
                checkpoint = t.get("checkpoint")
                if isinstance(checkpoint, dict):
                    checkpoint["trajectory_id"] = t["trajectory_id"]
                    checkpoint["at"] = now_iso()
                if stop_reason == "budget":
                    # 到点了，不是做完了：说清楚是哪一条预算。
                    t["note"] = "预算用完了（%s）" % (budget_used.get("limit") or "budget")

            written = self._finalize(workspace_id, task_id, status, done_fields)
            if written is not None:
                self._publish(
                    {"type": "task_done" if status == DONE else "task_waiting_confirmation",
                     "task": written})
                if status == DONE:
                    self.note_thread(written, "result", answer)
                else:
                    self.note_thread(written, "question", report.get("question") or "")
            return "waiting" if status == WAITING else "done"
        elif kind == "error":
            written = self._finalize(workspace_id, task_id, FAILED, {
                "error": event.get("message") or "the turn failed",
                "stop_reason": "error",
                "finished_at": now_iso()})
            if written is not None:
                self._publish({"type": "task_failed", "task": written})
                self.note_thread(written, "error", event.get("message") or "这一轮失败了")
            return "failed"
        elif kind == "stopped":
            written = self._finalize(workspace_id, task_id, CANCELLED, {
                "stop_reason": "cancelled",
                "finished_at": now_iso()})
            if written is not None:
                self._publish({"type": "task_cancelled", "task": written})
            return "cancelled"
        return None

    def _consume_fleet(self, workspace_id, task_id, provider, context, message=None):
        """Run the task on the devices: each device's OWN agent answers.

        The server does not have to know how a device is put right - the
        device does. The queue collects the answers and records them as the
        task's result, exactly like a local turn.
        """
        task = self.store.load(workspace_id, task_id) or {}
        prompt = message if message is not None else (task.get("prompt") or "")
        if context:
            prompt = "%s\n\n（%s）" % (prompt, context)
        selection = task.get("fleet") or {}
        who = selection.get("names") or selection.get("groups") or "所有设备"
        self.store.update(workspace_id, task_id, lambda t: t.update({
            "note": "在机群上问 %s" % who, "progress": 10}))
        self._publish({"type": "task_updated",
                             "task": self.store.load(workspace_id, task_id)})
        try:
            answer = self.agent.fleet_ask(
                self.args.fleet_url, self.args.fleet_token, prompt,
                groups=selection.get("groups") or [],
                names=selection.get("names") or [],
                labels=selection.get("labels") or {},
                timeout_s=int(selection.get("timeout_s") or 300))
        except AgentError as exc:
            self._finalize(workspace_id, task_id, FAILED,
                           {"error": "机群没答上来：%s" % str(exc)[:200],
                            "finished_at": now_iso()})
            self._publish({"type": "task_failed",
                                 "task": self.store.load(workspace_id, task_id)})
            return
        rows = answer.get("devices") or []
        lines = []
        for row in rows:
            name = row.get("device") or "?"
            if row.get("ok"):
                lines.append("【%s】%s" % (name, str(row.get("content") or "").strip()))
            else:
                lines.append("【%s】（没答上来）%s" % (name, row.get("error") or ""))
        text = "\n\n".join(lines) or "机群没有回答。"
        written = self._finalize(workspace_id, task_id, DONE, {
            "progress": 100, "result": text, "finished_at": now_iso(), "error": ""})
        if written is not None:
            self._publish({"type": "task_done", "task": written})
            self.note_thread(written, "result", text[:1000])

    def _apply_report(self, workspace_id, task_id, event, report):
        """The agent's own report (toolkit_task): progress, note, a question."""
        raw = event.get("arguments")
        args = {}
        if isinstance(raw, str):
            try:
                args = json.loads(raw) if raw.strip() else {}
            except ValueError:
                args = {}
        elif isinstance(raw, dict):
            args = raw
        if not isinstance(args, dict):
            args = {}

        progress = args.get("progress")
        note = str(args.get("note") or "").strip()
        question = str(args.get("question") or "").strip()
        needs = bool(args.get("needs_confirmation"))

        report["needs_confirmation"] = needs
        if question:
            report["question"] = question
        if note:
            report["note"] = note

        def apply(t):
            if isinstance(progress, int) and 0 <= progress <= 100:
                t["progress"] = progress
            if note:
                t["note"] = note
                t.setdefault("steps", []).append(
                    {"at": now_iso(), "progress": t.get("progress", 0), "note": note})
            if needs:
                t["needs_confirmation"] = True
                t["question"] = question
                # The question gets an id and a deadline of its own: the
                # caller answers *that* question, and the queue knows when it
                # has waited long enough.
                timeout = float(getattr(self.args, "interaction_timeout", 0) or 0)
                t["interaction"] = {
                    "interaction_id": "in_" + os.urandom(8).hex(),
                    "question": question,
                    "created_at": now_iso(),
                    "expires_at": (iso_from_epoch(time.time() + timeout)
                                    if timeout > 0 else ""),
                }

        self.store.update(workspace_id, task_id, apply)
        fresh = self.store.load(workspace_id, task_id)
        self._publish({"type": "task_updated", "task": fresh})
        if note:
            self.note_thread(fresh, "note", note)

    # ---- the loop ---------------------------------------------------------
    def run_forever(self):
        while True:
            try:
                self.round()
                self.check_plans()
            except AgentError as exc:
                log("round failed: %s" % exc)
            except Exception as exc:  # noqa: BLE001
                log("round failed: %s: %s" % (type(exc).__name__, exc))
            time.sleep(self.args.interval)


class Handler(BaseHTTPRequestHandler):
    """The task management interface."""

    server_version = "tea-task-server/1.0"
    protocol_version = "HTTP/1.1"

    # -- plumbing ------------------------------------------------------
    def log_message(self, *args):
        pass

    def handle_one_request(self):
        """A client that hangs up (the event stream, a closed browser tab) is
        not an error - without this, every disconnect fills the log with a
        traceback."""
        try:
            BaseHTTPRequestHandler.handle_one_request(self)
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            pass

    def _request_id(self):
        """The id of this request. It goes into the envelope and the header, so
        a user can quote it and the log can be found.

        The caller's own id is echoed when it sends one: a system further up
        (a proxy, a gateway, a queue of its own) can then follow one string
        through the whole chain. An id that is not a plain token is replaced -
        it ends up in a header and in a log line.
        """
        if not getattr(self, "_rid", ""):
            given = (self.headers.get("X-Request-Id") or "").strip()
            if given and len(given) <= 128 and all(
                    ch.isalnum() or ch in "-_.:@" for ch in given):
                self._rid = given
            else:
                self._rid = "req_" + base64.b32encode(os.urandom(5)).decode().strip("=").lower()
        return self._rid

    def _envelope(self, code, message, data):
        return {"code": code, "message": message,
                "request_id": self._request_id(), "data": data}

    def _json(self, status, payload):
        """One answer, one shape: {code, message, request_id, data}."""
        body = self._envelope(CODE_OK, "ok", payload)
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("X-Request-Id", self._request_id())
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(raw)

    def _error(self, status, code, message, retry_after=0):
        """An error is the same envelope with a number instead of a zero."""
        numeric = ERROR_CODES.get(code)
        if numeric is None:
            numeric = {400: CODE_BAD_REQUEST, 401: CODE_UNAUTHENTICATED,
                       403: CODE_FORBIDDEN, 404: CODE_NOT_FOUND,
                       409: CODE_CONFLICT, 429: CODE_RATE_LIMIT}.get(status, CODE_INTERNAL)
        body = self._envelope(numeric, message, {"error": {"code": code, "message": message}})
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("X-Request-Id", self._request_id())
        if retry_after > 0:
            # "Come back later" has to say when.
            self.send_header("Retry-After", str(int(retry_after)))
        self.end_headers()
        self.wfile.write(raw)

    def _body(self):
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except ValueError:
            return None

    def _query(self):
        return urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)

    def _int(self, name, default=0):
        raw = self._one(name)
        try:
            return int(str(raw).strip())
        except (TypeError, ValueError):
            return default

    def _key(self):
        """The project key the caller presents ("" = the local operator)."""
        raw = self.headers.get("Authorization") or ""
        if raw.lower().startswith("bearer "):
            raw = raw[7:].strip()
        if not raw:
            raw = self.headers.get("X-Project-Key") or ""
        return raw.strip()

    def _project_of_caller(self):
        """The project the caller's key belongs to ("" = operator / no key)."""
        return self.app.store.project_of_key(self._key())

    def _guard(self):
        """A key that belongs to no project gets nothing.

        No key at all = the local operator. A key that is not in the book =
        nobody: it must not pass for the operator, or a typo (or a guess)
        would turn into a master key that sees every project.

        A caller that names a tenant must be **of** that tenant: the key says
        whose work it may touch, and a mismatch is 403 - not a quiet success.
        """
        key = self._key()
        if key and not self.app.store.project_of_key(key):
            self._error(401, "unauthenticated", "this key belongs to no project")
            return False
        tenant = (self.headers.get("X-Tenant-Id") or "").strip()
        if tenant:
            own = self.app.store.tenant_of_key(key)
            if own and own != tenant:
                self._error(403, "forbidden",
                            "this key does not belong to the tenant %r" % tenant)
                return False
        return True

    def _forbid(self, what):
        return self._error(403, "forbidden",
                           "this key does not open %s" % what)

    def _check_project(self, task):
        """A task belongs to a project: a key of another project gets nothing."""
        if task is None:
            return True
        caller = self._project_of_caller()
        if not caller:
            return True              # the local operator
        if caller == (task.get("workspace_id") or ""):
            return True
        return False

    def _one(self, name, default=""):
        values = self._query().get(name) or []
        return values[0] if values else default

    @property
    def app(self):
        return self.server.app

    # -- routes ---------------------------------------------------------
    def _versioned(self, path):
        """The old path for a versioned one (the requirement's /v1/...).

        The requirement asks for /v1/agent-tasks; the queue has always been
        /api/tasks. Both work: the version segment is added, nothing is
        removed, and there is one set of handlers behind them.
        """
        if path == "/v1/agent-tasks":
            return "/api/tasks"
        if path.startswith("/v1/agent-tasks/"):
            rest = path[len("/v1/agent-tasks/"):]
            # The requirement's name for answering a question.
            if rest.endswith("/interactions"):
                rest = rest[:-len("/interactions")] + "/confirm"
            return "/api/tasks/" + rest
        if path == "/v1/projects":
            return "/api/projects"
        if path.startswith("/v1/queues/") and path.endswith("/status"):
            return "/api/queue/state"
        if path.startswith("/v1/exports/"):
            return "/api/exports/" + path[len("/v1/exports/"):]
        return path

    def _rate_limit(self):
        """One caller's allowance: N requests a minute (0 = no limit).

        The limit is per key (the tenant's), not per machine: one busy
        tenant must not lock the others out.
        """
        limit = int(getattr(self.app.args, "rate_limit_per_minute", 0) or 0)
        if limit <= 0:
            return True
        key = self._key() or "local-operator"
        now = time.time()
        with self.app.rate_lock:
            hits = [t for t in self.app.rate_hits.get(key, []) if now - t < 60.0]
            if len(hits) >= limit:
                self.app.rate_hits[key] = hits
                wait = max(1, int(60.0 - (now - hits[0])))
                self._error(429, "rate_limited",
                            "too many requests: at most %d a minute" % limit,
                            retry_after=wait)
                return False
            hits.append(now)
            self.app.rate_hits[key] = hits
        return True

    def do_OPTIONS(self):  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):  # noqa: N802
        path = self._versioned(urllib.parse.urlparse(self.path).path)
        if path in ("/", "/ui", "/index.html"):
            return self._page()
        if path == "/api/health":
            return self._health()
        if path == "/api/doc":
            return self._doc()
        if path.startswith("/api/doc/"):
            rest = path[len("/api/doc/"):]
            if rest.endswith(".md") and "/" not in rest:
                return self._doc(rest)
            return self._error(404, "not_found", "no such document")
        if not self._guard() or not self._rate_limit():
            return
        if path == "/api/tasks":
            return self._list()
        if path == "/api/events":
            return self._events()
        if path == "/api/audit":
            return self._audit()
        if path == "/api/projects":
            return self._projects()
        if path == "/api/queue/state":
            return self._queue_state()
        if path == "/api/triggers":
            return self._list_triggers()
        if path == "/api/threads":
            return self._list_threads()
        if path == "/api/plans":
            return self._list_plans()
        if path.startswith("/api/plans/"):
            return self._get_plan(path[len("/api/plans/"):])
        if path.startswith("/api/triggers/"):
            return self._get_trigger(path[len("/api/triggers/"):])
        if path.startswith("/api/threads/"):
            rest = path[len("/api/threads/"):]
            if rest.endswith("/context"):
                return self._thread_context(rest[:-len("/context")])
            return self._get_thread(rest)
        if path == "/api/task-order-template":
            return self._task_order_template()
        if path == "/api/workspaces":
            return self._workspaces()
        if path == "/api/providers":
            return self._providers()
        if path.startswith("/api/exports/"):
            rest = path[len("/api/exports/"):]
            if rest.endswith("/download"):
                return self._export_download(rest[:-len("/download")])
            return self._export_job(rest)
        if path.startswith("/api/tasks/"):
            rest = path[len("/api/tasks/"):]
            if "/" in rest:
                task_id, action = rest.split("/", 1)
                if action == "events":
                    return self._task_events(task_id)
                if action == "export":
                    return self._export(task_id)
                if action == "download":
                    return self._download(task_id)
                if action == "result":
                    return self._result(task_id)
                if action.startswith("artifacts/"):
                    return self._artifact(task_id, action[len("artifacts/"):])
                return self._error(404, "not_found", "no such action: %s" % action)
            return self._get(rest)
        return self._error(404, "not_found", "no such endpoint: %s" % path)

    def do_POST(self):  # noqa: N802
        path = self._versioned(urllib.parse.urlparse(self.path).path)
        if not self._guard() or not self._rate_limit():
            return
        if path == "/api/tasks":
            return self._create()
        if path == "/api/projects":
            return self._project_key()
        if path == "/api/queue/pause":
            return self._queue_pause(True)
        if path == "/api/queue/resume":
            return self._queue_pause(False)
        if path == "/api/events":
            return self._publish_event()
        if path == "/api/triggers":
            return self._submit_trigger()
        if path == "/api/threads":
            return self._create_thread()
        if path == "/api/plans":
            return self._create_plan()
        if path.startswith("/api/plans/") and path.endswith("/cancel"):
            return self._cancel_plan(path[len("/api/plans/"):-len("/cancel")])
        if path.startswith("/api/threads/") and path.endswith("/spill"):
            return self._thread_spill(path[len("/api/threads/"):-len("/spill")])
        if path.startswith("/api/tasks/"):
            rest = path[len("/api/tasks/"):]
            if "/" in rest:
                task_id, action = rest.split("/", 1)
                if action == "export":
                    return self._export_start(task_id)
                return self._action(task_id, action)
            return self._error(404, "not_found", "no such action")
        return self._error(404, "not_found", "no such endpoint: %s" % path)

    def do_DELETE(self):  # noqa: N802
        path = self._versioned(urllib.parse.urlparse(self.path).path)
        if not self._guard() or not self._rate_limit():
            return
        if path.startswith("/api/tasks/"):
            return self._delete(path.rsplit("/", 1)[-1])
        return self._error(404, "not_found", "no such endpoint: %s" % path)

    # -- handlers -------------------------------------------------------
    def _page(self):
        """The task queue's own page: queued, running, finished."""
        html = self.app.page_html()
        if not html:
            return self._error(404, "not_found",
                               "the page file (task_ui.html) is missing next to this script")
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _health(self):
        agent = self.app.agent
        try:
            health = agent.health()
            agent_ok = True
        except AgentError as exc:
            health = {"error": str(exc)}
            agent_ok = False
        provider = self.app.engine.effective_provider()
        model = self.app.engine.effective_model()
        left, _ = self.app.engine.quota_left(provider)
        self._json(200, {
            "ok": True,
            # Which queue this is. A client asks before it speaks: the
            # version segment (/v1/agent-tasks) arrived in 0.21.0, and a
            # newer client has to work against an older queue.
            "version": self.app.version(),
            # The agent behind the queue (they ship in the same package, but
            # the agent may not have been restarted yet).
            "agent_version": str(health.get("version") or ""),
            "agent": agent.base,
            "agent_ok": agent_ok,
            "agent_health": health,
            # The provider the queued work runs on, and the model it will
            # use. `agent_health` is the agent's OWN default (what the chat
            # front end uses) - the queue does not use it.
            "provider": provider,
            "model": model or self.app.agent.primary_model(provider),
            "task_model": (provider + "+" + model) if model else provider,
            "quota_percent": left,
            "data_dir": self.app.store.root,
            "counts": self.app.store.counts(),
            "metrics": self.app.metrics(),
        })

    def _list(self):
        workspace_id = self._one("workspace_id")
        status = self._one("status")
        query = self._query()
        page = self._int("page", 0)
        page_size = self._int("page_size", 0)
        filters = {"workspace_id": workspace_id or None, "status": status or None,
                   "created_from": self._one("created_from"),
                   "created_to": self._one("created_to"),
                   "tag": self._one("tag"), "business_id": self._one("business_id")}
        caller = self._project_of_caller()
        if caller:
            # A key sees its own project and nothing else.
            filters["workspace_id"] = caller
        everything = self.app.store.list(**filters)
        total = len(everything)
        # The queue order is read once for the whole list.
        order = self.app.queue_order()
        if page_size and page_size > 0:
            page = max(1, page)
            start = (page - 1) * page_size
            everything = everything[start:start + page_size]
        elif page and page > 0:
            start = (page - 1) * 50
            everything = everything[start:start + 50]
        self._json(200, {"tasks": [self.app.decorate(t, order=order) for t in everything],
                         "counts": self.app.store.counts(caller or workspace_id or None),
                         "total": total, "page": page or 1,
                         "page_size": page_size or 0})

    def _get(self, task_id):
        workspace_id = self._one("workspace_id")
        task = self.app.store.load(workspace_id, task_id)
        if task is None:
            return self._error(404, "not_found", "no task %r" % task_id)
        if not self._check_project(task):
            return self._forbid("task %s" % task_id)
        full = self._one("include").lower() in ("full", "1", "true")
        self._json(200, self.app.decorate(task, full=full))

    def _task_order_template(self):
        """The task order template, as a download.

        The queue's web page offers it: fill it in, hand it back, the work is
        queued - every setting a task can have is in that file.
        """
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "task-order.template")
        try:
            with open(path, encoding="utf-8") as f:
                body = f.read().encode("utf-8")
        except OSError:
            return self._error(404, "not_found",
                               "the task order template is missing next to the server")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Disposition",
                         'attachment; filename="task-order.template"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _workspaces(self):
        """The workspaces a task may run in.

        The queue's own workspace is in the list too (it is the default), and
        the agent's - the page draws its picker from this.
        """
        out = []
        try:
            for ws in self.app.agent.workspaces() or []:
                out.append({
                    "id": ws.get("id") or "",
                    "name": ws.get("name") or "",
                    "path": ws.get("path") or "",
                    "writable": bool(ws.get("writable", True)),
                    "write_error": ws.get("write_error") or "",
                })
        except AgentError as exc:
            self._json(200, {"workspaces": [], "agent_ok": False, "error": str(exc)})
            return
        own = self.app.task_workspace_id()
        if own and not any(one.get("id") == own for one in out):
            out.insert(0, {"id": own, "name": "队列自己的工作区",
                           "path": self.app.workspace_path(own),
                           "writable": True, "write_error": ""})
        self._json(200, {"workspaces": out, "agent_ok": True,
                         "default_workspace_id": self.app.default_workspace()})

    def _providers(self):
        """The providers the agent knows, with their models.

        The page draws its provider and model pickers from this, so a task
        can say which provider and which model it wants.
        """
        try:
            body = self.app.agent.providers()
        except AgentError as exc:
            self._json(200, {"providers": [], "agent_ok": False, "error": str(exc)})
            return
        if not isinstance(body, dict):
            body = {}
        providers = []
        for one in body.get("providers") or []:
            if not isinstance(one, dict):
                continue
            providers.append({
                "name": one.get("name") or "",
                "default_model": one.get("default_model") or "",
                "models": [str(m) for m in (one.get("models") or [])],
                "has_key": bool(one.get("has_key")),
                "supports_usage": bool(one.get("supports_usage")),
            })
        self._json(200, {"providers": providers, "agent_ok": True,
                         "active": body.get("active") or {},
                         "reasoning_efforts": body.get("reasoning_efforts") or []})

    def _projects(self):
        """The projects (workspaces) the queue knows about.

        The keys are not here: a caller that may see the list may not open
        somebody else's project.
        """
        known = self.app.store.projects()
        out = []
        caller = self._project_of_caller()
        try:
            workspaces = self.app.agent.workspaces() or []
        except AgentError:
            workspaces = []
        for ws in workspaces:
            workspace_id = ws.get("id") or ""
            if not workspace_id:
                continue
            if caller and workspace_id != caller:
                continue        # a key sees its own project, not the roster
            out.append({"workspace_id": workspace_id,
                        "name": ws.get("name") or "",
                        "has_key": bool(known.get(workspace_id))})
        for workspace_id in known:
            if caller and workspace_id != caller:
                continue
            if not any(p["workspace_id"] == workspace_id for p in out):
                out.append({"workspace_id": workspace_id, "name": "", "has_key": True})
        out.sort(key=lambda p: p["workspace_id"])
        self._json(200, {"projects": out})

    def _project_key(self):
        """The key of a project - for the local operator only.

        A caller that already carries a key may not ask for another one: that
        would make the key worthless.
        """
        if self._key():
            return self._forbid("another project's key")
        body = self._body()
        if body is None:
            return self._error(400, "bad_request", "the request body is not JSON")
        workspace_id = str(body.get("workspace_id") or "").strip()
        if not workspace_id:
            return self._error(400, "bad_request", "'workspace_id' is required")
        # The tenant this key belongs to and who is asking with it. A project
        # may have several keys: one per tenant, one per person.
        tenant_id = str(body.get("tenant_id") or "").strip()
        identity = str(body.get("identity") or "").strip()
        key = self.app.store.project_key(workspace_id, tenant_id, identity)
        self.app.store.audit("project_key", "", {"workspace_id": workspace_id,
                                                 "tenant_id": tenant_id,
                                                 "identity": identity})
        self._json(200, {"workspace_id": workspace_id, "key": key,
                         "tenant_id": tenant_id, "identity": identity})

    def _doc(self, name=""):
        """The queue's own documentation, the way the agent serves its API doc.

          GET /api/doc             a web page with the reference
          GET /api/doc?format=md   the same text as Markdown
          GET /api/doc?format=json {version, title, markdown, html}

        The text is docs/tasks.md from the repository - the same file the
        developer wrote, so there is no second copy to fall behind. It is not
        embedded in anything: the queue is a Python script that already lives
        next to the repository, and the file is read when it is asked for.
        """
        wanted = str(name or self._one("file") or "")
        if wanted:
            path = self.app.doc_path(wanted)
            if not path:
                return self._error(404, "not_found", "no such document")
        else:
            path = self.app.doc_path()
        try:
            # newline="" keeps CRLF as it is on disk: the documentation is served
            # byte for byte (and the queue runs on Windows as well as on Linux).
            with open(path, encoding="utf-8", newline="") as f:
                markdown = f.read()
        except OSError:
            return self._error(404, "not_found",
                               "the documentation file is missing: %s" % path)
        fmt = (self._one("format") or "html").lower()
        if fmt in ("markdown", "md"):
            body = markdown.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/markdown; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        html = render_markdown(markdown)
        if fmt == "json":
            self._json(200, {"version": self.app.version(),
                              "title": "tea_a2 task queue",
                              "path": path,
                              "markdown": markdown,
                              "html": html})
            return
        if fmt != "html":
            return self._error(400, "bad_request",
                               "'format' must be html, md or json")
        page = _doc_page("tea_a2 task queue", html)
        body = page.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _task_events(self, task_id):
        """The task's event log, optionally only what came after the last one
        the client saw."""
        workspace_id = self._one("workspace_id")
        task = self.app.store.load(workspace_id, task_id)
        if task is None:
            return self._error(404, "not_found", "no task %r" % task_id)
        if not self._check_project(task):
            return self._forbid("task %s" % task_id)
        since = self._int("since_event_id", 0)
        events = self.app.store.events(task_id, since)
        self._json(200, {"task_id": task_id, "events": events,
                         "last_event_id": events[-1]["event_id"] if events else since})

    def _export(self, task_id):
        """The result of a task, as one document or as a zip.

        The zip is what somebody wants six months later: the manifest, the
        final answer, the conversation, the events, the files. The layout is
        the one the requirement suggests.
        """
        fmt = (self._one("format") or "json").lower()
        with_artifacts = self._one("include_artifacts").lower() not in ("0", "false", "no")
        task = self._task_of(task_id)
        if task is None:
            return
        if fmt == "zip":
            body, manifest = self._export_bytes(task, fmt, with_artifacts)
            if body is None:
                return self._error(500, "export_failed", "the export could not be built")
            self.app.store.audit("export", task_id, {"format": "zip", "bytes": len(body)})
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition",
                             'attachment; filename="task-%s.zip"' % task_id)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if fmt != "json":
            return self._error(400, "bad_request", "format must be json or zip")
        document = self._export_document(task, with_artifacts)
        self.app.store.audit("export", task_id, {"format": "json"})
        self._json(200, document)

    def _export_bytes(self, task, fmt, with_artifacts):
        """The export of a task as bytes (the zip). Returns (bytes, manifest)."""
        import io
        import zipfile

        task_id = task.get("id") or ""
        events = self.app.store.events(task_id)
        artifacts = task.get("artifacts") or []
        manifest = self._manifest(task, artifacts)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
            z.writestr("final_answer.md", (task.get("result") or "").encode("utf-8"))
            z.writestr("error.json", json.dumps(
                {"error": task.get("error") or ""}, ensure_ascii=False, indent=2))
            z.writestr("conversation.jsonl", "".join(
                json.dumps(m, ensure_ascii=False) + "\n" for m in
                self._conversation(task)).encode("utf-8"))
            z.writestr("events.jsonl", "".join(
                json.dumps(e, ensure_ascii=False) + "\n" for e in events).encode("utf-8"))
            if with_artifacts:
                for art in artifacts:
                    rel = str(art.get("path") or "")
                    if not rel:
                        continue
                    data = self.app.agent.download(task.get("workspace_id") or "", rel)
                    if not data:
                        continue
                    z.writestr("artifacts/" + rel.lstrip("/"), data)
        return buf.getvalue(), manifest

    def _conversation(self, task):
        session = self.app.agent.session(task.get("session_id") or "")
        return session.get("messages") or []

    def _manifest(self, task, artifacts):
        task_id = task.get("id") or ""
        return {
            "task_id": task_id,
            "workspace_id": task.get("workspace_id") or "",
            "title": task.get("title") or "",
            "status": task.get("status") or "",
            "priority": task.get("priority") or 0,
            "attempt": task.get("attempt") or 1,
            "created_at": task.get("created_at") or "",
            "started_at": task.get("started_at") or "",
            "finished_at": task.get("finished_at") or "",
            "session_id": task.get("session_id") or "",
            "error": task.get("error") or "",
            "artifacts": artifacts,
            "exported_at": now_iso(),
        }

    def _export_document(self, task, with_artifacts):
        """The export of a task as one document (the json form)."""
        task_id = task.get("id") or ""
        artifacts = task.get("artifacts") or []
        return {
            "manifest": self._manifest(task, artifacts),
            "final_answer": task.get("result") or "",
            "events": self.app.store.events(task_id),
            "conversation": self._conversation(task),
            "artifacts": artifacts,
            "trace_id": task.get("trace_id") or "",
        }


    def _task_of(self, task_id):
        """The task, if the caller may see it.

        None when it may not: the answer has already been sent, so the caller
        only has to stop.
        """
        workspace_id = self._one("workspace_id")
        task = self.app.store.load(workspace_id, task_id)
        if task is None:
            self._error(404, "not_found", "no task %r" % task_id)
            return None
        if not self._check_project(task):
            self._forbid("task %s" % task_id)
            return None
        return task

    def _result(self, task_id):
        """The result of a task: the answer, the usage and the files.

        The requirement asks for a summary of the result and a list of the
        artifacts (and their ids, so they can be downloaded one by one).
        """
        task = self._task_of(task_id)
        if task is None:
            return
        artifacts = []
        for art in task.get("artifacts") or []:
            if not isinstance(art, dict):
                continue
            path = str(art.get("path") or "")
            one = dict(art)
            one["artifact_id"] = artifact_id(path)
            one["download_url"] = "/v1/agent-tasks/%s/artifacts/%s" % (
                task_id, one["artifact_id"])
            artifacts.append(one)
        self._json(200, {
            "task_id": task_id,
            "status": task.get("status") or "",
            "result": task.get("result") or "",
            "error": task.get("error") or "",
            "stop_reason": task.get("stop_reason") or "",
            "usage": task.get("usage") or {},
            "iterations": task.get("iterations") or 0,
            "trajectory_id": task.get("trajectory_id") or "",
            "session_id": task.get("session_id") or "",
            "artifacts": artifacts,
            "trace_id": task.get("trace_id") or "",
            "finished_at": task.get("finished_at") or "",
        })

    def _artifact(self, task_id, wanted):
        """One file the task produced, by its id.

        The id is the hash of the path: the path may be renamed later, the
        id does not change. The file is fetched from the agent (it lives in
        the agent's workspace) and the project key guards it, like the tasks.
        """
        task = self._task_of(task_id)
        if task is None:
            return
        for art in task.get("artifacts") or []:
            if not isinstance(art, dict):
                continue
            path = str(art.get("path") or "")
            if artifact_id(path) != wanted:
                continue
            data = self.app.agent.download(task.get("workspace_id") or "", path)
            if not data:
                return self._error(404, "not_found",
                                   "the file is gone (or empty): %r" % path[:80])
            self.app.store.audit("download", task_id, {"artifact_id": wanted})
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Disposition",
                             'attachment; filename="%s"' % os.path.basename(path))
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self._error(404, "not_found", "no artifact %r for this task" % wanted)

    # ---- the export as a job ----------------------------------------------
    def _export_start(self, task_id):
        """Start an export and answer with the job.

        A big export takes a while (the conversation, the events, the files):
        the caller gets a job id and comes back for it. The small ones are
        still served directly by GET .../export.
        """
        body = self._body() or {}
        fmt = str(body.get("format") or "zip").lower()
        with_artifacts = body.get("include_artifacts", True)
        with_artifacts = bool(with_artifacts) and str(with_artifacts).lower() not in (
            "0", "false", "no")
        task = self._task_of(task_id)
        if task is None:
            return
        if fmt not in ("zip", "json"):
            return self._error(400, "bad_request", "format must be json or zip")
        job_id = "ex_" + os.urandom(8).hex()
        ttl = float(getattr(self.app.args, "export_ttl_hours", 24.0) or 24.0)
        job = {
            "id": job_id,
            "task_id": task_id,
            "format": fmt,
            "status": "running",
            "created_at": now_iso(),
            "expires_at": iso_from_epoch(time.time() + ttl * 3600.0),
            "bytes": 0,
            "error": "",
        }
        self.app.store.save_export(job)
        threading.Thread(target=self._export_run,
                         args=(job_id, task_id, fmt, with_artifacts), daemon=True).start()
        self.app.store.audit("export_start", task_id, {"format": fmt, "export_id": job_id})
        self._json(202, {"export_id": job_id, "task_id": task_id, "status": "running",
                         "expires_at": job["expires_at"],
                         "status_url": "/v1/exports/" + job_id})

    def _export_run(self, job_id, task_id, fmt, with_artifacts):
        """Build the export in the background."""
        job = self.app.store.load_export(job_id) or {}
        try:
            task = self.app.store.load("", task_id)
            if task is None:
                raise RuntimeError("the task is gone")
            if fmt == "zip":
                data, manifest = self._export_bytes(task, fmt, with_artifacts)
                path = self.app.store.export_file(job_id)
                with open(path, "wb") as f:
                    f.write(data)
                job["bytes"] = len(data)
                job["manifest"] = manifest
            else:
                document = self._export_document(task, with_artifacts)
                data = json.dumps(document, ensure_ascii=False, indent=2).encode("utf-8")
                path = self.app.store.export_file(job_id)
                with open(path, "wb") as f:
                    f.write(data)
                job["bytes"] = len(data)
            job["status"] = "done"
            job["finished_at"] = now_iso()
        except Exception as exc:  # noqa: BLE001 - a job may fail, the queue may not
            job["status"] = "failed"
            job["error"] = "%s: %s" % (type(exc).__name__, exc)
            job["finished_at"] = now_iso()
        self.app.store.save_export(job)

    def _export_job(self, job_id):
        """The state of an export job."""
        job = self.app.store.load_export(job_id)
        if job is None:
            return self._error(404, "not_found", "no export job %r" % job_id)
        task = self.app.store.load("", job.get("task_id") or "")
        if task is not None and not self._check_project(task):
            return self._forbid("export %s" % job_id)
        out = dict(job)
        out.pop("manifest", None)
        if job.get("status") == "done":
            out["download_url"] = "/v1/exports/%s/download" % job_id
        self._json(200, out)

    def _export_download(self, job_id):
        """The bytes of a finished export job."""
        job = self.app.store.load_export(job_id)
        if job is None:
            return self._error(404, "not_found", "no export job %r" % job_id)
        task = self.app.store.load("", job.get("task_id") or "")
        if task is not None and not self._check_project(task):
            return self._forbid("export %s" % job_id)
        if job.get("status") != "done":
            return self._error(409, "bad_request", "the export is not finished yet")
        expires = parse_iso(job.get("expires_at") or "")
        if expires is not None and expires < time.time():
            return self._error(410, "not_found", "this export has expired")
        try:
            with open(self.app.store.export_file(job_id), "rb") as f:
                data = f.read()
        except OSError:
            return self._error(404, "not_found", "the export file is gone")
        fmt = job.get("format") or "zip"
        self.send_response(200)
        self.send_header("Content-Type",
                         "application/zip" if fmt == "zip" else "application/json")
        self.send_header("Content-Disposition",
                         'attachment; filename="task-%s.%s"' % (job.get("task_id") or "task", fmt))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _download(self, task_id):
        """One file the task produced.

        The queue serves it: the browser may not be able to reach the agent
        (it lives on another machine), and the project key that guards the
        tasks guards this file too. Only a file the task really produced is
        served - the task's workspace is not a public directory.
        """
        workspace_id = self._one("workspace_id")
        task = self.app.store.load(workspace_id, task_id)
        if task is None:
            return self._error(404, "not_found", "no task %r" % task_id)
        if not self._check_project(task):
            return self._forbid("task %s" % task_id)
        path = self._one("path")
        if not path:
            return self._error(400, "bad_request", "'path' is required")
        produced = [str(a.get("path") or "") for a in (task.get("artifacts") or [])
                    if isinstance(a, dict)]
        if path not in produced:
            return self._error(404, "not_found",
                               "%r is not a file this task produced" % path[:80])
        data = self.app.agent.download(task.get("workspace_id") or "", path)
        if not data:
            return self._error(404, "not_found",
                               "the file is gone (or empty): %r" % path[:80])
        name = path.replace("\\", "/").rsplit("/", 1)[-1] or "file"
        name = name.replace('"', "")
        self.app.store.audit("download", task_id, {"path": path, "bytes": len(data)})
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Disposition",
                         "attachment; filename=\"%s\"; filename*=UTF-8''%s"
                         % (name, urllib.parse.quote(name)))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _audit(self):
        task_id = self._one("task_id")
        limit = self._int("limit", 200)
        entries = self.app.store.audit_log(task_id, max(1, min(limit, 1000)))
        scope = self._project_of_caller()
        if scope:
            # The log holds every project's business, and a prompt is that
            # project's own. A key reads its own lines and no others.
            entries = [e for e in entries if self._entry_in_scope(e, scope)]
        self._json(200, {"entries": entries})

    def _entry_in_scope(self, entry, scope):
        """Is this audit line about the project the caller may see?"""
        detail = entry.get("detail") or {}
        workspace_id = detail.get("workspace_id") or ""
        if workspace_id:
            return workspace_id == scope
        task_id = entry.get("task_id") or ""
        if not task_id:
            return False
        task = self.app.store.load(scope, task_id)
        if task is None:
            return False        # the task is gone: nobody can vouch for it
        return (task.get("workspace_id") or "") == scope

    def _create(self):
        body = self._body()
        if body is None:
            return self._error(400, "bad_request", "the request body is not JSON")
        workspace_id = str(body.get("workspace_id") or "").strip()
        prompt = str(body.get("prompt") or "").strip()
        # The workspace may also be named by its path - that is what a person
        # types. The queue turns it into the id the agent knows.
        named = str(body.get("workspace") or body.get("workspace_path") or "").strip()
        if named and not workspace_id:
            try:
                workspace_id = resolve_workspace_id(self.app.agent, named)
            except task_order.OrderError as exc:
                return self._error(400, "bad_request", str(exc))
            except AgentError:
                workspace_id = named      # the agent is down: keep what was written
        if not workspace_id:
            # The same default the scheduler uses: the first workspace.
            workspace_id = self.app.default_workspace()
        if not workspace_id:
            return self._error(400, "bad_request",
                               "'workspace_id' is required (the agent has no workspace)")
        if not prompt:
            return self._error(400, "bad_request", "'prompt' is required")
        # Extra directories the file tools may touch while this task runs. The
        # agent's boundary is the workspace; this is the task saying "and these
        # ones, for this task only".
        allow_paths = []
        raw_paths = body.get("allow_paths")
        if isinstance(raw_paths, str):
            raw_paths = raw_paths.replace(";", ",").split(",")
        if isinstance(raw_paths, list):
            for one in raw_paths:
                if not isinstance(one, str):
                    continue
                one = one.strip()
                if one:
                    allow_paths.append(os.path.abspath(os.path.expanduser(one)))
        caller = self._project_of_caller()
        if caller and workspace_id != caller:
            return self._forbid("project %s" % workspace_id)
        if body.get("workspace_id"):
            # A task that names a workspace nobody has would sit in the queue and
            # fail later, with a much less obvious reason. Check it now - unless
            # the agent is down, in which case the queue has to keep accepting.
            try:
                known = [w.get("id") for w in self.app.agent.workspaces()]
            except AgentError:
                known = []          # the agent is down: the queue keeps accepting
            if known and workspace_id not in known:
                return self._error(400, "bad_request",
                                   "no workspace %r (the agent knows: %s)"
                                   % (workspace_id, ", ".join(known)))
        # The idempotency key: the same key must not queue the same work twice
        # (a client that retries a submission, a bot that repeats a command).
        key = str(self.headers.get("Idempotency-Key") or body.get("idempotency_key") or "").strip()
        if key:
            existing = self.app.store.find_by_idempotency(key)
            if existing is not None:
                self._json(200, self.app.decorate(existing))
                return
        try:
            priority = int(body.get("priority") or 0)
        except (TypeError, ValueError):
            priority = 0
        # "not before": the task may not start before this time. The text is
        # what the user wrote ("+5", "23:30"); the stamp is absolute.
        not_before, not_before_text = "", ""
        wanted = body.get("not_before")
        if wanted in (None, ""):
            # "start_at" is the word the web page uses.
            wanted = body.get("start_at")
        if wanted not in (None, ""):
            not_before, not_before_text = parse_not_before(str(wanted))
            if not not_before:
                return self._error(400, "bad_request",
                                   "'not_before' is not a time I understand: %r "
                                   "(try '+5' for five hours from now, or '23:30')"
                                   % str(wanted)[:40])
        # The model knobs of this task (temperature, max_tokens, ...). A knob
        # nobody knows is refused here - not after a long wait in the queue.
        model_params, params_error = parse_model_params(body)
        if params_error:
            return self._error(400, "bad_request", params_error)
        # The budget of this task (the ceilings the agent enforces) and the
        # permissions it runs with (which tools it may use). Both are checked
        # here, not after a long wait in the queue.
        limits, limits_error = parse_limits(body)
        if limits_error:
            return self._error(400, "bad_request", limits_error)
        policy, policy_error = parse_policy(body)
        if policy_error:
            return self._error(400, "bad_request", policy_error)
        task = new_task(body, workspace_id, allow_paths=allow_paths, priority=priority,
                        not_before=not_before, not_before_text=not_before_text, key=key,
                        meta=body.get("meta") if isinstance(body.get("meta"), dict) else {},
                        model_params=model_params, limits=limits, policy=policy)
        # Whose work this is and who asked for it: the key says. The local
        # operator (no key) leaves both empty.
        task["tenant_id"] = self.app.store.tenant_of_key(self._key())
        task["identity"] = self.app.store.identity_of_key(self._key())
        # Where the work runs: "" = the local agent, "fleet" = the devices'.
        task["target"] = str(body.get("target") or "").strip()
        if isinstance(body.get("fleet"), dict):
            task["fleet"] = dict(body["fleet"])
        self.app.store.create(task)
        self.app.publish("task_created", task)
        self.app.store.audit("submit", task["id"], {"prompt": prompt[:200], "key": key},
                             actor=self.app.store.identity_of_key(self._key()))
        self._json(200, self.app.decorate(task))

    def _delete(self, task_id):
        workspace_id = self._one("workspace_id")
        task = self.app.store.load(workspace_id, task_id)
        if task is None:
            return self._error(404, "not_found", "no task %r" % task_id)
        # The record knows its own workspace - use it, the caller may not have
        # said which one.
        owner = task.get("workspace_id") or workspace_id
        if not self._check_project(task):
            return self._forbid("task %s" % task_id)
        if task.get("status") == RUNNING:
            self.app.cancel_task(owner, task_id)
        # Tell the listeners first, then take the record away: the event is
        # the last thing the task says.
        self.app.publish("task_deleted", task)
        if not self.app.store.delete(owner, task_id):
            return self._error(404, "not_found", "no task %r" % task_id)
        self._json(200, {"ok": True, "id": task_id})

    def _action(self, task_id, action):
        # The task id is unique, so the workspace is optional: look the task up
        # and use the workspace it says it belongs to.
        workspace_id = self._one("workspace_id")
        task = self.app.store.load(workspace_id, task_id)
        if task is None:
            return self._error(404, "not_found", "no task %r" % task_id)
        workspace_id = task.get("workspace_id") or workspace_id
        if not self._check_project(task):
            return self._forbid("task %s" % task_id)
        if action == "start":
            return self._start(workspace_id, task)
        if action == "cancel":
            return self._cancel(workspace_id, task)
        if action == "confirm":
            return self._confirm(workspace_id, task)
        if action == "retry":
            return self._retry(workspace_id, task)
        if action == "fail":
            return self._fail(workspace_id, task)
        return self._error(404, "not_found", "no such action: %s" % action)

    def _start(self, workspace_id, task):
        if task.get("status") == RUNNING:
            return self._error(409, "task_running", "task %s is already running" % task.get("id"))
        if task.get("kind") == "plan":
            return self._error(409, "bad_request",
                               "task %s is a plan: its steps are the work, "
                               "the plan itself is not run" % task.get("id"))
        provider = task.get("provider") or self.app.engine.effective_provider()
        # The same gate the scheduler uses: the work starts while there is quota.
        left, _ = self.app.engine.quota_left(provider)
        if left is None and not self.app.args.allow_without_quota:
            return self._error(409, "no_quota",
                               "provider %r does not report a quota (use --allow-without-quota)"
                               % provider)
        if left is not None and left < self.app.args.min_remaining:
            return self._error(409, "no_quota",
                               "quota of %r: %s%% left (need %d%%)"
                               % (provider, left, self.app.args.min_remaining))
        self.app.engine.start_task(task, provider)
        self._json(200, self.app.store.load(workspace_id, task.get("id")) or task)

    def _cancel(self, workspace_id, task):
        if task.get("status") in TERMINAL:
            return self._error(409, "already_finished",
                               "task %s is %s - it cannot be cancelled"
                               % (task.get("id"), task.get("status")))
        self.app.cancel_task(workspace_id, task.get("id"))
        self.app.store.audit("cancel", task.get("id"))
        self._json(200, self.app.store.load(workspace_id, task.get("id")) or task)

    def _confirm(self, workspace_id, task):
        body = self._body()
        if body is None:
            return self._error(400, "bad_request", "the request body is not JSON")
        message = str(body.get("message") or body.get("response") or "").strip()
        if not message:
            return self._error(400, "bad_request", "'message' is required")
        # The answer may name the question it answers. A mismatch is a
        # conflict, not a new answer: the question that is open is the one
        # that gets answered.
        wanted = str(body.get("interaction_id") or "").strip()
        current = str((task.get("interaction") or {}).get("interaction_id") or "")
        if wanted and current and wanted != current:
            return self._error(409, "bad_request",
                               "interaction %r is not the question that is open (%r)"
                               % (wanted, current))
        if not task.get("needs_confirmation"):
            # Replying twice to the same question must not queue the task
            # twice: the answer is already there, so this is a no-op.
            if task.get("pending_message"):
                self._json(200, self.app.decorate(task))
                return
            return self._error(409, "not_waiting",
                               "task %s is not waiting for an answer" % task.get("id"))
        # The answer goes back into the queue - at the end of the line, and
        # without interrupting whatever is running now. The task carries the
        # answer with it: the agent picks it up as the next turn in the same
        # session.
        self.app.store.update(workspace_id, task.get("id"), lambda t: t.update({
            "status": QUEUED,
            "needs_confirmation": False,
            "question": "",
            "interaction": {},
            "pending_message": message,
            "note": "answered: " + message[:80],
        }))
        self.app.publish("task_answered", self.app.store.load(workspace_id, task.get("id")))
        self.app.store.audit("answer", task.get("id"), {"message": message[:200]})
        self._json(200, self.app.decorate(self.app.store.load(workspace_id, task.get("id")) or task))

    def _retry(self, workspace_id, task):
        """Try a failed task again: the same task, one attempt further."""
        if task.get("status") not in (FAILED, CANCELLED):
            return self._error(409, "not_failed",
                               "task %s is %s - only a failed or cancelled task is retried"
                               % (task.get("id"), task.get("status")))
        self.app.store.update(workspace_id, task.get("id"), lambda t: t.update({
            "status": QUEUED,
            "error": "",
            "finished_at": "",
            "progress": 0,
            "needs_confirmation": False,
            "question": "",
            "note": "attempt %d" % (int(t.get("attempt") or 1) + 1),
            "attempt": int(t.get("attempt") or 1) + 1,
        }))
        self.app.publish("task_retried", self.app.store.load(workspace_id, task.get("id")))
        self.app.store.audit("retry", task.get("id"))
        self._json(200, self.app.decorate(self.app.store.load(workspace_id, task.get("id")) or task))

    def _fail(self, workspace_id, task):
        """Mark a running task failed - the watchdog's way to stop a stuck one.

        The reason is required: a failed task without a reason is a task
        somebody has to guess about later.
        """
        body = self._body()
        if body is None:
            return self._error(400, "bad_request", "the request body is not JSON")
        reason = str(body.get("reason") or "").strip()
        if not reason:
            return self._error(400, "bad_request", "'reason' is required")
        if task.get("status") in TERMINAL:
            return self._error(409, "already_finished",
                               "task %s is %s - it cannot be failed"
                               % (task.get("id"), task.get("status")))
        written = self.app.engine.fail_task(workspace_id, task.get("id"), reason)
        if written is None:
            return self._error(409, "already_finished",
                               "task %s is already finished" % task.get("id"))
        self._json(200, self.app.decorate(written))

    def _submit_trigger(self):
        """One outside event comes in; the queue decides what to do with it.

        The trigger entry is the door every outside event comes through: a
        webhook, a mail, a chat. It decides (rules first, then the model)
        and only an accepted trigger becomes a task.
        """
        body = self._body()
        if body is None:
            return self._error(400, "bad_request", "the request body is not JSON")
        workspace_id = str(body.get("workspace_id") or "").strip()
        caller = self._project_of_caller()
        if caller and workspace_id and workspace_id != caller:
            return self._forbid("project %s" % workspace_id)
        if not workspace_id:
            workspace_id = self.app.default_workspace()
        if not workspace_id:
            return self._error(400, "bad_request",
                               "'workspace_id' is required (the agent has no workspace)")
        if caller and workspace_id != caller:
            return self._forbid("project %s" % workspace_id)
        body["workspace_id"] = workspace_id
        try:
            trigger, duplicate = self.app.trigger_service.submit(body)
        except triggers.TriggerError as exc:
            return self._error(400, "bad_request", str(exc))
        if not duplicate:
            self.app.events.publish({"type": "trigger_received", "trigger": trigger})
            self.app.events.publish({"type": "trigger_decided", "trigger": trigger})
            self.app.store.audit("trigger", trigger.get("id") or "",
                                 {"source": trigger.get("source") or "",
                                  "decision": trigger.get("decision") or "",
                                  "task_id": trigger.get("task_id") or ""})
        self._json(200, {"duplicate": duplicate, "trigger": trigger})

    def _list_triggers(self):
        caller = self._project_of_caller()
        workspace_id = caller or self._one("workspace_id")
        items = self.app.trigger_store.list_triggers(
            source=self._one("source"), decision=self._one("decision"),
            workspace_id=workspace_id, limit=self._int("limit", 100) or 100)
        self._json(200, {"triggers": items, "count": len(items)})

    def _get_trigger(self, trigger_id):
        trigger = self.app.trigger_store.load_trigger(trigger_id)
        if trigger is None:
            return self._error(404, "not_found", "no trigger %r" % trigger_id)
        if not self._check_project({"workspace_id": trigger.get("workspace_id") or ""}):
            return self._forbid("trigger %s" % trigger_id)
        self._json(200, trigger)

    def _list_plans(self):
        items = self.app.plan_store.list_plans()
        self._json(200, {"plans": items, "count": len(items)})

    def _get_plan(self, plan_id):
        plan = self.app.plan_store.load(plan_id)
        if plan is None:
            return self._error(404, "not_found", "no plan %r" % plan_id)
        if not self._check_project({"workspace_id": plan.get("workspace_id") or ""}):
            return self._forbid("plan %s" % plan_id)
        self._json(200, self.app.plan_view(plan))

    def _create_plan(self):
        body = self._body()
        if body is None:
            return self._error(400, "bad_request", "the request body is not JSON")
        # A plan file (what the planner wrote) may be handed in as it is.
        inner = body.get("plan")
        if isinstance(inner, dict):
            merged = dict(inner)
            for key, value in body.items():
                if key != "plan" and value not in (None, ""):
                    merged.setdefault(key, value)
            body = merged
        header_key = str(self.headers.get("Idempotency-Key") or "").strip()
        if header_key:
            body["idempotency_key"] = header_key
        try:
            plan, duplicate = self.app.create_plan(body)
        except plans.PlanError as exc:
            return self._error(400, "bad_request", str(exc))
        self._json(200, dict(self.app.plan_view(plan), duplicate=duplicate))

    def _cancel_plan(self, plan_id):
        plan = self.app.cancel_plan(plan_id)
        if plan is None:
            return self._error(404, "not_found", "no plan %r" % plan_id)
        self._json(200, self.app.plan_view(plan))

    def _list_threads(self):
        caller = self._project_of_caller()
        workspace_id = caller or self._one("workspace_id")
        items = self.app.trigger_store.list_threads(workspace_id=workspace_id)
        self._json(200, {"threads": items, "count": len(items)})

    def _get_thread(self, thread_id):
        thread = self.app.trigger_store.load_thread(thread_id)
        if thread is None:
            return self._error(404, "not_found", "no thread %r" % thread_id)
        if not self._check_project({"workspace_id": thread.get("workspace_id") or ""}):
            return self._forbid("thread %s" % thread_id)
        thread = dict(thread)
        tasks = []
        for task_id in thread.get("task_ids") or []:
            task = self.app.store.load("", task_id)
            if task:
                tasks.append(self.app.decorate(task))
        thread["tasks"] = tasks
        self._json(200, thread)

    def _create_thread(self):
        """Open a thread by hand - a conversation that did not start with a trigger."""
        body = self._body() or {}
        key = str(body.get("key") or "").strip()
        if not key:
            return self._error(400, "bad_request", "'key' is required")
        workspace_id = str(body.get("workspace_id") or "").strip()
        caller = self._project_of_caller()
        if caller and workspace_id and workspace_id != caller:
            return self._forbid("project %s" % workspace_id)
        if not workspace_id:
            workspace_id = self.app.default_workspace()
        if not workspace_id:
            return self._error(400, "bad_request",
                               "'workspace_id' is required (the agent has no workspace)")
        thread_id = self.app.trigger_store.thread_id(workspace_id, key)
        thread = self.app.trigger_store.attach(thread_id, workspace_id, key)
        self._json(200, thread)

    def _thread_context(self, thread_id):
        """What a thread knows: the window, what fell out of it, and the prompt
        the next task of this thread would get."""
        thread = self.app.trigger_store.load_thread(thread_id)
        if thread is None:
            return self._error(404, "not_found", "no thread %r" % thread_id)
        if not self._check_project({"workspace_id": thread.get("workspace_id") or ""}):
            return self._forbid("thread %s" % thread_id)
        self._json(200, {
            "id": thread_id,
            "key": thread.get("key") or "",
            "workspace_id": thread.get("workspace_id") or "",
            "task_ids": thread.get("task_ids") or [],
            "context": thread.get("context") or [],
            "spill": thread.get("spill") or [],
            "prompt": self.app.trigger_store.context_of(thread_id),
        })

    def _thread_spill(self, thread_id):
        """The router writes the summary of what fell out of the window.

        Only the local operator may do this: a summary is part of what the
        model is told, and a project key must not be able to write it.
        """
        if self._key():
            return self._error(403, "forbidden",
                               "only the local operator may write a thread summary")
        body = self._body() or {}
        summary = str(body.get("summary") or "").strip()
        if not summary:
            return self._error(400, "bad_request", "'summary' is required")
        thread = self.app.trigger_store.load_thread(thread_id)
        if thread is None:
            return self._error(404, "not_found", "no thread %r" % thread_id)
        written = self.app.trigger_store.set_summary(thread_id, summary,
                                                      int(body.get("count") or 0))
        self.app.events.publish({"type": "thread_summarized", "thread": written})
        self._json(200, written)

    def _queue_state(self):
        """Is the scheduler paused, and why."""
        state = self.app.store.state()
        state["counts"] = self.app.store.counts()
        self._json(200, state)

    def _queue_pause(self, paused):
        """Stop (or start) the scheduler.

        The pause is an operator decision and it lives in a file, so it
        survives a restart of this service: a queue that starts working again
        by itself is a queue nobody can trust to stay quiet.
        """
        body = self._body() or {}
        reason = str(body.get("reason") or "").strip()
        state = self.app.store.set_paused(paused, reason)
        # No task on the event: this is about the whole queue, so every
        # channel may hear it.
        self.app.events.publish({"type": "queue_paused" if paused else "queue_resumed",
                                 "reason": reason})
        self.app.store.audit("queue_pause" if paused else "queue_resume", "",
                             {"reason": reason})
        self._json(200, state)

    def _publish_event(self):
        """Let an outside service put an event into the queue's stream.

        The watchdog needs it: an alert about a stuck task has to reach the
        person who queued it, and the event stream is already the road to
        that person (the chat front ends listen to it).

        Only the local operator may do this. A project key must not be able
        to forge events - "task_done" for somebody else's task would be a
        lie that the queue believes.
        """
        if self._key():
            return self._error(403, "forbidden",
                               "only the local operator may publish events")
        body = self._body()
        if body is None:
            return self._error(400, "bad_request", "the request body is not JSON")
        kind = str(body.get("type") or "").strip()
        if not kind:
            return self._error(400, "bad_request", "'type' is required")
        event = {"type": kind}
        task_id = str(body.get("task_id") or "").strip()
        if task_id:
            task = self.app.store.load(str(body.get("workspace_id") or ""), task_id)
            if task is None:
                return self._error(404, "not_found", "no task %r" % task_id)
            if not self._check_project(task):
                return self._forbid("task %s" % task_id)
            event["task"] = task
        for key in ("check", "level", "reason", "action", "source",
                    # The router's words: one sentence per event. The chat
                    # front ends deliver them instead of translating the
                    # events themselves (that is the "one voice" part).
                    "line", "speech_of"):
            if key in body:
                event[key] = body[key]
        self.app.events.publish(event)
        self._json(200, {"ok": True, "type": kind})

    def _events(self):
        """The queue's event stream (SSE).

        A caller that presents a project key sees only that project's events:
        the stream is a broadcast, so without this filter a key would learn
        what every other project is doing (the prompt text travels with the
        event). The local operator (no key) sees everything.
        """
        caller = self._project_of_caller()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        queue, state, listener = self.app.events.subscribe()
        idle = 0.0
        try:
            while state["alive"]:
                if queue:
                    event = queue.pop(0)
                    if caller and not self._event_belongs_to(event, caller):
                        continue
                    frame = "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"
                    self.wfile.write(frame.encode("utf-8"))
                    self.wfile.flush()
                    idle = 0.0
                else:
                    time.sleep(0.3)
                    idle += 0.3
                    # A client that is gone (a closed tab, a dead network) must
                    # not keep a thread of this server for ever.
                    if self._client_gone():
                        break
                    if idle >= self.app.args.sse_idle_timeout:
                        log("event stream idle for %d s - closing it"
                            % self.app.args.sse_idle_timeout)
                        break
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.app.events.unsubscribe(listener)

    def _client_gone(self):
        """Is the client still there? A closed tab shows up as an empty read."""
        try:
            ready, _, _ = select.select([self.connection], [], [], 0)
            if not ready:
                return False
            return not self.connection.recv(1024, socket.MSG_PEEK)
        except (OSError, ValueError):
            return True

    @staticmethod
    def _event_belongs_to(event, workspace_id):
        """Does this event belong to that project?

        An event WITHOUT a task is an operator-level event (the queue is
        paused, the agent is down, the watchdog saw something): it carries no
        project data, so it goes to everybody. An event WITH a task only goes
        to the project that owns it - a key must not learn what the other
        projects are doing.
        """
        task = event.get("task")
        if task:
            return (task.get("workspace_id") or "") == workspace_id
        trigger = event.get("trigger")
        if trigger:
            return (trigger.get("workspace_id") or "") == workspace_id
        return True


class App:
    """Everything the handler needs, in one place."""

    def __init__(self, args):
        self.args = args
        self._page = None
        self._task_ws_id = ""
        # The rate limiter's memory: one list of timestamps per key (the
        # tenant's). It is in memory on purpose - a limit that survives a
        # restart is a limit nobody expects.
        self.rate_lock = threading.Lock()
        self.rate_hits = {}
        data_dir = args.data_dir
        if not data_dir:
            data_dir = os.path.join(os.path.abspath(os.path.expanduser(args.workspace_path)),
                                    "tasks")
        self.store = Store(data_dir)
        self.agent = Agent(args.agent, timeout=60,
                           chat_timeout=getattr(args, "chat_timeout", 3600))
        self.events = Events(self.store)
        # The trigger entry: one door for every outside event (a webhook, a
        # mail, a chat). The records live in their own directories next to
        # the task records.
        self.trigger_store = triggers.TriggerStore(data_dir)
        # The plans (the complex jobs that were split into steps).
        self.plan_store = plans.PlanStore(data_dir)
        self.engine = Engine(self.store, self.agent, self.events, args, self.trigger_store,
                             self.write_thread_file, self.thread_file_path,
                             self.decorate_event, self.plan_store)
        self.trigger_rules = triggers.Rules(getattr(args, "trigger_config", ""))
        self.trigger_service = triggers.TriggerService(
            self.trigger_store, self.trigger_rules,
            judge=self.judge_trigger, make_task=self.make_task_from_trigger)

    def judge_trigger(self, trigger):
        """Ask the model whether this trigger is worth doing.

        The rules already had their say; this is the last word and it costs
        money, so the call is as small as it can be. When the model cannot
        be reached the decision is left empty - the service then keeps the
        work rather than losing it, and says so in the reason.
        """
        system = ("你是任务分派助手。有人会给一条外部消息（可能是工作请求，也可能是闲聊、"
                 "通知、广告）。判断这条消息值不值得让 agent 去做一件事。\n"
                 "值得做的：有人明确要求做事、报告了故障或失败、需要一个决定。\n"
                 "不值得做的：打招呼、广告、订阅摘要、自动通知、跟这里的事无关的闲聊。\n"
                 "只输出一个 JSON 对象，不要解释，不要 markdown 代码块：\n"
                 "{\"decision\": \"accept\" 或 \"ignore\", \"reason\": \"一句话\"}")
        prompt = "外部消息（来源：%s）：\n%s" % (trigger.get("source") or "?", trigger.get("text") or "")
        try:
            answer = self.agent.llm_chat(
                prompt=prompt, system=system,
                provider=self.engine.effective_provider() or None,
                model=self.engine.effective_model() or None,
                temperature=0.0, max_tokens=300,
                timeout=getattr(self.args, "trigger_timeout", 60))
        except AgentError as exc:
            return "", "模型没答上来：%s" % str(exc)[:160]
        content = answer.get("content") or ""
        if not content:
            # Some models put the answer in their thinking and leave the body
            # empty - the mail watcher learned that the hard way.
            content = answer.get("reasoning") or ""
        decision, reason = triggers.parse_decision(content)
        return decision, reason

    def make_task_from_trigger(self, trigger, body):
        """The task an accepted trigger becomes."""
        meta = {}
        if isinstance(body.get("meta"), dict):
            meta = dict(body["meta"])
        meta["trigger_id"] = trigger.get("id") or ""
        meta["source"] = trigger.get("source") or ""
        not_before = ""
        not_before_text = ""
        if body.get("not_before"):
            not_before_text = str(body["not_before"]).strip()
            not_before = parse_not_before(not_before_text) or ""
        task = new_task({
            "prompt": trigger.get("text") or "",
            "title": str(body.get("title") or "")[:80],
            "provider": str(body.get("provider") or ""),
            "model": str(body.get("model") or ""),
        }, trigger.get("workspace_id") or "",
            priority=int(body.get("priority") or 0),
            not_before=not_before, not_before_text=not_before_text, meta=meta)
        task["trigger_id"] = trigger.get("id") or ""
        task["thread_id"] = trigger.get("thread_id") or ""
        # Where the work runs: "" = the local agent, "fleet" = the devices'.
        task["target"] = str(body.get("target") or "").strip()
        if isinstance(body.get("fleet"), dict):
            task["fleet"] = dict(body["fleet"])
        self.store.create(task)
        self.publish("task_created", task)
        self.store.audit("submit", task["id"],
                         {"prompt": task.get("prompt", "")[:200],
                          "trigger_id": task["trigger_id"]})
        return task

    def create_plan(self, body):
        """One complex job, split into steps: the plan and its tasks.

        The plan becomes one parent task (the container: it holds the final
        result) and one child per step. The children may depend on each
        other; the queue starts a child only when what it waits for is done.

        The body:
            title            what the whole thing is about
            goal             the goal the steps serve
            steps            the steps: [{key, title, prompt, depends_on}]
            workspace_id     where the steps run (the queue's own by default)
            thread_key       the conversation the steps belong to
            aggregate_prompt how the result should be put together

        Returns (plan, duplicate).
        """
        steps = plans.normalize_steps(body.get("steps"))
        workspace_id = str(body.get("workspace_id") or "").strip()
        if not workspace_id:
            workspace_id = self.default_workspace()
        if not workspace_id:
            raise plans.PlanError("'workspace_id' is required (the agent has no workspace)")
        title = str(body.get("title") or "").strip() or (steps[0].get("title") or "")
        goal = str(body.get("goal") or "").strip() or title
        key = str(body.get("idempotency_key") or "").strip()
        if key:
            existing = self.plan_store.find_by_key(key)
            if existing is not None:
                return existing, True
        plan_id = self.plan_store.new_plan_id()
        # The steps are one conversation: the router remembers what the other
        # steps did, and the model can read the plan's whole record.
        thread_key = str(body.get("thread_key") or "").strip() or ("plan:" + plan_id)
        thread_id = self.trigger_store.thread_id(workspace_id, thread_key)
        self.trigger_store.attach(thread_id, workspace_id, thread_key, title=title)

        # The whole plan runs on one provider (and one model): the steps
        # inherit it, and the summary is made with it too.
        plan_provider = (str(body.get("provider") or "").strip()
                         or self.engine.effective_provider())
        plan_model = str(body.get("model") or "").strip()
        parent = new_task({"prompt": goal, "title": title[:80],
                           "provider": plan_provider, "model": plan_model},
                          workspace_id)
        parent["kind"] = "plan"
        parent["plan_id"] = plan_id
        parent["thread_id"] = thread_id
        parent["note"] = "计划：%d 步" % len(steps)
        self.store.create(parent)
        self.trigger_store.attach(thread_id, workspace_id, thread_key,
                                  task_id=parent["id"], title=title)
        self.publish("task_created", parent)

        children = []
        for step in steps:
            child = new_task({"prompt": step["prompt"],
                              "title": step["title"][:80],
                              "provider": step.get("provider") or plan_provider,
                              "model": step.get("model") or plan_model},
                             step.get("workspace_id") or workspace_id)
            child["kind"] = "step"
            child["plan_id"] = plan_id
            child["parent_id"] = parent["id"]
            child["thread_id"] = thread_id
            self.store.create(child)
            children.append((step, child))
            self.trigger_store.attach(thread_id, workspace_id, thread_key,
                                      task_id=child["id"])
            self.publish("task_created", child)
            self.store.audit("submit", child["id"],
                             {"prompt": child.get("prompt", "")[:200],
                              "plan_id": plan_id})

        # The dependencies are the tasks of the steps that are waited for.
        by_key = dict((step["key"], child["id"]) for step, child in children)
        for step, child in children:
            deps = [by_key.get(name, "") for name in (step.get("depends_on") or [])]
            deps = [one for one in deps if one]
            if deps:
                self.store.update(child["workspace_id"], child["id"],
                                  lambda t: t.update({"depends_on": deps}))
                child["depends_on"] = deps
        self.store.update(parent["workspace_id"], parent["id"],
                          lambda t: t.update({"children": [c["id"] for _, c in children]}))

        plan = {
            "id": plan_id,
            "title": title,
            "goal": goal,
            "aggregate_prompt": str(body.get("aggregate_prompt") or "").strip(),
            "workspace_id": workspace_id,
            "provider": plan_provider,
            "model": plan_model,
            "thread_id": thread_id,
            "parent_id": parent["id"],
            "steps": [{"key": step["key"], "title": step["title"],
                       "task_id": child["id"],
                       "depends_on": list(step.get("depends_on") or [])}
                      for step, child in children],
            "status": "running",
            "attempts": 0,
            "idempotency_key": key,
            "created_at": now_iso(),
            "updated_at": now_iso(),
        }
        self.plan_store.save(plan)
        self.store.audit("plan", parent["id"], {"plan_id": plan_id,
                                                "steps": len(steps)})
        return plan, False

    def plan_view(self, plan):
        """The plan, with the tasks it became."""
        if not plan:
            return {}
        workspace_id = plan.get("workspace_id") or ""
        parent = self.store.load(workspace_id, plan.get("parent_id") or "")
        # The queue order is read once for the whole answer: asking for it
        # per step would scan the whole ledger per step.
        order = self.queue_order()
        steps = []
        for step in plan.get("steps") or []:
            child = self.store.load(workspace_id, step.get("task_id") or "")
            steps.append({"key": step.get("key"), "title": step.get("title"),
                          "depends_on": step.get("depends_on") or [],
                          "task": self.decorate(child, order=order) if child else None})
        return {"plan": plan,
                "parent": self.decorate(parent, order=order) if parent else None,
                "steps": steps}

    def cancel_plan(self, plan_id):
        """Stop a plan: the parent and every step that is still open."""
        plan = self.plan_store.load(plan_id)
        if plan is None:
            return None
        workspace_id = plan.get("workspace_id") or ""
        stopped = []
        for step in plan.get("steps") or []:
            child = self.store.load(workspace_id, step.get("task_id") or "")
            if child is None or child.get("status") in TERMINAL:
                continue
            if self.cancel_task(workspace_id, child["id"]):
                stopped.append(child["id"])
        parent = self.store.load(workspace_id, plan.get("parent_id") or "")
        if parent is not None and parent.get("status") not in TERMINAL:
            self.cancel_task(workspace_id, parent["id"])
        plan["status"] = "cancelled"
        self.plan_store.save(plan)
        return plan

    def thread_file_path(self, workspace_id, thread_id):
        """Where the full record of a thread lives, relative to the workspace.

        It goes into the workspace's own run directory: that is the one place
        the agent can reach with the tools it already has (toolkit_file), so
        "read the older part of this conversation" needs no new tool.
        """
        if not workspace_id or not thread_id:
            return ""
        return ".tea_agent_run/threads/%s.md" % thread_id

    def write_thread_file(self, thread_id):
        """Write the thread's full record into the workspace.

        The queue writes into the agent's run directory on purpose: it is
        the agent's own directory, and the agent's file tools can read it
        by path. If the workspace is gone (or the agent is down) the record
        simply is not written - the inline context still works.
        """
        thread = self.trigger_store.load_thread(thread_id)
        if thread is None:
            return False
        workspace_id = thread.get("workspace_id") or ""
        rel = self.thread_file_path(workspace_id, thread_id)
        if not rel:
            return False
        root = self.workspace_path(workspace_id)
        if not root:
            return False
        path = os.path.join(root, rel)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            body = self.trigger_store.history_md(thread_id)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(body)
            os.replace(tmp, path)
        except OSError:
            return False
        return True

    def page_html(self):
        """The page, read once at startup (it is a static file next to us)."""
        if self._page is None:
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "task_ui.html")
            try:
                with open(path, encoding="utf-8") as f:
                    self._page = f.read()
            except OSError:
                self._page = ""
        return self._page

    def decorate_event(self, event):
        """Add the thread (its title) to an event.

        The router says things about conversations, not only about task ids:
        "做完了 t_x（字体设置）" says more than "做完了 t_x".
        """
        task = event.get("task") or {}
        thread_id = task.get("thread_id") or ""
        if not thread_id or event.get("thread"):
            return event
        thread = self.trigger_store.load_thread(thread_id)
        if thread:
            event["thread"] = {"id": thread_id,
                               "title": thread.get("title") or "",
                               "key": thread.get("key") or ""}
        return event

    def publish(self, event_type, task, **extra):
        """Tell everybody who is listening (and write the event down)."""
        event = {"type": event_type, "task": task}
        event.update(extra)
        self.events.publish(self.decorate_event(event))
        return event

    def decorate(self, task, full=False, order=None):
        """Add the bits a reader wants: where the record is, which workspace,
        the agent to reach, and where the task stands in the queue.

        `order` is the queue order of the answer (see queue_order): a list of
        many tasks gets it computed once, not once per task.
        """
        task = dict(task)
        task["record_path"] = self.store.record_path(task.get("workspace_id"), task.get("id"))
        task["agent_url"] = self.agent.base
        path = self.workspace_path(task.get("workspace_id"))
        if path:
            task["workspace_path"] = path
        # Where the task stands: only a queued task has a place in the line.
        task["queue_position"] = self.queue_position(task, order)
        task["current_step"] = task.get("note") or ""
        # When the task may start: the absolute time and how it was written.
        not_before = task.get("not_before") or ""
        task["not_before"] = not_before
        task["not_before_text"] = task.get("not_before_text") or ""
        if not_before:
            stamp = parse_iso(not_before)
            task["starts_at_local"] = (time.strftime("%Y-%m-%d %H:%M", time.localtime(stamp))
                                       if stamp is not None else not_before)
            task["starts_in_s"] = int(stamp - time.time()) if stamp is not None else 0
        else:
            task["starts_at_local"] = ""
            task["starts_in_s"] = 0
        if task.get("kind") == "plan":
            task["child_count"] = len(task.get("children") or [])
        # The files the task produced: each one gets a link the reader can
        # click. The queue serves the file itself, so the link also works when
        # the agent lives on another machine (a browser cannot reach it there).
        artifacts = []
        for art in (task.get("artifacts") or []):
            if not isinstance(art, dict):
                continue
            item = dict(art)
            path = str(item.get("path") or "")
            if path:
                item["name"] = path.replace("\\", "/").rsplit("/", 1)[-1]
                item["download_url"] = ("/api/tasks/%s/download?path=%s"
                                        % (urllib.parse.quote(task.get("id") or ""),
                                           urllib.parse.quote(path)))
                item["agent_download_url"] = (
                    "%s/api/workspaces/%s/download?path=%s"
                    % (self.agent.base,
                       urllib.parse.quote(task.get("workspace_id") or ""),
                       urllib.parse.quote(path)))
            artifacts.append(item)
        task["artifacts"] = artifacts
        # The whole result as one document (or as one zip).
        task["export_url"] = ("/api/tasks/%s/export?format=zip"
                              % urllib.parse.quote(task.get("id") or ""))
        if full:
            task["events"] = self.store.events(task.get("id") or "")
            task["history"] = task.get("steps") or []
        return task

    def queue_order(self):
        """Where every queued task stands in the line: {task id: position}.

        The line is the same for every task of one answer, so it is read once
        per answer instead of once per task.
        """
        queued = self.store.list(status=QUEUED)
        queued.sort(key=lambda t: (-int(t.get("priority") or 0),
                                   t.get("created_at") or "", t.get("id") or ""))
        return dict((one.get("id"), index + 1) for index, one in enumerate(queued))

    def queue_position(self, task, order=None):
        """1 = next in line, 0 = not queued (nothing to wait for)."""
        if not task or task.get("status") != QUEUED:
            return 0
        if order is None:
            order = self.queue_order()
        return order.get(task.get("id"), 0)

    def metrics(self):
        """What somebody watching the queue wants to see.

        The queue length and how long the work waits are the two numbers that
        say whether the queue is healthy; the failure rate says whether the
        work itself is.
        """
        tasks = self.store.list()
        counts = self.store.counts()
        waits = []
        durations = []
        failed = 0
        finished = 0
        for task in tasks:
            created = parse_iso(task.get("created_at") or "")
            started = parse_iso(task.get("started_at") or "")
            ended = parse_iso(task.get("finished_at") or "")
            if task.get("status") == QUEUED and created is not None:
                waits.append(max(0, time.time() - created))
            if started is not None and ended is not None:
                durations.append(max(0, ended - started))
            if task.get("status") in (DONE, FAILED):
                finished += 1
                if task.get("status") == FAILED:
                    failed += 1
        return {
            "queue_length": counts.get(QUEUED, 0),
            "running": counts.get(RUNNING, 0),
            "waiting_interaction": counts.get(WAITING, 0),
            "done": counts.get(DONE, 0),
            "failed": counts.get(FAILED, 0),
            "cancelled": counts.get(CANCELLED, 0),
            "avg_queue_wait_s": round(sum(waits) / len(waits), 1) if waits else 0.0,
            "avg_task_duration_s": (round(sum(durations) / len(durations), 1)
                                    if durations else 0.0),
            "failure_rate": (round(failed / float(finished), 3) if finished else 0.0),
        }

    def workspace_name(self, workspace_id):
        try:
            for ws in self.agent.workspaces():
                if ws.get("id") == workspace_id:
                    return ws.get("name") or ""
        except AgentError:
            pass
        return ""

    def workspace_paths(self):
        """workspace id -> path, read from the agent at most once every few
        seconds.

        The task list decorates every task with its workspace path, and a page
        of a few hundred tasks means a few hundred questions to the agent. When
        the agent is down (or merely slow) each one is a connect timeout, so the
        list took minutes instead of milliseconds. One cached answer serves the
        whole page; a workspace registered meanwhile shows up five seconds
        later.
        """
        cached = getattr(self, "_ws_paths", None)
        now = time.time()
        if cached and now - cached[0] < 5.0:
            return cached[1]
        paths = {}
        try:
            for ws in self.agent.workspaces():
                paths[ws.get("id") or ""] = ws.get("path", "")
        except AgentError:
            pass
        self._ws_paths = (now, paths)
        return paths

    def workspace_path(self, workspace_id):
        if not workspace_id:
            return ""
        return self.workspace_paths().get(workspace_id, "")

    def task_workspace_id(self):
        """The queue's OWN workspace: a directory of its own, registered with
        the agent so the agent can work there.

        The queue must not scribble into somebody's project: a background task
        that writes files belongs in a directory that is nobody's checkout. So
        the service keeps one workspace of its own (by default under the tea
        home), and every task that does not name a workspace works there.
        """
        if self.args.workspace:
            return self.args.workspace        # the operator named one
        if self._task_ws_id:
            return self._task_ws_id
        path = os.path.abspath(os.path.expanduser(self.args.workspace_path))
        try:
            os.makedirs(path, exist_ok=True)
            for ws in self.agent.workspaces():
                if os.path.abspath(ws.get("path", "")) == path:
                    self._task_ws_id = ws.get("id", "")
                    return self._task_ws_id
            created = self.agent.register_workspace(path, self.args.workspace_name)
            self._task_ws_id = created.get("id", "")
            log("task workspace: %s (%s)" % (path, self._task_ws_id or "?"))
        except (AgentError, OSError) as exc:
            log("cannot prepare the task workspace %s: %s" % (path, exc))
        return self._task_ws_id

    def doc_path(self, name="tasks.md"):
        """Where a documentation file is.

        docs/ in the repository - the same files the developer wrote, so
        there is no second copy to fall behind. The name may not leave the
        directory: no slashes, no "..".
        """
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        name = os.path.basename(str(name or "").strip())
        if not name or not name.endswith(".md") or name.startswith("."):
            return ""
        return os.path.join(repo, "docs", name)

    def version(self):
        """Which queue this is.

        The queue's **own** version: a client asks the queue whether the
        queue's API knows something (the version segment arrived in 0.21.0),
        and the agent's version is not the answer to that question - it is
        merely the version of the sibling in the same package. The agent's
        version is reported separately (agent_version).
        """
        return QUEUE_VERSION

    def agent_version(self):
        """Which agent is behind the queue (empty when it cannot be reached)."""
        try:
            health = self.agent.health()
            return str((health or {}).get("version") or "")
        except (AgentError, AttributeError):
            return ""

    def default_workspace(self):
        """The workspace a task goes to when the caller does not name one."""
        own = self.task_workspace_id()
        if own:
            return own
        wanted = self.args.workspace
        try:
            workspaces = self.agent.workspaces()
        except AgentError:
            return wanted
        if not workspaces:
            return wanted
        if wanted:
            for ws in workspaces:
                if ws.get("id") == wanted:
                    return wanted
        return workspaces[0].get("id", "")

    def cancel_task(self, workspace_id, task_id):
        task = self.store.load(workspace_id, task_id)
        if task is None:
            return False
        if task.get("status") in TERMINAL:
            # A finished task is history: cancelling it would rewrite what
            # happened.
            return False
        session_id = task.get("session_id")
        if session_id:
            try:
                self.agent.stop(session_id)
            except AgentError as exc:
                log("could not stop the turn of %s: %s" % (task_id, exc))
        self.store.update(workspace_id, task_id, lambda t: t.update({
            "status": CANCELLED, "finished_at": now_iso()}))
        self.events.publish({"type": "task_cancelled", "task": self.store.load(workspace_id, task_id)})
        return True


def pick_workspace(agent, wanted):
    workspaces = agent.workspaces()
    if not workspaces:
        raise SystemExit("the agent has no workspaces; add one first")
    if wanted:
        for ws in workspaces:
            if ws.get("id") == wanted:
                return wanted
        raise SystemExit("no workspace with id %r (known: %s)"
                         % (wanted, ", ".join(w.get("id", "?") for w in workspaces)))
    return workspaces[0].get("id")


def submit_order(app, args):
    """Read a task order file and queue it.

    The order is a text file the user fills in (see task-order.template). It
    carries every setting a task can have, so a task can be handed over as a
    file instead of a curl.
    """
    try:
        with open(args.submit_order, encoding="utf-8") as f:
            text = f.read()
    except OSError as exc:
        print("cannot read the task order: %s" % exc, file=sys.stderr)
        return 2
    try:
        body = task_order.parse_order(text)
    except task_order.OrderError as exc:
        print("the task order has a problem:\n%s" % exc, file=sys.stderr)
        return 2
    if not body.get("prompt"):
        print("the task order has no 'prompt' - what should be done?", file=sys.stderr)
        return 2
    not_before, not_before_text = "", ""
    if body.get("not_before"):
        not_before, not_before_text = parse_not_before(str(body["not_before"]))
        if not not_before:
            print("'not_before' is not a time I understand: %r" % body["not_before"],
                  file=sys.stderr)
            return 2
    agent = Agent(args.agent)
    try:
        workspace_id = resolve_workspace_id(agent, body.get("workspace_id"))
    except (AgentError, task_order.OrderError) as exc:
        print("cannot pick the workspace: %s" % exc, file=sys.stderr)
        return 2
    if not workspace_id:
        workspace_id = app.default_workspace()
    allow_paths = [os.path.abspath(os.path.expanduser(p))
                   for p in (body.get("allow_paths") or []) if str(p).strip()]
    model_params, params_error = parse_model_params(body)
    if params_error:
        print("the task order has a problem:\n%s" % params_error, file=sys.stderr)
        return 2
    task = new_task(body, workspace_id, allow_paths=allow_paths,
                    priority=int(body.get("priority") or 0),
                    not_before=not_before, not_before_text=not_before_text,
                    key=str(body.get("idempotency_key") or ""),
                    meta=body.get("meta") if isinstance(body.get("meta"), dict) else {},
                    model_params=model_params)
    app.store.create(task)
    log("queued task %s (%s)" % (task["id"], task["title"] or task["prompt"][:40]))
    if task["not_before"]:
        log("  it may start at %s (%s)" % (task["not_before"], task["not_before_text"]))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="The task queue: enqueue, run and watch tasks through the agent's API.")
    parser.add_argument("--agent", default=DEFAULT_AGENT,
                        help="the agent's HTTP API (default: %s)" % DEFAULT_AGENT)
    parser.add_argument("--host", default="127.0.0.1", help="where to serve the task API")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help="the port of the task API (default: %d)" % DEFAULT_PORT)
    parser.add_argument("--data-dir", default="",
                        help="where the task records live (default: <workspace-path>/tasks)")
    parser.add_argument("--workspace", default=os.environ.get("TEA_AGENT_WORKSPACE", ""),
                        help="the workspace whose queue is worked on (default: the queue's own)")
    parser.add_argument("--workspace-path", default=DEFAULT_WORKSPACE_PATH,
                        help="the queue's own workspace directory (created and registered with the agent)")
    parser.add_argument("--workspace-name", default=os.environ.get("TEA_TASK_WORKSPACE_NAME", "任务"),
                        help="the name the queue's workspace gets in the agent")
    parser.add_argument("--provider", default="",
                        help="the provider whose quota gates the work and which runs "
                             "the queued tasks (default: provider.yaml's task_model:, "
                             "else %s)" % DEFAULT_PROVIDER)
    parser.add_argument("--window", default="window", choices=["window", "weekly"],
                        help="which quota window to watch (default: the short one)")
    parser.add_argument("--rate-limit-per-minute", type=int, default=0,
                        help="每个钥匙每分钟最多几个请求（0 = 不限）")
    parser.add_argument("--interaction-timeout", type=float, default=0.0,
                        help="等人回答最多等多久（秒，0 = 一直等）")
    parser.add_argument("--retention-days", type=float, default=0.0,
                        help="任务记录留多久（天，0 = 一直留）")
    parser.add_argument("--cleanup-interval", type=float, default=3600.0,
                        help="多久清一次旧记录（秒）")
    parser.add_argument("--resume-on-restart", action="store_true",
                        help="重启后把断掉的任务**接着做**（同一会话里新的一轮）；"
                             "默认是判失败并写明原因")
    parser.add_argument("--export-ttl-hours", type=float, default=24.0,
                        help="导出的包留多久（小时）")
    parser.add_argument("--quota-tokens", type=float, default=0.0,
                        help="100%% 的额度值多少 token（0 = 不换算，额度只当开工前的闸门）")
    parser.add_argument("--quota-share", type=float, default=0.5,
                        help="一个任务最多能用剩下的多少（默认一半）")
    parser.add_argument("--min-remaining", type=float, default=5.0,
                        help="only work while at least this much of the window is left (%%)")
    parser.add_argument("--allow-without-quota", action="store_true",
                        help="work even when the provider does not report a quota")
    parser.add_argument("--max-concurrent", type=int, default=0,
                        help="how many tasks may run at the same time in total "
                             "(0 = only the per-project rule: one per project)")
    parser.add_argument("--quota-interval", type=float, default=60.0,
                        help="how long the provider's quota answer is reused (the queue polls "
                             "every --interval seconds; the quota endpoint is a real call)")
    parser.add_argument("--stale-after", type=float, default=300.0,
                        help="a task that was not touched for this many seconds does not hold "
                             "a slot any more")
    parser.add_argument("--interval", type=float, default=2.0,
                        help="seconds between rounds (default: 2)")
    parser.add_argument("--fleet-url", default=os.environ.get("TEA_FLEET_URL", ""),
                        help="the fleet service's HTTP API (a task with target=fleet runs "
                             "on the devices' own agents)")
    parser.add_argument("--fleet-token", default=os.environ.get("TEA_FLEET_TOKEN", ""),
                        help="the token for the fleet service")
    parser.add_argument("--trigger-config", default=triggers.DEFAULT_RULES_PATH,
                        help="the rules of the trigger entry: which outside events are "
                             "work and which are not (see docs/router-watchdog.md)")
    parser.add_argument("--aggregate-timeout", type=float, default=180.0,
                        help="seconds for the model call that summarizes a plan")
    parser.add_argument("--aggregate-max-tokens", type=int, default=4000,
                        help="how many tokens the summary may take (the model's "
                             "thinking comes out of the same budget)")
    parser.add_argument("--trigger-timeout", type=float, default=60.0,
                        help="seconds for the model call that judges a trigger")
    parser.add_argument("--chat-timeout", type=float, default=3600.0,
                        help="how long a turn may take before the queue gives up on it "
                             "(seconds; 0 = no limit)")
    parser.add_argument("--sse-idle-timeout", type=float, default=3600.0,
                        help="how long an event stream may stay quiet before it is closed "
                             "(seconds)")
    parser.add_argument("--max-per-round", type=int, default=4,
                        help="how many queued tasks one round may start (the per-project rule still "
                             "holds: two tasks of the same project never run together)")
    parser.add_argument("--submit-order", metavar="FILE",
                        help="queue the task described by this task order file")
    parser.add_argument("--submit", metavar="PROMPT",
                        help="put a task in the queue and exit (no server needed)")
    parser.add_argument("--title", default="", help="title for --submit")
    parser.add_argument("--allow-path", action="append", default=[],
                        help="a directory the task may touch (repeatable; the file "
                             "tools are otherwise fenced to the workspace)")
    parser.add_argument("--allow-tool", action="append", default=[],
                        help="a tool the task may use (repeatable; empty = the usual set)")
    parser.add_argument("--limit-iterations", type=int, default=0,
                        help="how many model iterations this turn may run "
                             "(0 = the machine's own ceiling)")
    parser.add_argument("--limit-tool-calls", type=int, default=0,
                        help="how many tool calls this turn may make (0 = no limit)")
    parser.add_argument("--limit-wall-ms", type=int, default=0,
                        help="the wall clock of this turn, in ms (0 = no limit)")
    parser.add_argument("--no-serve", action="store_true",
                        help="do not serve; only run the scheduler (for a cron-style setup)")
    args = parser.parse_args(argv)

    app = App(args)

    if args.submit_order:
        return submit_order(app, args)

    if args.submit:
        agent = Agent(args.agent)
        try:
            workspace_id = pick_workspace(agent, args.workspace)
        except (AgentError, SystemExit) as exc:
            print("cannot find a workspace: %s" % exc, file=sys.stderr)
            return 2
        task = {
            "id": "t_" + os.urandom(6).hex(),
            "workspace_id": workspace_id,
            "prompt": args.submit,
            "title": args.title,
            "provider": app.engine.effective_provider(),
            "model": "",
            "allow_paths": [os.path.abspath(os.path.expanduser(p)) for p in args.allow_path],
            # The budget of this task and the tools it may use. The agent
            # enforces both; the queue only asks.
            "limits": dict((k, v) for k, v in (
                ("max_iterations", args.limit_iterations),
                ("max_tool_calls", args.limit_tool_calls),
                ("max_wall_ms", args.limit_wall_ms)) if v),
            "policy": ({"tools": list(args.allow_tool)} if args.allow_tool else {}),
            "session_id": "",
            "status": QUEUED,
            "progress": 0,
            "note": "",
            "steps": [],
            "needs_confirmation": False,
            "question": "",
            "error": "",
            "result": "",
            "meta": {},
            "stop_reason": "",
            "budget_used": {},
            "usage": {},
            "iterations": 0,
            "trajectory_id": "",
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "started_at": "",
            "finished_at": "",
        }
        app.store.create(task)
        log("queued task %s (%s)" % (task["id"], task["title"] or ""))
        return 0

    # The queue's own workspace: created (and registered with the agent) once.
    app.task_workspace_id()

    # The records of a previous run: a task that was running then is not
    # running now (its worker died with that process). Everything that is
    # running is taken over, however fresh its record is: this process has
    # just started, so it cannot be the one that was working on it - and a
    # task whose turn is still alive on the agent's side is **not** lost
    # work, it is work that is still going on.
    app.engine.recover_stale_tasks(min_age=0)

    try:
        health = app.agent.health()
        log("agent up: %s (provider %s, model %s)"
            % (args.agent, health.get("provider"), health.get("model")))
    except AgentError as exc:
        # As a service, the agent may simply not be up yet (or be restarting).
        # Serving anyway and letting the scheduler retry every round is more
        # useful than exiting and letting systemd bounce us.
        log("the agent at %s is not reachable yet (%s) - serving anyway, retrying every round"
            % (args.agent, exc))
    log("task server: http://%s:%d  (records in %s)" % (args.host, args.port, app.store.root))

    if not args.no_serve:
        server = ThreadingHTTPServer((args.host, args.port), Handler)
        server.app = app
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()

    try:
        app.engine.run_forever()
    except KeyboardInterrupt:
        log("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
