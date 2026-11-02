#!/usr/bin/env python3
"""The router: the one that remembers the conversation and speaks for the work.

The queue runs the work; every task is its own session and knows nothing
about the task before it. The router is what ties those sessions together:

  * **It remembers.** Every step of a task (a progress note, a result, a
    question) goes into the thread the task belongs to. The next task of
    that thread is told what happened before it, so the model does not start
    from nothing. What falls out of the window is not thrown away: the
    router compresses it into a summary (that is the one model call it
    makes, and only when there is enough to compress).

  * **It speaks.** One event, one sentence, written down. Right now the
    chat front ends still talk to the queue themselves - the router only
    *records* what it would say ("shadow mode"), so the wording can be
    compared with what the users actually get before anybody is switched
    over.

The router is a separate process on purpose: it must not be able to take
the queue down with it, and the queue must not wait for it.

Usage:
    python3 sched/router.py                       # follow the queue's events
    python3 sched/router.py --interval 30 --spill-after 4
"""

import argparse
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

DEFAULT_TASKS = os.environ.get("TEA_TASK_URL", "http://127.0.0.1:8283")
DEFAULT_AGENT = os.environ.get("TEA_AGENT_URL", "http://127.0.0.1:8282")
DEFAULT_DATA_DIR = os.environ.get(
    "TEA_ROUTER_DATA", os.path.join("~", ".tea_agent", "router"))

