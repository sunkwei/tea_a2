#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The message platform: publish and subscribe over HTTP, next to the queue.

Why this exists: the queue's event stream is a broadcast to whoever happens to
be connected right now - no ids, no replay, no token, and a slow listener grows
the queue's memory. That is fine for "somebody is watching the web page"; it is
not fine for the pieces that have to keep working when nobody watches: the
router (which speaks for the work) and the watchdog (which has to notice that
the router stopped speaking). So this is a small bus with the three things a
watcher needs:

  * **ids + since** - a subscriber that reconnects says where it stopped; what
    is still in the window is handed over, and what is not is said out loud
    (a `gap` frame) instead of being silently skipped;
  * **topics** - `a2/queue/task`, `a2/router/speech`, `a2/heartbeat/#` ... with
    the same `+` / `#` matching MQTT uses, so the day this moves to MQTT the
    subscribers do not change;
  * **a token** - it listens on 127.0.0.1 only, and publishing or subscribing
    needs the token file (`<data>/token`, mode 0600).

What it deliberately is not: a database. The window (1000 messages / 60
minutes), the retained message of a topic and the idempotency keys all live in
memory; a restart clears them, and `/health` says `"persisted": false` so
nobody believes otherwise. The queue stays the source of truth for the work;
this only distributes what happened.

Usage:

  # start it (default: 127.0.0.1:8299, data in ~/.tea_agent/bus)
  python3 sched/bus_server.py

  # publish one message
  curl -X POST http://127.0.0.1:8299/api/publish \
       -H "Authorization: Bearer $(cat ~/.tea_agent/bus/token)" \
       -H 'Content-Type: application/json' \
       -d '{"topic":"a2/test/hello","payload":{"a":1}}'

  # watch everything (curl keeps the stream open)
  curl -N "http://127.0.0.1:8299/api/subscribe?filter=a2/%23&since=0" \
       -H "Authorization: Bearer $(cat ~/.tea_agent/bus/token)"

