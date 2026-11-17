#!/usr/bin/env python3
"""Smoke test for the export that merges an interrupted turn with the turn
that continued it.

A turn that was stopped (or that died) and the turn that picked the work up
again are ONE piece of work, so the exported file shows them as one session
("第 1-2 条会话") instead of two: the first one without an answer, the second
one asking to continue.

What this pins:

  * the turn that was interrupted and the turn that continued it are one block
    in the topic export (brief and full);
  * the question of the continued turn is not shown (its pictures are), the
    answer of the last turn is;
  * "download the current session" of a turn inside such a group shows the
    whole group;
  * a chain (interrupted, continued, interrupted again) is one group;
  * a turn that was not interrupted is never merged;
  * an interrupted turn with nothing after it says so;
  * the JSON block still carries every turn, so an imported file is the
    conversation it was;
  * without a trajectory (an old session) nothing is merged and the export
    still works;
  * the compressed trajectory (.jsonl.gz) is read as well;
  * a tool result that says it was interrupted is enough on its own.

Usage: python tests/smoke_export_merge.py [path-to-tea_agent-binary]
"""

import gzip
import http.client
import json
import os
import random
import socket
import subprocess
import sys
import tempfile
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def free_port():
    while True:
        port = random.randint(20000, 60000)
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue


