#!/usr/bin/env python3
"""Memory weights: a note fades with its age, a confirmation keeps it alive,
and the caps keep the file from growing without bound.

What this pins (the rules live in docs/workspace.md, "记忆的权重"):

  * the effective weight is the stored weight decayed by the note's absolute
    age (14-day half-life), and a note a person wrote never decays;
  * the prompt is built from the tiers in a fixed order - manual, strong,
    medium (matched against the conversation), weak (a deterministic sample of
    2) - and the budgets (24 / 14 / 2) are real: 26 strong notes mean 24 in,
    2 out, and the log says so;
  * the weak sample is stable for a whole UTC day, so two turns see the same
    two notes;
  * a note the agent adds through toolkit_memory starts at 0.9 (it fades), and
    a similar note is merged into it instead of duplicated: the weight goes up,
    the wording follows the newer one, the age is renewed (at most once a day,
    at most 6 times);
  * a note a person writes keeps the person's wording even when the agent
    restates it, and turns the note permanent;
  * short notes ("note-00" / "note-01") and notes that differ in a number
    ("port 8080" / "port 9090") are different notes;
  * the caps: 21 hand-written notes leave 20 and evict the least recently
    confirmed one (with an audit line), 101 agent notes leave 100 dropping the
    weakest;
  * an old memory.json (no source / weight_base) is read as a person's notes and
    is not rewritten by a GET; the next write fills the fields in.

Usage: python tests/smoke_memory_weights.py [path-to-tea_agent-binary]
"""

import http.client
import json
import os
import random
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler

try:
    from http.server import ThreadingHTTPServer
except ImportError:  # Python 3.6
    from http.server import HTTPServer
    from socketserver import ThreadingMixIn

    class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
        daemon_threads = True

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# What the mock provider saw: the system prompt of every request.
seen = []
lock = threading.Lock()
# "请记住：X" in a user message makes the mock call toolkit_memory op=add X.
MEMORY_INTENT = "请记住："