SUMMARY_SYSTEM = ("你在替一个 agent 系统整理工作记录。下面是同一件活儿（一条线索）里"
                  "先后发生的若干条进展。请用两三句话把它们收拢成一段摘要：做了什么、"
                  "现在到哪一步、还差什么。只输出摘要本身，不要解释，不要 markdown 代码块。")


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def log(message):
    print("%s  %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), message), flush=True)


def http_json(url, method="GET", body=None, timeout=30, headers=None):
    """One JSON request. Returns (status, parsed_body). Never raises."""
    data = None
    head = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
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
    except urllib.error.URLError as exc:
        return 0, {"error": "cannot reach %s: %s" % (url, exc.reason)}
    except Exception as exc:  # noqa: BLE001 - socket timeouts and the like
        return 0, {"error": "cannot reach %s: %s" % (url, exc)}
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, {"error": raw[:500]}


def unwrap(body):
    """The queue answers in one shape: {code, message, request_id, data}."""
    if isinstance(body, dict) and "data" in body and "code" in body:
        if body.get("code"):
            return {"error": body.get("message") or "the queue refused"}
        return body.get("data") or {}
    return body


class Router(object):
    """Follows the queue's events and keeps the threads together."""

    def __init__(self, args):
        self.args = args
        self.tasks_url = args.tasks.rstrip("/")
        self.agent_url = args.agent.rstrip("/")
        self.data_dir = os.path.abspath(os.path.expanduser(args.data_dir))
        self.speech_path = args.speech or os.path.join(self.data_dir, "speech.jsonl")
        self.summarized = {}     # thread id -> how many entries are done

    # ---- what the router says --------------------------------------------
    @staticmethod
    def thread_label(event):
        """The conversation this event is about, in a few words.

        A task id says nothing to a human; "（字体设置）" does. The title
        comes with the event (the queue adds it), so the router does not
        have to ask.
        """
        thread = event.get("thread") or {}
        title = str(thread.get("title") or "").strip()
        return ("（%s）" % title) if title else ""

    def speak(self, event):
        """One event, one sentence. What the router would tell the people."""
        kind = event.get("type") or ""
        task = event.get("task") or {}
        trigger = event.get("trigger") or {}
        task_id = task.get("id") or ""
        label = self.thread_label(event)
        if kind == "task_created":
            title = task.get("title") or task.get("prompt") or ""
            if task.get("kind") == "plan":
                # A plan is not one job but a few: say so, and say how many.
                steps = len(task.get("children") or []) or int(task.get("child_count") or 0)
                return "排了一个计划%s%s（%d 步）：%s" % (
                    ("（%s）" % task_id) if task_id else "", label, steps, str(title)[:60])
            return "排了一个活儿%s%s：%s" % (("（%s）" % task_id) if task_id else "",
                                            label, str(title)[:60])
        if kind == "task_done":
            answer = str(task.get("result") or "").strip()
            return "做完了 %s%s%s" % (task_id, label,
                                     ("：%s" % answer[:60]) if answer else "")
        if kind == "task_failed":
            return "失败了 %s%s：%s" % (task_id, label, task.get("error") or "原因不明")
        if kind == "task_waiting_confirmation":
            return "%s%s 在等回答：%s" % (task_id, label, task.get("question") or "")
        if kind == "task_cancelled":
            return "%s%s 停了" % (task_id, label)
        if kind == "watchdog_alert":
            return "看门狗：%s：%s" % (event.get("check") or "", event.get("reason") or "")
        if kind == "trigger_decided":
            return "来了一个事件（%s）：%s%s" % (
                trigger.get("source") or "?",
                trigger.get("decision") or "?",
                ("：%s" % trigger.get("reason")) if trigger.get("reason") else "")
        if kind == "queue_paused":
            return "队列停了：%s" % (event.get("reason") or "没有写原因")
        if kind == "queue_resumed":
            return "队列继续了"
        if kind == "thread_summarized":
            return "一条线索收拢成了一句摘要"
        return ""

    def say(self, line, event=None):
        """Write down what the router said, and hand it to the queue.

        The chat front ends deliver this sentence instead of translating the
        raw events themselves: that is the "one voice" part - the wording
        lives in one place. `speech_of` says which event the sentence is
        about, so the front end can still attach the files of a finished
        task.
        """
        if not line:
            return
        log(line)
        entry = {"at": now_iso(), "line": line}
        try:
            os.makedirs(self.data_dir, exist_ok=True)
            with open(self.speech_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass
        if not self.args.publish:
            return
        body = {"type": "speech", "line": line}
        if event:
            body["speech_of"] = event.get("type") or ""
            task = event.get("task") or {}
            if task.get("id"):
                body["task_id"] = task.get("id")
                body["workspace_id"] = task.get("workspace_id") or ""
        status, answer = http_json(self.tasks_url + "/api/events", "POST", body,
                                   timeout=self.args.timeout)
        if status == 0 or status >= 400:
            log("cannot hand the line to the queue: %s" % (answer or {}).get("error"))

    # ---- the events --------------------------------------------------------
    def follow_events(self):
        """Follow the queue's event stream until it ends.

        The stream is a long-poll: it ends when the queue closes it, and
        then the router simply connects again.
        """
        url = self.tasks_url + "/api/events"
        req = urllib.request.Request(url, headers={"Accept": "text/event-stream"},
                                     method="GET")
        with urllib.request.urlopen(req, timeout=self.args.timeout) as resp:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if not payload or payload == "[DONE]":
                    continue
                try:
                    event = json.loads(payload)
                except ValueError:
                    continue
                self.handle(event)

    def handle(self, event):
        self.say(self.speak(event), event)

    # ---- the threads -------------------------------------------------------
    def threads(self):
        status, body = http_json(self.tasks_url + "/api/threads", timeout=self.args.timeout)
        if status == 0 or status >= 400:
            return []
        data = unwrap(body)
        if not isinstance(data, dict):
            return []
        return data.get("threads") or []

    def summarize(self, thread):
        """Compress what fell out of the window into one sentence.

        This is the only model call the router makes, and it only happens
        when there is enough to compress: the summary replaces the raw
        entries, so the prompt of the next task stays small.
        """
        thread_id = thread.get("id") or ""
        spill = thread.get("spill") or []
        raw = [e for e in spill if not e.get("summary")]
        if len(raw) < self.args.spill_after:
            return False
        lines = []
        for entry in raw:
            text = str(entry.get("text") or "").strip()
            if text:
                lines.append("- %s" % text)
        if not lines:
            return False
        count = len(raw)
        prompt = "这条线索的进展：\n%s" % "\n".join(lines)
        summary = self.ask_model(prompt)
        if not summary:
            return False
        status, body = http_json(self.tasks_url + "/api/threads/%s/spill" % thread_id,
                                 "POST", {"summary": summary, "count": count},
                                 timeout=self.args.timeout)
        if status == 0 or status >= 400:
            log("cannot write the summary of %s: %s" % (thread_id, body))
            return False
        # The summary replaced the raw entries, so there is nothing to remember.
        self.summarized.pop(thread_id, None)
        log("线索 %s 收拢成了一句摘要（%d 条）" % (thread_id, count))
        return True

    def ask_model(self, prompt):
        status, body = http_json(self.agent_url + "/api/llm/chat", "POST", {
            "prompt": prompt,
            "system": SUMMARY_SYSTEM,
            "temperature": 0.1,
            "max_tokens": 400,
            "stream": False,
        }, timeout=self.args.model_timeout)
        if status == 0 or status >= 400:
            log("the model did not answer: %s" % (body or {}).get("error"))
            return ""
        if not isinstance(body, dict):
            return ""
        return (body.get("content") or body.get("reasoning") or "").strip()

    def check_threads(self):
        """Compress the threads that need it."""
        for thread in self.threads():
            try:
                self.summarize(thread)
            except Exception as exc:  # noqa: BLE001 - one bad thread must not stop the rest
                log("cannot compress a thread: %s: %s" % (type(exc).__name__, exc))

    # ---- the loop ----------------------------------------------------------
    def follow_forever(self):
        """Follow the events until the process ends (reconnecting every time)."""
        while True:
            try:
                self.follow_events()
            except Exception as exc:  # noqa: BLE001 - the stream ends all the time
                log("event stream: %s" % exc)
            time.sleep(max(2.0, self.args.retry))

    def run_forever(self):
        log("router: watching %s (agent %s)" % (self.tasks_url, self.agent_url))
        # The events are followed in the background: the stream is a long poll
        # that can stay open for hours, and the threads have to be checked
        # every now and then all the same.
        watcher = threading.Thread(target=self.follow_forever, daemon=True)
        watcher.start()
        while True:
            try:
                self.check_threads()
            except Exception as exc:  # noqa: BLE001
                log("thread check failed: %s: %s" % (type(exc).__name__, exc))
            time.sleep(max(5.0, self.args.interval))


def build_parser():
    parser = argparse.ArgumentParser(
        description="Keep the threads together and speak for the work.")
    parser.add_argument("--tasks", default=DEFAULT_TASKS, help="the task server's HTTP API")
    parser.add_argument("--agent", default=DEFAULT_AGENT, help="the agent's HTTP API")
    parser.add_argument("--spill-after", type=int, default=4,
                        help="how many entries have to fall out of the window before the "
                             "router compresses them into a summary")
    parser.add_argument("--timeout", type=float, default=60.0,
                        help="seconds for one HTTP request")
    parser.add_argument("--model-timeout", type=float, default=120.0,
                        help="seconds for the model call that writes a summary")
    parser.add_argument("--retry", type=float, default=5.0,
                        help="seconds between two attempts to follow the event stream")
    parser.add_argument("--interval", type=float, default=30.0,
                        help="seconds between two checks of the threads")
    parser.add_argument("--publish", dest="publish", action="store_true", default=True,
                        help="hand every sentence to the queue's event stream, so the chat "
                             "front ends deliver the router's words (default: on)")
    parser.add_argument("--no-publish", dest="publish", action="store_false",
                        help="only write the sentences down (shadow mode)")
    parser.add_argument("--data-dir", default=DEFAULT_DATA_DIR,
                        help="where the speech log lives")
    parser.add_argument("--speech", default="", help="the speech log (default: under --data-dir)")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    return Router(args).run_forever()


if __name__ == "__main__":
    sys.exit(main())