def wait_health(port, timeout=30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        try:
            conn.request("GET", "/api/health")
            if conn.getresponse().status == 200:
                return True
        except OSError:
            pass
        time.sleep(0.2)
    return False


def call(port, method, path, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    payload = json.dumps(body) if body is not None else None
    headers = {"Content-Type": "application/json"} if payload else {}
    conn.request(method, path, body=payload, headers=headers)
    resp = conn.getresponse()
    data = resp.read()
    return resp.status, dict(resp.getheaders()), data


def turn(second, question, answer, extra=None):
    """One turn: a question with its timestamp and the answer that followed."""
    stamp = "2026-01-01T00:%02d:%02dZ" % (second // 60, second % 60)
    messages = [
        {"role": "user", "content": question, "created_at": stamp},
        {"role": "assistant", "content": answer, "created_at": stamp},
    ]
    if extra:
        messages.extend(extra)
    return messages


def record(second, cancelled=False, partial=False, finished=True):
    """One line of a trajectory file: when the turn ran and how it ended."""
    start = "2026-01-01T00:%02d:%02d.500Z" % (second // 60, second % 60)
    end = "2026-01-01T00:%02d:%02d.900Z" % (second // 60, second % 60)
    line = {
        "turn_id": "t_%d" % second,
        "session_id": "x",
        "started_at": start,
        "ok": not cancelled,
        "cancelled": cancelled,
        "partial": partial,
        "version": 1,
        "steps": [],
    }
    if finished:
        line["finished_at"] = end
    return line


def write_session(run_dir, sid, title, messages):
    sessions = os.path.join(run_dir, "sessions")
    os.makedirs(sessions, exist_ok=True)
    with open(os.path.join(sessions, sid + ".json"), "w", encoding="utf-8") as f:
        json.dump({"id": sid, "title": title, "created_at": "2026-01-01T00:00:00Z",
                   "updated_at": "2026-01-01T00:10:00Z", "messages": messages}, f,
                  ensure_ascii=False)


def write_trajectory(run_dir, sid, records, compressed=False):
    path = os.path.join(run_dir, "trajectories", sid + ".jsonl")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    body = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records)
    if compressed:
        with gzip.open(path + ".gz", "wb") as f:
            f.write(body.encode("utf-8"))
        return
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)


def split_body(text):
    """The rendered part of the export and the JSON block it carries."""
    marker = '<script type="application/json" id="tea-session-data">'
    at = text.find(marker)
    if at < 0:
        return text, ""
    body = text[:at]
    payload = text[at + len(marker):]
    end = payload.find("</script>")
    return body, payload[:end].replace("<\\/", "</") if end >= 0 else ""


def main():
    binary = (sys.argv[1] if len(sys.argv) > 1
              else os.path.join(REPO_ROOT, "build", "bin", "tea_agent"))
    if not os.path.exists(binary):
        print("binary not found:", binary)
        return 1

    tmp = tempfile.mkdtemp(prefix="tea_merge_")
    port = free_port()
    ws_a = os.path.join(tmp, "proj_a")
    ws_b = os.path.join(tmp, "proj_b")
    for d in (ws_a, ws_b, os.path.join(tmp, "home")):
        os.makedirs(d, exist_ok=True)
    config = os.path.join(tmp, "provider.yaml")
    with open(config, "w", encoding="utf-8") as f:
        f.write("""version: 1
providers:
  mock:
    api_url: http://127.0.0.1:9/v1
    api_key: sk-test
    default_model: m
    models:
      m:
        max_context_tokens: 32768
""")
    proc = subprocess.Popen([
        binary, "--config", config, "--tea-home", os.path.join(tmp, "home"),
        "--host", "127.0.0.1", "--port", str(port), "--no-console",
        "--log-file", os.path.join(tmp, "tea.log"),
        "--workspace", ws_a, "--workspace", ws_b,
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    failures = []
    checks = []

    def check(name, ok, detail=""):
        checks.append(name)
        print(("PASS " if ok else "FAIL ") + name + (("  " + str(detail)) if not ok else ""))
        if not ok:
            failures.append(name)

    run_a = os.path.join(ws_a, ".tea_agent_run")
    try:
        if not wait_health(port):
            print("server did not become healthy")
            return 1

        # ---- the seeds ----------------------------------------------------
        # a: turn 1 was interrupted, turn 2 continued it.
        a_messages = (turn(0, "重新编译 msvc 成本命令", "我先看看") +
                      turn(60, "继续", "编译完成") +
                      turn(120, "新问题", "新回答") +
                      turn(180, "最后的问题", "最后的回答"))
        write_session(run_a, "s_merge_a", "合并 测试", a_messages)
        write_trajectory(run_a, "s_merge_a", [record(0, cancelled=True), record(60),
                                              record(120), record(180)])

        # b: a chain - turn 1 and turn 2 were interrupted, turn 3 finished.
        b_messages = (turn(0, "第一个任务", "先做一半") +
                      turn(60, "接着做", "又做一半") +
                      turn(120, "再继续", "做完了"))
        write_session(run_a, "s_merge_b", "链式 测试", b_messages)
        write_trajectory(run_a, "s_merge_b", [record(0, cancelled=True),
                                              record(60, cancelled=True), record(120)])

        # c: nothing was interrupted.
        c_messages = turn(0, "普通问题", "普通回答") + turn(60, "另一个问题", "另一个回答")
        write_session(run_a, "s_merge_c", "不合并 测试", c_messages)
        write_trajectory(run_a, "s_merge_c", [record(0), record(60)])

        # d: the last turn was interrupted - nothing continued it.
        d_messages = turn(0, "第一个问题", "第一个回答") + turn(60, "没跑完的问题", "说到一半")
        write_session(run_a, "s_merge_d", "末尾中断 测试", d_messages)
        write_trajectory(run_a, "s_merge_d", [record(0), record(60, cancelled=True)])

        # e: no trajectory at all, but a tool result says it was interrupted.
        e_messages = (turn(0, "老会话的问题", "老会话的回答", extra=[
            {"role": "tool", "tool_call_id": "c1", "name": "toolkit_exec",
             "content": "cancelled by user", "cancelled": True, "ok": False,
             "created_at": "2026-01-01T00:00:30Z"}]) +
            turn(60, "继续", "老会话继续完了"))
        write_session(run_a, "s_merge_e", "退化判定 测试", e_messages)

        # f: the same as a, but the trajectory was compressed.
        write_session(run_a, "s_merge_f", "压缩轨迹 测试", a_messages)
        write_trajectory(run_a, "s_merge_f", [record(0, cancelled=True), record(60),
                                              record(120), record(180)], compressed=True)

        # ---- the topic export (brief) --------------------------------------
        status, headers, raw = call(port, "GET",
                                    "/api/sessions/s_merge_a/export?scope=topic&detail=brief")
        text = raw.decode("utf-8")
        body, payload = split_body(text)
        check("the topic export works", status == 200, status)
        check("the interrupted turn and the one that continued it are one session",
              body.count("第 1-2 条会话") == 1, body.count("第 1-2 条会话"))
        check("the continued turn is not a session of its own",
              "第 2 条会话" not in body, "found it")
        check("the other turns are still there",
              "第 3 条会话" in body and "第 4 条会话" in body)
        check("the question of the interrupted turn is shown",
              "重新编译 msvc 成本命令" in body)
        check("the question that continued it is not shown", "继续" not in body)
        check("the answer that finished the work is shown", "编译完成" in body)
        check("the half answer of the interrupted turn is not shown", "我先看看" not in body)
        check("the group says it holds an interrupt", "含中断" in body)
        check("the header counts the turns and the sessions",
              "整个主题（4 轮 · 3 条会话，1 处续做已合并）" in text,
              text[:0])
        check("the header explains the merge",
              "其中 1 处是被中断后接着做完的，已并成一条会话。" in text)
        check("the JSON still carries every turn",
              len([m for m in json.loads(payload).get("messages", [])
                   if m.get("role") == "user"]) == 4,
              json.loads(payload).get("messages"))
        check("the message count matches the JSON",
              headers.get("X-Session-Messages") ==
              str(len(json.loads(payload).get("messages", []))),
              headers.get("X-Session-Messages"))

        # ---- the topic export (full) ---------------------------------------
        status, headers, raw = call(port, "GET",
                                    "/api/sessions/s_merge_a/export?scope=topic&detail=full")
        text = raw.decode("utf-8")
        body, payload = split_body(text)
        check("the full topic export works", status == 200, status)
        check("it is one session as well", body.count("第 1-2 条会话") == 1)
        check("the continued turn is not a session of its own", "第 2 条会话" not in body)
        check("the half answer is there (everything is)", "我先看看" in body)
        check("the question that continued it is still not shown", "继续" not in body)
        check("the answer is there", "编译完成" in body)

        # ---- the current session -------------------------------------------
        status, headers, raw = call(
            port, "GET", "/api/sessions/s_merge_a/export?scope=turn&detail=brief&turn=2")
        text = raw.decode("utf-8")
        body, payload = split_body(text)
        check("the current session export works", status == 200, status)
        check("it shows the whole group",
              "重新编译 msvc 成本命令" in body and "编译完成" in body)
        check("it says which session it is", "当前会话（第 1-2 条，共 4 轮）" in text)
        check("it leaves the other turns out",
              "新问题" not in body and "最后的问题" not in body)

        status, headers, raw = call(
            port, "GET", "/api/sessions/s_merge_a/export?scope=turn&detail=brief&turn=3")
        text = raw.decode("utf-8")
        body, payload = split_body(text)
        check("a turn outside the group is alone",
              "新问题" in body and "新回答" in body and "编译完成" not in body)
        check("it says which session it is", "当前会话（第 3 条，共 4 轮）" in text)

        # ---- a chain ---------------------------------------------------------
        status, headers, raw = call(port, "GET",
                                    "/api/sessions/s_merge_b/export?scope=topic&detail=brief")
        text = raw.decode("utf-8")
        body, payload = split_body(text)
        check("a chain is one session", body.count("第 1-3 条会话") == 1)
        check("the middle turn is not a session of its own", "第 2 条会话" not in body)
        check("the header counts it as one",
              "整个主题（3 轮 · 1 条会话，1 处续做已合并）" in text)

        # ---- nothing was interrupted ------------------------------------------
        status, headers, raw = call(port, "GET",
                                    "/api/sessions/s_merge_c/export?scope=topic&detail=brief")
        text = raw.decode("utf-8")
        body, payload = split_body(text)
        check("a normal session is not merged",
              "第 1 条会话" in body and "第 2 条会话" in body)
        check("the header says nothing was merged",
              "整个主题（2 轮 · 2 条会话）" in text and "续做已合并" not in text)

        # ---- an interrupted turn with nothing after it --------------------------
        status, headers, raw = call(port, "GET",
                                    "/api/sessions/s_merge_d/export?scope=topic&detail=brief")
        text = raw.decode("utf-8")
        body, payload = split_body(text)
        check("the interrupted turn is a session of its own",
              "第 1 条会话" in body and "第 2 条会话" in body)
        check("it says the turn did not finish", "这一轮被中断，没有跑完。" in body)

        # ---- no trajectory at all -----------------------------------------------
        status, headers, raw = call(port, "GET",
                                    "/api/sessions/s_merge_e/export?scope=topic&detail=brief")
        text = raw.decode("utf-8")
        body, payload = split_body(text)
        check("without a trajectory a cancelled tool is enough",
              body.count("第 1-2 条会话") == 1, body.count("第 1-2 条会话"))

        status, headers, raw = call(port, "GET",
                                    "/api/sessions/s_merge_c/export?scope=topic&detail=brief")
        check("the export works without a trajectory at all", status == 200, status)

        # ---- the compressed trajectory --------------------------------------------
        status, headers, raw = call(port, "GET",
                                    "/api/sessions/s_merge_f/export?scope=topic&detail=brief")
        text = raw.decode("utf-8")
        body, payload = split_body(text)
        check("a compressed trajectory is read too",
              body.count("第 1-2 条会话") == 1, body.count("第 1-2 条会话"))

        # ---- the old export is untouched -------------------------------------------
        status, headers, raw = call(port, "GET", "/api/sessions/s_merge_a/export")
        text = raw.decode("utf-8")
        check("the legacy export still works", status == 200, status)
        check("the legacy export has no sessions", "第 1-2 条会话" not in text)
        check("the legacy export has the conversation", "重新编译 msvc 成本命令" in text)

        # ---- import back --------------------------------------------------------
        status, headers, raw = call(port, "GET",
                                    "/api/sessions/s_merge_a/export?scope=topic&detail=brief")
        text = raw.decode("utf-8")
        _, _, listing = call(port, "GET", "/api/workspaces")
        wid_b = [w for w in json.loads(listing)["workspaces"]
                 if w["path"].endswith("proj_b")][0]["id"]
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
        conn.request("POST", "/api/workspaces/%s/sessions/import" % wid_b,
                     body=text.encode("utf-8"),
                     headers={"Content-Type": "text/html; charset=utf-8"})
        resp = conn.getresponse()
        imported = json.loads(resp.read())
        check("the merged file imports into another workspace", resp.status == 200,
              imported)
        new_sid = (imported.get("session") or {}).get("id", "")
        check("the imported session keeps every turn",
              imported.get("messages") == 8, imported.get("messages"))
        _, _, detail = call(port, "GET", "/api/sessions/%s" % new_sid)
        back = json.loads(detail)
        questions = [m.get("content") for m in back.get("messages", [])
                     if m.get("role") == "user"]
        check("the questions are all there, the continued one included",
              len(questions) == 4 and "继续" in questions, questions)
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            proc.kill()

    print()
    print("%d/%d checks passed" % (len(checks) - len(failures), len(checks)))
    if failures:
        print("FAILED:", ", ".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