class MockProvider(BaseHTTPRequestHandler):
    """Answers every request; asks for the memory tool when the user says so."""

    def do_POST(self):  # noqa: N802 (http.server API)
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        try:
            payload = json.loads(body.decode("utf-8", "replace"))
        except ValueError:
            payload = {}
        messages = payload.get("messages") or []
        system = ""
        for message in messages:
            if message.get("role") == "system":
                system = message.get("content") or ""
                break
        with lock:
            seen.append(system)

        last = messages[-1] if messages else {}
        text = last.get("content") if last.get("role") == "user" else None
        if isinstance(text, str) and MEMORY_INTENT in text:
            note = text.split(MEMORY_INTENT, 1)[1].strip()
            self._tool_call("call_mem_1", "toolkit_memory",
                            {"op": "add", "text": note})
        else:
            self._content("ok")

    def _start(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()

    def _chunk(self, payload):
        try:
            self.wfile.write(("data: " + json.dumps(payload) + "\n\n").encode())
            self.wfile.flush()
            return True
        except OSError:
            return False

    def _done(self):
        try:
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except OSError:
            pass

    def _content(self, text):
        self._start()
        self._chunk({"id": "c1", "object": "chat.completion.chunk",
                     "choices": [{"index": 0, "delta": {"content": text},
                                  "finish_reason": None}]})
        self._chunk({"id": "c1", "object": "chat.completion.chunk",
                     "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
        self._done()

    def _tool_call(self, call_id, name, args):
        self._start()
        self._chunk({"id": "c1", "object": "chat.completion.chunk",
                     "choices": [{"index": 0, "delta": {
                         "role": "assistant",
                         "tool_calls": [{"index": 0, "id": call_id, "type": "function",
                                         "function": {"name": name,
                                                      "arguments": json.dumps(args)}}],
                     }, "finish_reason": None}]})
        self._chunk({"id": "c1", "object": "chat.completion.chunk",
                     "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]})
        self._done()

    def log_message(self, *args):  # keep the test output clean
        pass


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
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            conn.request("GET", "/api/health")
            if conn.getresponse().status == 200:
                return True
        except OSError:
            pass
        time.sleep(0.2)
    return False


def call(port, method, path, body=None, timeout=60):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    headers = {"Content-Type": "application/json"} if body is not None else {}
    conn.request(method, path, body=json.dumps(body) if body is not None else None,
                 headers=headers)
    resp = conn.getresponse()
    raw = resp.read()
    try:
        return resp.status, json.loads(raw or b"null")
    except ValueError:
        return resp.status, {"raw": raw.decode("utf-8", "replace")}


def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def iso_days_ago(days):
    when = datetime.now(timezone.utc) - timedelta(days=days)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def note(note_id, text, days_ago, source="agent", weight=None):
    stamp = iso_days_ago(days_ago)
    out = {"id": note_id, "text": text, "created_at": stamp}
    if source is not None:
        out["source"] = source
        out["decay_anchor"] = stamp
    if weight is not None:
        out["weight_base"] = weight
    return out


def main():
    binary = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        REPO_ROOT, "build-mingw", "bin", "tea_agent.exe")
    if not os.path.isfile(binary):
        print("binary not found: " + binary)
        return 2

    tmp = tempfile.mkdtemp(prefix="tea-memory-")
    mock_port = free_port()
    mock = ThreadingHTTPServer(("127.0.0.1", mock_port), MockProvider)
    threading.Thread(target=mock.serve_forever, daemon=True).start()

    ws_dir = os.path.join(tmp, "ws")
    run_dir = os.path.join(ws_dir, ".tea_agent_run")
    os.makedirs(os.path.join(run_dir, "memory"))
    memory_path = os.path.join(run_dir, "memory", "memory.json")
    audit_path = os.path.join(run_dir, "memory", "evicted.jsonl")
    log_path = os.path.join(tmp, "tea_agent.log")

    # 2 manual notes, 26 strong, 20 medium (2 of them about the conversation),
    # 12 weak. The strong notes all sit at w_eff = 0.9 (fresh, base 0.9), so the
    # tie-break is the id and STRONG-24/STRONG-25 are the two that do not fit.
    seeded = []
    seeded.append(note("n_manual_01", "MANUAL 回答要简洁清晰", 400, source="user", weight=1.0))
    seeded.append(note("n_manual_02", "MANUAL 发版在另一台机器", 30, source="user", weight=1.0))
    for i in range(26):
        seeded.append(note("n_strong_%02d" % i, "STRONG-%02d 构建与测试约定" % i, 0,
                           weight=0.9))
    for i in range(2):
        seeded.append(note("n_medium_ex_%02d" % i, "ZXQ 导出 HTML 标题 的写法 %02d" % i,
                           9, weight=0.9))
    for i in range(18):
        seeded.append(note("n_medium_ot_%02d" % i, "MEDIUM-OTHER-%02d 终端会话记录" % i,
                           9, weight=0.9))
    for i in range(12):
        seeded.append(note("n_weak_%02d" % i, "WEAK-%02d 早期机器状态" % i, 30, weight=0.9))
    write_json(memory_path, {"notes": seeded, "updated_at": iso_days_ago(0)})

    config_path = os.path.join(tmp, "provider.yaml")
    with open(config_path, "w", encoding="utf-8") as f:
        f.write("""version: 1
providers:
  mock:
    api_url: http://127.0.0.1:%d
    api_key: sk-test
    default_model: m
    models:
      m:
        max_context_tokens: 32768
        max_output_tokens: 4096
        supports_tools: true
""" % mock_port)

    agent_port = free_port()
    proc = subprocess.Popen([
        binary, "--config", config_path, "--tea-home", os.path.join(tmp, "home"),
        "--host", "127.0.0.1", "--port", str(agent_port), "--no-console",
        "--log-file", log_path, "--workspace", ws_dir,
    ])

    failures = []
    checks = []

    def check(name, ok, detail=""):
        checks.append(name)
        print(("PASS " if ok else "FAIL ") + name +
              ((("  [" + str(detail) + "]")) if not ok else ""))
        if not ok:
            failures.append(name)

    def turn(message):
        return call(agent_port, "POST", "/api/chat",
                    {"workspace_id": wid, "message": message}, timeout=120)

    def disk_notes():
        return read_json(memory_path)["notes"]

    try:
        if not wait_health(agent_port):
            print("server did not become healthy")
            return 1
        wid = call(agent_port, "GET", "/api/workspaces")[1]["workspaces"][0]["id"]
        base = "/api/workspaces/%s/memory" % wid

        # ---- 1. the weights the API reports -------------------------------
        status, listing = call(agent_port, "GET", base)
        rows = {n["id"]: n for n in listing["notes"]}
        check("GET /memory reports every note", status == 200 and len(rows) == len(seeded),
              "%d of %d" % (len(rows), len(seeded)))
        check("a note a person wrote is permanent",
              rows["n_manual_01"]["tier"] == "manual" and
              rows["n_manual_01"]["weight_effective"] == 1.0,
              rows["n_manual_01"])
        check("a 9-day-old agent note is medium, not strong",
              rows["n_medium_ex_00"]["tier"] == "medium", rows["n_medium_ex_00"])
        check("a 30-day-old agent note is weak",
              rows["n_weak_00"]["tier"] == "weak", rows["n_weak_00"])
        aged = rows["n_weak_00"]["weight_effective"]  # 30 days old, base 0.9
        check("14-day half-life: 30 days at 0.9 is ~0.20",
              abs(aged - 0.9 * 0.5 ** (30.0 / 14.0)) < 0.01, aged)
        check("the caps are reported",
              listing["limits"]["manual_cap"] == 20 and
              listing["limits"]["agent_cap"] == 100 and
              listing["limits"]["manual_used"] == 2 and
              listing["limits"]["agent_used"] == len(seeded) - 2,
              listing["limits"])

        # ---- 2. the prompt: tiers, budgets, order -------------------------
        focus = "请按 ZXQ 导出 HTML 标题 处理这件事"
        turn(focus)
        check("the mock provider saw a request", bool(seen))
        system = seen[0] if seen else ""
        check("manual notes are always in", "MANUAL 回答要简洁清晰" in system and
              "MANUAL 发版在另一台机器" in system)
        check("strong tier is capped at 24 of 26", system.count("STRONG-") == 24,
              system.count("STRONG-"))
        check("the two that did not fit are the last two",
              "STRONG-24" not in system and "STRONG-25" not in system)
        check("the medium note the conversation is about is in",
              system.count("ZXQ 导出 HTML 标题") == 2, system.count("ZXQ 导出 HTML 标题"))
        check("an unrelated medium note is not in", "MEDIUM-OTHER" not in system)
        check("the weak tier contributes exactly 2 notes", system.count("WEAK-") == 2,
              system.count("WEAK-"))
        order = [system.find("MANUAL 回答"), system.find("STRONG-00"),
                 system.find("ZXQ 导出"), system.find("WEAK-")]
        check("tiers are emitted manual -> strong -> medium -> weak",
              all(x >= 0 for x in order) and order == sorted(order), order)
        with open(log_path, encoding="utf-8", errors="replace") as f:
            log_text = f.read()
        check("the log says the strong budget truncated 2 notes",
              "truncated 2" in log_text, log_text[-400:])

        # ---- 3. the weak sample is stable within the day ------------------
        first_weak = [ln for ln in system.splitlines() if ln.startswith("- WEAK-")]
        turn(focus)
        second_weak = [ln for ln in seen[-1].splitlines() if ln.startswith("- WEAK-")]
        check("the same day samples the same weak notes",
              first_weak and first_weak == second_weak, (first_weak, second_weak))

        # ---- 4. a hand-written note keeps the person's wording ------------
        before = len(disk_notes())
        first_text = "这个项目构建只用 MinGW-w64，不要用 MSVC 编译器"
        call(agent_port, "POST", base, {"text": first_text})
        status, second_add = call(agent_port, "POST", base, {"text": first_text + "。"})
        notes = disk_notes()
        merged = [n for n in notes if n["id"] == second_add["note"]["id"]]
        check("a similar note is merged, not duplicated",
              second_add["merged_into"] and len(notes) == before + 1,
              "%d -> %d" % (before, len(notes)))
        check("the person's wording is not rewritten by the restatement",
              merged and merged[0]["text"] == first_text, merged)
        check("the restatement is kept as a trace",
              merged and merged[0].get("merged_from"))
        check("a confirmation raises the weight",
              merged and merged[0].get("weight_base") == 1.0, merged)
        check("the note records the confirmation",
              merged and merged[0].get("merge_count") == 1, merged)
        anchor_before = merged[0].get("decay_anchor") if merged else None
        call(agent_port, "POST", base, {"text": first_text + "！"})
        again = [n for n in disk_notes() if n["id"] == second_add["note"]["id"]]
        check("a second confirmation the same day does not renew the age",
              again and again[0].get("decay_anchor") == anchor_before,
              (anchor_before, again[0].get("decay_anchor") if again else None))

        # ---- 5. the agent's own note: starts at 0.9, fades, can be renewed -
        agent_text = "AGENT 记下的构建约定只用 MinGW-w64 编译器"
        turn(MEMORY_INTENT + agent_text)
        agent_notes = [n for n in disk_notes()
                       if n.get("source") == "agent" and n["text"] == agent_text]
        check("toolkit_memory stored a note the agent wrote", len(agent_notes) == 1,
              agent_notes)

        turn(MEMORY_INTENT + agent_text + "。")
        merged_agent = [n for n in disk_notes() if n.get("source") == "agent" and
                        n["text"].startswith("AGENT 记下的构建约定")]
        check("the agent's restatement merges into its own note",
              len(merged_agent) == 1 and merged_agent[0]["text"] == agent_text + "。",
              merged_agent)
        check("the agent's note counts the confirmation",
              merged_agent and merged_agent[0].get("merge_count") == 1, merged_agent)
        check("a confirmation inside 24 hours does not renew the age",
              merged_agent and merged_agent[0].get("renew_count") in (0, None),
              merged_agent[0].get("renew_count") if merged_agent else None)

        # Backdate the note: the next restatement is a renewal, 30 days old.
        target = merged_agent[0] if merged_agent else None
        if target:
            notes = disk_notes()
            for n in notes:
                if n["id"] == target["id"]:
                    n["decay_anchor"] = iso_days_ago(30)
                    n["weight_base"] = 0.9
                    n["renew_count"] = 0
            write_json(memory_path, {"notes": notes, "updated_at": iso_days_ago(0)})
            turn(MEMORY_INTENT + agent_text + "！！")
            renewed = [n for n in disk_notes() if n["id"] == target["id"]]
            check("a later confirmation renews the age",
                  renewed and renewed[0].get("renew_count") == 1 and
                  renewed[0].get("decay_anchor") != iso_days_ago(30),
                  renewed)

            # A person restating the agent's note turns it into a permanent one.
            call(agent_port, "POST", base, {"text": agent_text + "！！。"})
            promoted = [n for n in disk_notes() if n["id"] == target["id"]]
            check("a person's restatement makes the note permanent",
                  promoted and promoted[0].get("source") == "user" and
                  promoted[0].get("weight_base") == 1.0, promoted)

        # ---- 6. what is not the same note --------------------------------
        before = len(disk_notes())
        call(agent_port, "POST", base, {"text": "note-00"})
        call(agent_port, "POST", base, {"text": "note-01"})
        short = len(disk_notes())
        check("short notes that differ are not merged", short == before + 2,
              "%d -> %d" % (before, short))
        call(agent_port, "POST", base, {"text": "本项目默认端口使用 8080 对外"})
        call(agent_port, "POST", base, {"text": "本项目默认端口使用 9090 对外"})
        numbered = len(disk_notes())
        check("notes that differ in a number are not merged", numbered == short + 2,
              "%d -> %d" % (short, numbered))

        # ---- 7. the manual cap -------------------------------------------
        write_json(memory_path, {"notes": [], "updated_at": iso_days_ago(0)})
        for i in range(20):
            call(agent_port, "POST", base, {"text": "手工便签%02d" % i})
            time.sleep(0.01)
        status, extra = call(agent_port, "POST", base, {"text": "手工便签第 21 条"})
        notes = disk_notes()
        texts = [n["text"] for n in notes]
        check("21 hand-written notes leave 20", len(notes) == 20, len(notes))
        check("the least recently confirmed one was evicted",
              "手工便签00" not in texts and "手工便签第 21 条" in texts, texts[:2])
        check("the response names what it evicted",
              extra.get("evicted") and extra["evicted"][0]["text"] == "手工便签00",
              extra.get("evicted"))
        audit = [json.loads(line) for line in
                 open(audit_path, encoding="utf-8").read().splitlines() if line.strip()]
        check("the eviction is in the audit file",
              audit and audit[-1]["reason"] == "manual_cap" and
              audit[-1]["text"] == "手工便签00", audit[-1:] if audit else [])
        check("the evicted note is gone from memory.json",
              all(n["id"] != extra["evicted"][0]["id"] for n in notes))

        # ---- 8. the agent cap --------------------------------------------
        bulk = [note("n_bulk%04d" % i, "批量自动记忆条目 %04d" % i, 40 + (i % 5), weight=0.9)
                for i in range(101)]
        write_json(memory_path, {"notes": bulk, "updated_at": iso_days_ago(0)})
        status, res = call(agent_port, "POST", base, {"text": "触发一次写入以执行上限"})
        notes = disk_notes()
        agents = [n for n in notes if n.get("source") == "agent"]
        check("101 agent notes leave 100", len(agents) == 100, len(agents))
        check("the weakest agent note was dropped, the newest kept",
              res.get("evicted") and res["evicted"][0]["id"].startswith("n_bulk") and
              any(n["text"] == "触发一次写入以执行上限" for n in notes),
              res.get("evicted"))
        fresh = [n for n in notes if n["text"] == "触发一次写入以执行上限"][0]
        check("a note written in the UI is a person's note",
              fresh.get("source") == "user" and fresh.get("weight_base") == 1.0, fresh)

        # ---- 9. an old file is readable and is not rewritten by a GET -----
        legacy = [
            {"id": "n_legacy0001", "text": "老格式的一条记忆", "created_at": iso_days_ago(3)},
            {"id": "n_legacy0002", "text": "老格式的另一条", "created_at": iso_days_ago(60)},
        ]
        write_json(memory_path, {"notes": legacy, "updated_at": iso_days_ago(0)})
        before_bytes = open(memory_path, "rb").read()
        status, listing = call(agent_port, "GET", base)
        rows = {n["id"]: n for n in listing["notes"]}
        check("an old note is read as a person's permanent note",
              rows["n_legacy0002"]["source"] == "user" and
              rows["n_legacy0002"]["tier"] == "manual" and
              rows["n_legacy0002"]["weight_effective"] == 1.0, rows["n_legacy0002"])
        check("a GET does not rewrite the file",
              open(memory_path, "rb").read() == before_bytes)
        call(agent_port, "POST", base, {"text": "写入一次以固化字段"})
        fixed = disk_notes()
        check("the next write fills the fields in",
              all(n.get("source") == "user" and n.get("weight_base") == 1.0
                  and n.get("decay_anchor") for n in fixed), fixed[:2])

        # ---- 10. delete still works --------------------------------------
        victim = fixed[0]["id"]
        status, _ = call(agent_port, "DELETE", base + "/" + victim)
        check("a note can be deleted", status == 200 and
              all(n["id"] != victim for n in disk_notes()))
        status, _ = call(agent_port, "DELETE", base + "/n_does_not_exist")
        check("deleting an unknown note is a 404", status == 404, status)

        # ---- 11. concurrent writes keep every note ------------------------
        write_json(memory_path, {"notes": [], "updated_at": iso_days_ago(0)})
        results = [None] * 12
        barrier = threading.Barrier(12)

        def add(index):
            try:
                barrier.wait(timeout=20)
            except threading.BrokenBarrierError:
                pass
            results[index] = call(agent_port, "POST", base,
                                  {"text": "并发便签%02d" % index})

        threads = [threading.Thread(target=add, args=(i,)) for i in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        notes = disk_notes()
        check("12 concurrent writes all survive",
              len(notes) == 12 and all(r and r[0] == 200 for r in results),
              len(notes))
        check("the file is still valid JSON after the writes",
              isinstance(notes, list) and all("id" in n for n in notes))

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except Exception:
            proc.kill()
        mock.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    print("%d checks, %d failed" % (len(checks), len(failures)))
    if failures:
        for name in failures:
            print("  FAILED: " + name)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
