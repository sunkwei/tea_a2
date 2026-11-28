#!/usr/bin/env python3
"""The four memory layers behind one search (docs/memory-design.md).

What this pins:

  * one search covers the four layers - what was said (the conversations),
    what was written down (the journal), what was understood (the knowledge
    pages) - and the match is literal: the words that were actually written;
  * a hit carries a ref, and the ref reads the original text back (not a
    summary);
  * a knowledge page opens by name and knows its relations: the pages it
    links to, the pages that link to it, the pages it points at but nobody
    wrote;
  * a search that could not read a source says so in 'coverage' - zero hits
    does not mean it never happened;
  * the cursor is bound to the query: a cursor from another query is refused
    instead of pointing somewhere else.

Usage: python tests/smoke_memory_layers.py [path-to-tea_agent-binary]
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

try:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
except ImportError:  # Python 3.6
    from http.server import HTTPServer
    from socketserver import ThreadingMixIn

    class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
        daemon_threads = True

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Markers the mock provider reacts to.
FIND = "FIND:"
FIND1 = "FIND1:"
PAGE = "PAGE:"
OLDEST = "OLDEST:"
READ = "READ:"
OPEN = "OPEN:"
MISS = "MISS:"

KEYWORD = "LAYERKEY"
JOURNAL_TEXT = "LAYERKEY the journal said this once"
PAGE_TEXT = "LAYERKEY the knowledge page says this"
SAID_TEXT = "LAYERKEY this is what was said in the conversation"


def last_cursor(tool_output):
    """The nextCursor of the last search, if there was one."""
    if not tool_output:
        return ""
    start = tool_output.find("{")
    end = tool_output.rfind("}")
    if start < 0 or end <= start:
        return ""
    try:
        payload = json.loads(tool_output[start:end + 1])
    except ValueError:
        return ""
    return payload.get("nextCursor") or ""


# What the mock provider saw: the system messages of every request.
seen_system = []


class MockProvider(BaseHTTPRequestHandler):
    """Calls the memory tools when the user message carries a marker."""

    def do_POST(self):  # noqa: N802 (http.server API)
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        try:
            payload = json.loads(body.decode("utf-8", "replace"))
        except ValueError:
            payload = {}
        messages = payload.get("messages") or []

        user_text = ""
        tool_output = ""
        system_text = ""
        for message in messages:
            role = message.get("role")
            content = message.get("content")
            if not isinstance(content, str):
                continue
            if role == "user":
                user_text = content
            elif role == "tool" and content.strip():
                tool_output = content
            elif role == "system":
                system_text += content + "\n"

        seen_system.append(system_text)
        # How many tool results this turn has produced. The first request of a
        # turn has none; after that the marker is already handled - except for
        # the "second page" search, which is the point of that marker.
        tool_count = sum(1 for m in messages if m.get("role") == "tool")
        if tool_count == 0:
            if FIND1 in user_text:
                query = user_text.split(FIND1, 1)[1].strip()
                self._tool_call("call_find1", "toolkit_memory_find",
                                {"query": query, "limit": 1})
                return
            if PAGE in user_text:
                query = user_text.split(PAGE, 1)[1].strip()
                self._tool_call("call_page", "toolkit_memory_find",
                                {"query": query, "limit": 1})
                return
        if OLDEST in user_text:
            query = user_text.split(OLDEST, 1)[1].strip()
            self._tool_call("call_oldest", "toolkit_memory_find",
                            {"query": query, "order": "oldest"})
            return
        if FIND in user_text:
            query = user_text.split(FIND, 1)[1].strip()
            self._tool_call("call_find", "toolkit_memory_find", {"query": query})
            return
        if READ in user_text:
            ref = user_text.split(READ, 1)[1].strip()
            self._tool_call("call_read", "toolkit_memory_read", {"ref": ref})
            return
        if MISS in user_text:
            self._tool_call("call_miss", "toolkit_memory_find",
                            {"query": "ZZQQNOTFOUNDZZQ"})
            return
        if tool_count == 1 and PAGE in user_text:
            # The second page of the same search: the cursor of the first.
            cursor = last_cursor(tool_output)
            if cursor:
                query = user_text.split(PAGE, 1)[1].strip()
                self._tool_call("call_page2", "toolkit_memory_find",
                                {"query": query, "limit": 1, "cursor": cursor})
                return
        if tool_count == 2 and PAGE in user_text:
            # The cursor of that search, used for another query: it must be
            # refused instead of pointing somewhere else.
            cursor = last_cursor(tool_output)
            if cursor:
                self._tool_call("call_page3", "toolkit_memory_find",
                                {"query": "SOMETHINGELSE", "limit": 1, "cursor": cursor})
                return
        if tool_count >= 1:
            self._content(tool_output)
            return
        if OPEN in user_text:
            title = user_text.split(OPEN, 1)[1].strip()
            self._tool_call("call_open", "toolkit_knowledge_open", {"title": title})
            return
        if tool_output:
            # The tool answered: hand the result back as the answer, so the
            # test can read it.
            self._content(tool_output)
            return
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


def call(port, method, path, body=None, timeout=120):
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


def chat(port, workspace_id, message, session_id="s_layers"):
    """One turn; returns the tool results it produced (in order)."""
    status, payload = call(port, "POST", "/api/chat",
                           {"workspace_id": workspace_id, "message": message,
                            "session_id": session_id})
    raw = payload.get("raw", "") if isinstance(payload, dict) else ""
    outputs = []
    for frame in raw.split("\n\n"):
        frame = frame.strip()
        if not frame.startswith("data:"):
            continue
        body = frame[5:].strip()
        if not body or body == "[DONE]":
            continue
        try:
            event = json.loads(body)
        except ValueError:
            continue
        if event.get("type") == "tool_result":
            outputs.append(event.get("output") or "")
    return status, outputs


def write_file(path, text):
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def parse_json_output(output):
    """The tool answers with JSON; the mock may have wrapped it in words."""
    text = output.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except ValueError:
        return None


def main():
    binary = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        REPO_ROOT, "build-mingw", "bin", "tea_agent.exe")
    if not os.path.isfile(binary):
        print("binary not found: " + binary)
        return 2

    tmp = tempfile.mkdtemp(prefix="tea-layers-")
    mock_port = free_port()
    mock = ThreadingHTTPServer(("127.0.0.1", mock_port), MockProvider)
    threading.Thread(target=mock.serve_forever, daemon=True).start()

    ws_dir = os.path.join(tmp, "ws")
    run_dir = os.path.join(ws_dir, ".tea_agent_run")

    # L2: the journal - one page per day.
    write_file(os.path.join(run_dir, "journal", "2020-01-02.md"),
               "# 2020-01-02\n\n" + JOURNAL_TEXT + "\n\nThe rest of that day.\n")
    # L3: the knowledge pages - one page per thing understood.
    write_file(os.path.join(run_dir, "knowledge", "mqtt-broker.md"),
               "# MQTT Broker\n\n" + PAGE_TEXT + ".\n\n"
               "The broker is the one that talks to the devices.\n\n"
               "See also [[Zone]] and [[Nowhere]].\n")
    write_file(os.path.join(run_dir, "knowledge", "zone.md"),
               "# Zone\n\nA zone belongs to the device.\n\nBack to [[MQTT Broker]].\n")
    # L4: the catalog of the pages.
    write_file(os.path.join(run_dir, "knowledge", "index.md"),
               "# 目录\n\n- MQTT Broker: LAYERKEY what the broker does\n"
               "- Zone: what a zone is\n")

    # The catalog is one line long here, so one of the two pages has to be
    # left out - and the model has to be told that it was.
    write_file(os.path.join(run_dir, "settings.yaml"),
               "settings:\n  memory_catalog_lines: 1\n")

    # A session file that cannot be read: the search must say so instead of
    # pretending the layer was empty.
    write_file(os.path.join(run_dir, "sessions", "s_broken.json"), "{not json")
    with open(os.path.join(run_dir, "sessions", "index.json"), "w", encoding="utf-8") as f:
        json.dump([{"id": "s_broken", "title": "broken", "created_at": "2020-01-01T00:00:00Z",
                    "updated_at": "2020-01-01T00:00:00Z", "message_count": 1}], f)

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
        "--log-file", os.path.join(tmp, "tea_agent.log"), "--workspace", ws_dir,
    ])

    failures = []
    checks = []

    def check(name, ok, detail=""):
        checks.append(name)
        print(("PASS " if ok else "FAIL ") + name +
              ((("  [" + str(detail) + "]")) if not ok else ""))
        if not ok:
            failures.append(name)

    try:
        if not wait_health(agent_port):
            print("server did not become healthy")
            return 1
        wid = call(agent_port, "GET", "/api/workspaces")[1]["workspaces"][0]["id"]

        # ---- 1. say something, so the conversation layer has something ----
        chat(agent_port, wid, SAID_TEXT)

        # ---- 2. one search, all the layers -------------------------------
        status, outputs = chat(agent_port, wid, FIND + KEYWORD)
        check("the turn ran the search tool", status == 200 and outputs, outputs)
        found = parse_json_output(outputs[0]) if outputs else None
        check("the search answered with JSON", bool(found), outputs[:1])
        if not found:
            raise SystemExit(1)
        kinds = [item.get("kind") for item in found.get("items", [])]
        check("the conversation layer is searched", "conversation" in kinds, kinds)
        check("the journal layer is searched", "journal" in kinds, kinds)
        check("the knowledge layer is searched", "knowledge" in kinds, kinds)
        for item in found.get("items", []):
            if item.get("kind") == "journal":
                check("the journal hit carries the day",
                      item.get("title") == "2020-01-02.md", item)
        knowledge_titles = [i.get("title") for i in found.get("items", [])
                          if i.get("kind") == "knowledge"]
        check("a knowledge hit carries the page title",
              "MQTT Broker" in knowledge_titles, knowledge_titles)
        check("every hit carries a ref",
              all(item.get("ref") for item in found.get("items", [])), found.get("items"))
        check("the match is literal: the excerpt holds the words",
              all(KEYWORD in (item.get("text") or "") for item in found.get("items", [])),
              [item.get("text") for item in found.get("items", [])])

        # ---- 3. a source that could not be read says so ------------------
        coverage = found.get("coverage") or {}
        check("an unreadable source is reported, not hidden",
              coverage.get("state") == "partial" and coverage.get("failures"),
              coverage)

        # ---- 4. read the original text back ------------------------------
        journal_ref = ""
        for item in found.get("items", []):
            if item.get("kind") == "journal":
                journal_ref = item.get("ref") or ""
                break
        check("the journal hit has a ref", bool(journal_ref), found.get("items"))
        status, outputs = chat(agent_port, wid, READ + journal_ref)
        read_back = parse_json_output(outputs[0]) if outputs else None
        check("the original text comes back", bool(read_back) and
              JOURNAL_TEXT in (read_back.get("content") or ""), outputs[:1])
        check("the read says how long the original is",
              bool(read_back) and read_back.get("totalChars", 0) > len(JOURNAL_TEXT),
              read_back)

        # ---- 5. a knowledge page opens by name ---------------------------
        status, outputs = chat(agent_port, wid, OPEN + "MQTT Broker")
        opened = parse_json_output(outputs[0]) if outputs else None
        check("the page opens by its title", bool(opened) and
              PAGE_TEXT in (opened.get("content") or ""), outputs[:1])
        related = (opened or {}).get("related") or {}
        check("the page knows the page it links to", "Zone" in (related.get("links") or []),
              related)
        check("the page knows the page that links to it",
              "Zone" in (related.get("linkedFrom") or []), related)
        check("a link to a page nobody wrote is reported",
              "Nowhere" in (related.get("missing") or []), related)

        status, outputs = chat(agent_port, wid, OPEN + "No Such Page")
        check("a page that does not exist says so",
              outputs and "no page named" in outputs[0].lower(), outputs[:1])

        # ---- 6. zero hits is not "never happened" ------------------------
        status, outputs = chat(agent_port, wid, MISS)
        empty = parse_json_output(outputs[0]) if outputs else None
        check("a search that finds nothing returns no items",
              bool(empty) and not empty.get("items"), empty)
        check("a search that finds nothing explains itself",
              bool(empty) and empty.get("note"), empty)

        # ---- 7. the cursor belongs to the query --------------------------
        status, outputs = chat(agent_port, wid, FIND1 + KEYWORD)
        first = parse_json_output(outputs[0]) if outputs else None
        cursor = (first or {}).get("nextCursor") or ""
        check("a limited search says there is more", bool(cursor), first)
        status, outputs = chat(agent_port, wid, PAGE + KEYWORD)
        check("the page turn searched twice", len(outputs) >= 2, outputs)
        page_one = parse_json_output(outputs[0]) if outputs else None
        page_two = parse_json_output(outputs[1]) if len(outputs) > 1 else None
        page_one_ref = ((page_one or {}).get("items") or [{}])[0].get("ref")
        page_two_ref = ((page_two or {}).get("items") or [{}])[0].get("ref")
        check("the second page starts somewhere else",
              page_one is not None and page_two is not None and page_two_ref and
              page_two_ref != page_one_ref, (page_one_ref, page_two_ref))
        check("the cursor did not expire on its own query",
              page_two is not None and "cursorExpired" not in json.dumps(page_two), page_two)
        refused = outputs[2] if len(outputs) > 2 else ""
        check("a cursor from another query is refused",
              "cursorExpired" in refused, refused[:200])

        # ---- 8. the catalog of the pages ---------------------------------
        status, outputs = chat(agent_port, wid, FIND + KEYWORD)
        catalog = seen_system[-1] if seen_system else ""
        check("the catalog is in the prompt", "Knowledge pages" in catalog, catalog[-300:])
        check("the page that matters for this conversation is listed first",
              "[[MQTT Broker]]" in catalog, catalog[-300:])
        check("the page that did not fit is not listed", "[[Zone]]" not in catalog,
              catalog[-300:])
        check("the model is told how many pages it did not see",
              "1 more page not listed" in catalog, catalog[-300:])

        # ---- 9. the oldest first ----------------------------------------
        status, outputs = chat(agent_port, wid, OLDEST + KEYWORD)
        oldest = parse_json_output(outputs[0]) if outputs else None
        items = (oldest or {}).get("items") or []
        check("the oldest hit comes first", items and items[0].get("kind") == "journal",
              [i.get("kind") for i in items])

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