Standard library only, Python 3.6: this runs on the device, next to the queue.
"""

import argparse
import hmac
import json
import os
import re
import select
import signal
import socket
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler

if sys.version_info[0] < 3:
    # 这台机器的 `python` 是 2.7（`python3` 才是 3.6）。这个脚本是 Python 3 的，
    # 在 2 里连解析都过不去 —— 与其让用户看 "Non-ASCII character"，
    # 不如直接说该用哪个解释器。
    sys.stderr.write("这个脚本要 Python 3：请用 python3 跑"
                     "（这台机器的 python 是 2.7）。\n")
    sys.exit(2)

try:
    # Python 3.7+
    from http.server import ThreadingHTTPServer
except ImportError:  # Python 3.6
    from http.server import HTTPServer
    from socketserver import ThreadingMixIn

    class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
        """The 3.6 spelling of http.server.ThreadingHTTPServer."""
        daemon_threads = True


VERSION = "1.0.0"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8299
DEFAULT_WINDOW_MESSAGES = 1000
DEFAULT_WINDOW_MINUTES = 60
DEFAULT_MAX_PAYLOAD = 64 * 1024
DEFAULT_SUBSCRIBER_BUFFER = 1000
DEFAULT_KEY_TABLE = 2000
DEFAULT_KEEPALIVE = 15.0
DEFAULT_IDLE_TIMEOUT = 3600.0
DEFAULT_HEARTBEAT_FILE = 10.0
DEFAULT_HEARTBEAT_TOPIC = 30.0

# The shape of a topic level. Lower case on purpose: the bus is written by our
# own processes, and one spelling per topic is worth more than freedom here.
LEVEL_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")
MAX_TOPIC_LEN = 200
MAX_LEVELS = 8

# A key is the caller's name for "this event"; it is not a path.
KEY_RE = re.compile(r"^[!-~]{1,200}$")

TEA_HOME = os.path.expanduser(os.environ.get("TEA_AGENT_HOME", os.path.join("~", ".tea_agent")))


def now_iso():
    """UTC, milliseconds - the same spelling everywhere else in the repo."""
    millis = int((time.time() % 1) * 1000)
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + (".%03dZ" % millis)


def log(message):
    print("%s  %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), message), flush=True)


_LOG_LOCK = threading.Lock()
_LOG_LAST = {}


def log_throttled(key, message, interval=60.0):
    """A line that would repeat on every attempt is written once per interval."""
    now = time.time()
    with _LOG_LOCK:
        last = _LOG_LAST.get(key)
        if last is not None and (now - last) < interval:
            return False
        _LOG_LAST[key] = now
    log(message)
    return True


# ---- topics -----------------------------------------------------------------

def topic_error(topic):
    """Why this topic name is not acceptable (empty string = it is fine)."""
    if not isinstance(topic, str) or not topic:
        return "the topic is empty"
    if len(topic) > MAX_TOPIC_LEN:
        return "the topic is longer than %d characters" % MAX_TOPIC_LEN
    levels = topic.split("/")
    if len(levels) > MAX_LEVELS:
        return "the topic has more than %d levels" % MAX_LEVELS
    for level in levels:
        if not level:
            return "the topic has an empty level (leading / or //)"
        if not LEVEL_RE.match(level):
            return "the level %r is not lower-case [a-z0-9_.-]" % level
    if "#" in topic or "+" in topic:
        return "a topic name may not contain + or #"
    return ""


def filter_error(flt):
    """Why this filter is not acceptable (empty string = it is fine)."""
    if not isinstance(flt, str) or not flt:
        return "the filter is empty"
    if len(flt) > MAX_TOPIC_LEN:
        return "the filter is longer than %d characters" % MAX_TOPIC_LEN
    levels = flt.split("/")
    if len(levels) > MAX_LEVELS:
        return "the filter has more than %d levels" % MAX_LEVELS
    for i, level in enumerate(levels):
        if level == "#":
            if i != len(levels) - 1:
                return "'#' must be the last level"
            continue
        if level == "+":
            continue
        if not level:
            return "the filter has an empty level"
        if "#" in level or "+" in level:
            return "a wildcard must be a level of its own"
        if not LEVEL_RE.match(level):
            return "the level %r is not lower-case [a-z0-9_.-]" % level
    return ""


def topic_matches(flt, topic):
    """MQTT wildcard matching: '+' is one level, '#' is the rest.

    The same rules as src/mqtt_codec.h (topic_matches): `a/#` matches `a` too,
    because '#' swallows the rest - including nothing.
    """
    flevels = flt.split("/")
    tlevels = topic.split("/")
    for i, level in enumerate(flevels):
        if level == "#":
            return True
        if i >= len(tlevels):
            return False
        if level != "+" and level != tlevels[i]:
            return False
    return len(flevels) == len(tlevels)


# ---- the data -----------------------------------------------------------------

def public(message):
    """The message without the fields the server keeps for itself."""
    return {k: v for k, v in message.items() if not k.startswith("_")}


class Subscriber(object):
    """One open subscription: a filter, a backlog and a pending wake-up.

    The backlog is capped: a subscriber that stops reading must not grow the
    server's memory. What is dropped is counted (`dropped`) - the only honest
    answer to "did I miss anything".
    """

    def __init__(self, sid, flt, since, buffer_size):
        self.id = sid
        self.filter = flt
        self.since = since
        self.lock = threading.Lock()
        self.queue = []
        # The backlog of a `since` request is bounded by the window (it is
        # the window that is handed over), not by the live buffer: someone
        # who asks for 1000 messages must get them even if he is slower than
        # the live traffic afterwards.
        self.backlog = []
        self.buffer_size = max(1, int(buffer_size))
        self.dropped = 0
        self.connected_at = now_iso()
        self.last_sent_at = ""
        self.closed = False

    def put(self, frame):
        """Queue one frame; drop the oldest when the buffer is full."""
        with self.lock:
            if self.closed:
                return
            if len(self.queue) >= self.buffer_size:
                self.queue.pop(0)
                self.dropped += 1
                if self.dropped >= self.buffer_size * 4:
                    # Hopelessly behind: free the memory and let it reconnect
                    # with `since` - a live connection nobody reads is worse.
                    self.closed = True
                    self.queue = []
                    return
            self.queue.append(frame)

    def put_backlog(self, frame):
        """Queue one frame of the initial hand-over (no live cap)."""
        with self.lock:
            if not self.closed:
                self.backlog.append(frame)

    def drain(self):
        with self.lock:
            frames = self.backlog + self.queue
            self.backlog = []
            self.queue = []
        return frames

    def queued(self):
        with self.lock:
            return len(self.backlog) + len(self.queue)

    def close(self):
        with self.lock:
            self.closed = True
            self.queue = []
            self.backlog = []


class Hub(object):
    """The window, the retained messages, the keys and the subscribers."""

    def __init__(self, window_messages, window_minutes, subscriber_buffer,
                 key_table=DEFAULT_KEY_TABLE):
        self.lock = threading.RLock()
        self.messages = []            # oldest first; each has "_epoch"
        self.capacity = max(1, int(window_messages))
        self.window_seconds = max(0.0, float(window_minutes) * 60.0)
        self.retained = {}            # topic -> message
        self.keys = {}                # key -> id
        self.subscribers = []
        self.next_id = 1
        self.next_subscriber_id = 1
        self.subscriber_buffer = max(1, int(subscriber_buffer))
        self.key_table = max(1, int(key_table))
        self.started = time.time()
        self.closed = False
        self.host = ""
        self.port = 0

    # -- publishing -------------------------------------------------------
    def publish(self, topic, payload, key="", retain=False):
        """(message, duplicate). Raises ValueError for a bad topic/payload."""
        problem = topic_error(topic)
        if problem:
            raise ValueError("bad_topic: " + problem)
        if not isinstance(payload, dict):
            raise ValueError("bad_payload: the payload must be a JSON object")
        if key and not KEY_RE.match(key):
            raise ValueError("bad_key: the key must be 1-200 printable characters")
        now = time.time()
        with self.lock:
            if self.closed:
                raise ValueError("shutting_down: the bus is stopping")
            if key and key in self.keys:
                message = self._by_id(self.keys[key])
                if message is not None:
                    return public(message), True
                # The message went out of the window: the key is stale. Drop it
                # and let the new message take it - otherwise a key would stay
                # poisoned forever.
                self.keys.pop(key, None)
            self._prune(now)
            message = {
                "id": self.next_id,
                "topic": topic,
                "at": now_iso(),
                "payload": payload,
                "_epoch": now,
            }
            if key:
                message["key"] = key
            self.next_id += 1
            self.messages.append(message)
            while len(self.messages) > self.capacity:
                self._drop_oldest()
            if retain:
                self.retained[topic] = message
            if key:
                self.keys[key] = message["id"]
                while len(self.keys) > self.key_table:
                    self._drop_oldest_key()
            frame = {"event": "message", "id": message["id"], "data": public(message)}
            for subscriber in self.subscribers:
                if topic_matches(subscriber.filter, topic):
                    subscriber.put(frame)
            return public(message), False

    def _prune(self, now):
        """Drop what fell out of the window by time."""
        if self.window_seconds <= 0:
            return
        cutoff = now - self.window_seconds
        while self.messages and self.messages[0]["_epoch"] < cutoff:
            self._drop_oldest()

    def _drop_oldest(self):
        message = self.messages.pop(0)
        # The retained copy must not outlive the message it points at: the
        # window is the memory, and a retained message older than the window
        # would be a lie about "the last state".
        if message.get("key"):
            if self.keys.get(message["key"]) == message["id"]:
                self.keys.pop(message["key"], None)
        topic = message.get("topic") or ""
        kept = self.retained.get(topic)
        if kept is not None and kept.get("id") == message["id"]:
            self.retained.pop(topic, None)

    def _drop_oldest_key(self):
        oldest_id = None
        oldest_key = None
        for key, message_id in self.keys.items():
            if oldest_id is None or message_id < oldest_id:
                oldest_id, oldest_key = message_id, key
        if oldest_key is not None:
            self.keys.pop(oldest_key, None)

    def _by_id(self, message_id):
        for message in self.messages:
            if message["id"] == message_id:
                return message
        return None

    # -- subscribing ------------------------------------------------------
    def subscribe(self, flt, since):
        """Register a subscriber and hand it the backlog, atomically.

        `since` is None (live only), 0 (everything in the window) or N
        (everything after N). When N cannot be honoured the subscriber first
        gets a `gap` frame - never a silent hole.
        """
        with self.lock:
            self._prune(time.time())
            subscriber = Subscriber(self.next_subscriber_id, flt, since,
                                    self.subscriber_buffer)
            self.next_subscriber_id += 1
            if since is not None:
                backlog = list(self.messages)
                if backlog:
                    oldest = backlog[0]["id"]
                    newest = backlog[-1]["id"]
                    if since > newest:
                        # The cursor is from another run (ids restart at 1):
                        # say so instead of pretending there is nothing new.
                        subscriber.put_backlog({"event": "gap", "data": {
                            "reason": "no_history", "requested": since,
                            "oldest": oldest, "newest": newest}})
                    elif since < oldest - 1:
                        subscriber.put_backlog({"event": "gap", "data": {
                            "reason": "window", "requested": since,
                            "oldest": oldest, "newest": newest}})
                        backlog = [m for m in backlog if m["id"] > since]
                    else:
                        backlog = [m for m in backlog if m["id"] > since]
                elif since > 0:
                    subscriber.put_backlog({"event": "gap", "data": {
                        "reason": "no_history", "requested": since,
                        "oldest": None, "newest": None}})
                for message in backlog:
                    if topic_matches(flt, message["topic"]):
                        subscriber.put_backlog({"event": "message",
                                                "id": message["id"],
                                                "data": public(message)})
            self.subscribers.append(subscriber)
            return subscriber

    def unsubscribe(self, subscriber):
        with self.lock:
            if subscriber in self.subscribers:
                self.subscribers.remove(subscriber)

    # -- observing --------------------------------------------------------
    def health(self):
        with self.lock:
            self._prune(time.time())
            subscribers = []
            backlog = 0
            for subscriber in self.subscribers:
                queued = subscriber.queued()
                backlog += queued
                subscribers.append({
                    "id": "s_%d" % subscriber.id,
                    "filter": subscriber.filter,
                    "since": subscriber.since,
                    "connected_at": subscriber.connected_at,
                    "last_sent_at": subscriber.last_sent_at,
                    "queued": queued,
                    "dropped": subscriber.dropped,
                })
            window = {"capacity": self.capacity,
                      "minutes": self.window_seconds / 60.0,
                      "messages": len(self.messages),
                      "oldest": self.messages[0]["id"] if self.messages else None,
                      "newest": self.messages[-1]["id"] if self.messages else None}
            last = self.messages[-1]["at"] if self.messages else ""
            return {
                "ok": True,
                "version": VERSION,
                "pid": os.getpid(),
                "uptime_s": int(time.time() - self.started),
                "persisted": False,
                "host": self.host,
                "port": self.port,
                "window": window,
                "retained": sorted(self.retained.keys()),
                "last_message_at": last,
                "backlog": backlog,
                "subscribers": subscribers,
            }

    def close_all(self):
        """Tell every subscriber the platform is going away, then stop.

        The frame is queued but the subscriber is not closed here: the
        handler has to drain what is already queued (the frame among it)
        before it exits, otherwise the last thing a client hears is
        silence - and silence is what 'the platform is dead' looks like.
        """
        with self.lock:
            self.closed = True
            for subscriber in list(self.subscribers):
                subscriber.put({"event": "closed",
                                "data": {"reason": "shutting_down"}})
            self.subscribers = []


# ---- the HTTP side ------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    """Three routes: publish, subscribe, health."""

    server_version = "tea-bus/" + VERSION
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def handle_one_request(self):
        """A hanging-up client (a closed tab, a killed subscriber) is normal."""
        try:
            BaseHTTPRequestHandler.handle_one_request(self)
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            pass

    # -- helpers ----------------------------------------------------------
    @property
    def app(self):
        return self.server.app

    def _authorized(self):
        token = self.app.token
        if not token:
            return True
        given = (self.headers.get("X-Bus-Token") or "").strip()
        if not given:
            auth = (self.headers.get("Authorization") or "").strip()
            if auth.lower().startswith("bearer "):
                given = auth[7:].strip()
        return hmac.compare_digest(given, token)

    def _json(self, status, payload):
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(raw)

    def _error(self, status, code, message, extra=None):
        body = {"code": code, "message": message}
        if extra:
            body.update(extra)
        self._json(status, body)

    def _body(self):
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length <= 0:
            raise ValueError("the request has no body")
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            raise ValueError("the body is not JSON")

    # -- routes -----------------------------------------------------------
    def do_GET(self):  # noqa: N802 (http.server API)
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/health":
            return self._json(200, self.app.hub.health())
        if path == "/":
            return self._json(200, {"ok": True, "name": "tea_a2 bus",
                                    "version": VERSION,
                                    "publish": "POST /api/publish",
                                    "subscribe": "GET /api/subscribe?filter=...&since=...",
                                    "health": "GET /health"})
        if path == "/api/subscribe":
            return self._subscribe(urllib.parse.parse_qs(parsed.query))
        return self._error(404, "not_found", "no such path: %s" % path)

    def do_POST(self):  # noqa: N802 (http.server API)
        if self.path != "/api/publish":
            return self._error(404, "not_found", "no such path: %s" % self.path)
        if not self._authorized():
            return self._error(401, "unauthorized", "the token is missing or wrong")
        declared = int(self.headers.get("Content-Length", "0") or 0)
        if declared > self.app.max_payload + 4096:
            return self._error(413, "payload_too_large",
                               "the body is %d bytes; the limit is %d"
                               % (declared, self.app.max_payload),
                               {"limit": self.app.max_payload, "size": declared})
        try:
            body = self._body()
        except ValueError as exc:
            return self._error(400, "bad_request", str(exc))
        if not isinstance(body, dict):
            return self._error(400, "bad_request", "the body must be a JSON object")
        topic = body.get("topic")
        payload = body.get("payload")
        key = body.get("key") or ""
        retain = bool(body.get("retain"))
        if payload is None:
            payload = {}
        try:
            raw_payload = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        except (TypeError, ValueError):
            return self._error(400, "bad_payload", "the payload is not JSON")
        if len(raw_payload) > self.app.max_payload:
            return self._error(413, "payload_too_large",
                               "the payload is %d bytes; the limit is %d"
                               % (len(raw_payload), self.app.max_payload),
                               {"limit": self.app.max_payload,
                                "size": len(raw_payload)})
        try:
            message, duplicate = self.app.hub.publish(topic, payload, key, retain)
        except ValueError as exc:
            code, _, message = str(exc).partition(": ")
            if code == "shutting_down":
                return self._error(503, code, message or "the bus is stopping")
            if code not in ("bad_topic", "bad_payload", "bad_key"):
                code, message = "bad_request", str(exc)
            return self._error(400, code, message)
        answer = {"id": message["id"], "topic": message["topic"], "at": message["at"]}
        if duplicate:
            answer["duplicate"] = True
        return self._json(200, answer)

    # -- the event stream ---------------------------------------------------
    def _subscribe(self, query):
        if not self._authorized():
            return self._error(401, "unauthorized", "the token is missing or wrong")
        flt = (query.get("filter") or [""])[0].strip()
        problem = filter_error(flt)
        if problem:
            return self._error(400, "bad_filter", problem)
        since = None
        raw_since = (query.get("since") or [""])[0].strip()
        if raw_since != "":
            try:
                since = int(raw_since)
            except ValueError:
                return self._error(400, "bad_since", "since must be an integer")
            if since < 0:
                return self._error(400, "bad_since", "since must not be negative")
        hub = self.app.hub
        subscriber = hub.subscribe(flt, since)
        idle_timeout = self.app.idle_timeout
        keepalive = self.app.keepalive
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        idle = 0.0
        quiet = 0.0
        try:
            while True:
                frames = subscriber.drain()
                if frames:
                    for frame in frames:
                        self._frame(frame)
                        if frame.get("event") == "closed":
                            subscriber.closed = True
                    subscriber.last_sent_at = now_iso()
                    idle = 0.0
                    quiet = 0.0
                # What was queued has been written: only now may the loop
                # stop (the `closed` frame above is the last thing sent).
                if subscriber.closed or hub.closed:
                    break
                if not frames:
                    time.sleep(0.2)
                    idle += 0.2
                    quiet += 0.2
                    if self._client_gone():
                        break
                    if keepalive > 0 and quiet >= keepalive:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                        quiet = 0.0
                    if idle_timeout > 0 and idle >= idle_timeout:
                        log("subscriber %s idle for %d s - closing it"
                            % (subscriber.id, idle_timeout))
                        break
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            hub.unsubscribe(subscriber)

    def _frame(self, frame):
        """One SSE frame. A gap carries no `id:` - it is not a message, and the
        client must not remember it as 'the last one I saw'."""
        event = frame.get("event") or "message"
        data = json.dumps(frame.get("data") or {}, ensure_ascii=False)
        out = []
        if frame.get("id") is not None:
            out.append("id: %d" % int(frame["id"]))
        out.append("event: %s" % event)
        out.append("data: %s" % data)
        self.wfile.write(("\n".join(out) + "\n\n").encode("utf-8"))
        self.wfile.flush()

    def _client_gone(self):
        """Is the client still there? A closed tab shows up as an empty read."""
        try:
            ready, _, _ = select.select([self.connection], [], [], 0)
            if not ready:
                return False
            return not self.connection.recv(1024, socket.MSG_PEEK)
        except (OSError, ValueError):
            return True


class BusServer(ThreadingHTTPServer):
    """A server that refuses to share its port (silently) and knows its app."""

    daemon_threads = True
    allow_reuse_address = False

    def server_bind(self):
        # Windows: SO_REUSEADDR off is not enough - another process may still
        # bind the same port. SO_EXCLUSIVEADDRUSE is the one that refuses.
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            try:
                self.socket.setsockopt(socket.SOL_SOCKET,
                                       socket.SO_EXCLUSIVEADDRUSE, 1)
            except OSError:
                pass
        ThreadingHTTPServer.server_bind(self)


# ---- process plumbing ---------------------------------------------------------

class App(object):
    """Everything the handler needs: the hub, the token, the limits."""

    def __init__(self, hub, token, max_payload, keepalive, idle_timeout, data_dir):
        self.hub = hub
        self.token = token
        self.max_payload = max_payload
        self.keepalive = keepalive
        self.idle_timeout = idle_timeout
        self.data_dir = data_dir


def pid_alive(pid):
    """Does this pid name a running process?

    Windows needs the Win32 call: os.kill(pid, 0) is not a probe there - it
    terminates the process. (Same helper as tests/smoke_port_guard.py.)
    """
    if not pid:
        return False
    if os.name == "nt":
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, int(pid))
        if not handle:
            return kernel32.GetLastError() == 5  # access denied: it exists
        try:
            code = ctypes.c_ulong()
            return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def write_file_atomic(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def ensure_token(path, given=""):
    """The token file, mode 0600. An existing one is never overwritten."""
    if given:
        return given
    try:
        with open(path, encoding="utf-8") as fh:
            token = fh.read().strip()
        if token:
            return token
    except OSError:
        pass
    token = os.urandom(32).hex()
    try:
        # os.open with 0600 keeps the window between "created" and
        # "chmod" closed; O_EXCL means two starters cannot split the token.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        # It exists but was empty (a crash between create and write):
        # rewrite it atomically instead of failing every start from now on.
        write_file_atomic(path, token + "\n")
        return token
    try:
        os.write(fd, (token + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    return token


def load_config(path):
    return read_json(path)


def build_parser():
    parser = argparse.ArgumentParser(
        description="The tea_a2 message platform: publish/subscribe over HTTP.")
    parser.add_argument("--host", default=None,
                        help="listen address (default %s; 0.0.0.0 is warned about)"
                             % DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=None,
                        help="listen port (default %d)" % DEFAULT_PORT)
    parser.add_argument("--data-dir", default=None,
                        help="where the token, the pid file and the heartbeat live "
                             "(default <tea home>/bus)")
    parser.add_argument("--config", default=None,
                        help="the JSON config (default <tea home>/bus.json)")
    parser.add_argument("--token", default="",
                        help="override the token (tests only; the file is the real way)")
    parser.add_argument("--window-messages", type=int, default=None,
                        help="how many messages the window holds (default %d)"
                             % DEFAULT_WINDOW_MESSAGES)
    parser.add_argument("--window-minutes", type=float, default=None,
                        help="how long a message stays in the window (default %d)"
                             % DEFAULT_WINDOW_MINUTES)
    parser.add_argument("--max-payload", type=int, default=None,
                        help="the largest payload, in bytes (default %d)"
                             % DEFAULT_MAX_PAYLOAD)
    parser.add_argument("--subscriber-buffer", type=int, default=None,
                        help="how many frames one subscriber may fall behind "
                             "(default %d)" % DEFAULT_SUBSCRIBER_BUFFER)
    parser.add_argument("--keepalive", type=float, default=None,
                        help="seconds between SSE keepalive comments (default %g, 0 = off)"
                             % DEFAULT_KEEPALIVE)
    parser.add_argument("--idle-timeout", type=float, default=None,
                        help="close a silent stream after this many seconds "
                             "(default %g, 0 = never)" % DEFAULT_IDLE_TIMEOUT)
    parser.add_argument("--heartbeat-file", type=float, default=None,
                        help="seconds between heartbeat-file writes "
                             "(default %g)" % DEFAULT_HEARTBEAT_FILE)
    parser.add_argument("--heartbeat-topic", type=float, default=None,
                        help="seconds between a2/heartbeat/bus messages "
                             "(default %g)" % DEFAULT_HEARTBEAT_TOPIC)
    parser.add_argument("--version", action="store_true", help="print the version and exit")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.version:
        print(VERSION)
        return 0

    tea_home = TEA_HOME
    data_dir = os.path.abspath(os.path.expanduser(
        args.data_dir or os.environ.get("TEA_BUS_DATA") or os.path.join(tea_home, "bus")))
    config_path = os.path.abspath(os.path.expanduser(
        args.config or os.path.join(tea_home, "bus.json")))
    config = load_config(config_path)
    os.makedirs(data_dir, exist_ok=True)

    host = args.host or os.environ.get("TEA_BUS_HOST") or config.get("host") or DEFAULT_HOST
    port = args.port or int(os.environ.get("TEA_BUS_PORT") or 0) or \
        int(config.get("port") or DEFAULT_PORT)
    window_messages = args.window_messages or int(
        config.get("window_messages") or DEFAULT_WINDOW_MESSAGES)
    window_minutes = args.window_minutes or float(
        config.get("window_minutes") or DEFAULT_WINDOW_MINUTES)
    max_payload = args.max_payload or int(
        config.get("max_payload_bytes") or DEFAULT_MAX_PAYLOAD)
    subscriber_buffer = args.subscriber_buffer or int(
        config.get("subscriber_buffer") or DEFAULT_SUBSCRIBER_BUFFER)
    keepalive = args.keepalive if args.keepalive is not None else float(
        config.get("sse_keepalive_s") or DEFAULT_KEEPALIVE)
    idle_timeout = args.idle_timeout if args.idle_timeout is not None else float(
        config.get("sse_idle_timeout_s") or DEFAULT_IDLE_TIMEOUT)
    heartbeat_file = args.heartbeat_file if args.heartbeat_file is not None else float(
        config.get("heartbeat_file_s") or DEFAULT_HEARTBEAT_FILE)
    heartbeat_topic = args.heartbeat_topic if args.heartbeat_topic is not None else float(
        config.get("heartbeat_topic_s") or DEFAULT_HEARTBEAT_TOPIC)

    token = ensure_token(os.path.join(data_dir, "token"), args.token)
    hub = Hub(window_messages, window_minutes, subscriber_buffer)
    hub.host = host
    hub.port = port
    app = App(hub, token, max_payload, keepalive, idle_timeout, data_dir)

    pid_path = os.path.join(data_dir, "bus.pid")
    previous = read_json(pid_path)
    if previous.get("pid") and pid_alive(previous["pid"]) and int(previous["pid"]) != os.getpid():
        log("bus: another instance is already running (pid %s, started %s, data %s)"
            % (previous.get("pid"), previous.get("started_at") or "?", data_dir))
        log("bus: stop it first, or give this one another --port/--data-dir")
        return 3

    try:
        server = BusServer((host, port), Handler)
    except OSError as exc:
        holder = ""
        if previous.get("pid"):
            holder = " (the pid file says pid %s, started %s)" % (
                previous.get("pid"), previous.get("started_at") or "?")
        log("bus: cannot listen on %s:%d: %s%s" % (host, port, exc, holder))
        log("bus: is another bus (or another program) on that port? Use --port.")
        return 3

    server.app = app
    write_file_atomic(pid_path, json.dumps(
        {"pid": os.getpid(), "started_at": now_iso(), "host": host, "port": port},
        ensure_ascii=False, indent=2) + "\n")

    if host not in ("127.0.0.1", "localhost", "::1"):
        log("bus: WARNING - listening on %s, not just this machine. There is one "
            "token and no TLS: do not put this on a network you do not control." % host)
    log("bus %s: listening on %s:%d (data %s, window %d messages / %g min, "
        "token %s...)" % (VERSION, host, port, data_dir, window_messages,
                          window_minutes, token[:8]))

    stop = threading.Event()

    def heartbeat():
        """One file for 'the process is alive', one retained message for the
        subscribers. The first is readable even when the platform is the thing
        that died; the second is what the watchdog subscribes to."""
        path = os.path.join(data_dir, "bus.heartbeat")
        last_topic = 0.0
        while not stop.wait(max(0.1, heartbeat_file)):
            try:
                write_file_atomic(path, json.dumps(
                    {"pid": os.getpid(), "at": now_iso()}, ensure_ascii=False) + "\n")
            except OSError as exc:
                log_throttled("heartbeat-file", "bus: cannot write the heartbeat: %s" % exc)
            if time.time() - last_topic >= max(0.1, heartbeat_topic):
                last_topic = time.time()
                try:
                    hub.publish("a2/heartbeat/bus", {
                        "at": now_iso(), "pid": os.getpid(), "version": VERSION,
                        "uptime_s": int(time.time() - hub.started),
                        "window": {"oldest": hub.messages[0]["id"] if hub.messages else None,
                                   "newest": hub.messages[-1]["id"] if hub.messages else None},
                        "subscribers": len(hub.subscribers),
                    }, retain=True)
                except ValueError as exc:
                    log_throttled("heartbeat-topic", "bus: heartbeat publish failed: %s" % exc)

    thread = threading.Thread(target=heartbeat, name="bus-heartbeat", daemon=True)
    thread.start()

    def on_term(signum, frame):
        # systemd and subprocess.terminate() send SIGTERM, not SIGINT: stop
        # the same way, so the pid file is removed and the subscribers get
        # their `closed` frame.
        raise KeyboardInterrupt

    if hasattr(signal, "SIGTERM"):
        try:
            signal.signal(signal.SIGTERM, on_term)
        except (ValueError, OSError):
            pass
    try:
        server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        log("bus: stopping")
    finally:
        stop.set()
        hub.close_all()
        try:
            server.server_close()
        except OSError:
            pass
        try:
            os.unlink(pid_path)
        except OSError:
            pass
        log("bus: stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
