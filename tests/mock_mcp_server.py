#!/usr/bin/env python3
"""A mock MCP server: it speaks the MCP stdio protocol and nothing else.

The agent starts an MCP server as a child process and talks to it over its
stdin/stdout with newline-delimited JSON-RPC 2.0. This script is that child
process, so the MCP client can be tested without a real server:

    python3 tests/mock_mcp_server.py            # a well-behaved server
    python3 tests/mock_mcp_server.py --crash    # dies at once
    python3 tests/mock_mcp_server.py --garbage  # answers nonsense

It offers three tools:

    echo     - says the text back
    add      - adds two numbers
    picture  - returns a small PNG, to exercise the image path

Usage:  python3 tests/mock_mcp_server.py [--crash] [--garbage] [--slow]
"""

import base64
import json
import sys
import time

PROTOCOL_VERSION = "2024-11-05"

# A 1x1 PNG. The agent stores it in the workspace and attaches it to the
# conversation, so this is what proves that an MCP image survives the trip.
ONE_PIXEL_PNG = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)

TOOLS = [
    {
        "name": "echo",
        "description": "Echo the text back, with a marker in front of it.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "what to echo"}},
            "required": ["text"],
        },
    },
    {
        "name": "add",
        "description": "Add two numbers and return the sum.",
        "inputSchema": {
            "type": "object",
            "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
            "required": ["a", "b"],
        },
    },
    {
        "name": "picture",
        "description": "Return a small PNG image.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def send(message):
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def ok(msg_id, result):
    send({"jsonrpc": "2.0", "id": msg_id, "result": result})


def fail(msg_id, code, message):
    send({"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}})


def call_tool(params):
    name = params.get("name")
    args = params.get("arguments") or {}
    if name == "echo":
        text = args.get("text")
        if not isinstance(text, str):
            return {"content": [{"type": "text", "text": "echo needs a 'text' argument"}],
                    "isError": True}
        return {"content": [{"type": "text", "text": "MOCK-ECHO: " + text}]}
    if name == "add":
        a, b = args.get("a"), args.get("b")
        if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
            return {"content": [{"type": "text", "text": "add needs the numbers 'a' and 'b'"}],
                    "isError": True}
        return {"content": [{"type": "text", "text": "MOCK-SUM: %s" % (a + b)}]}
    if name == "picture":
        return {"content": [{"type": "image", "data": ONE_PIXEL_PNG, "mimeType": "image/png"}]}
    return {"content": [{"type": "text", "text": "there is no tool called '%s'" % name}],
            "isError": True}


def main():
    crash = "--crash" in sys.argv
    garbage = "--garbage" in sys.argv
    slow = "--slow" in sys.argv
    if crash:
        sys.stderr.write("mock-mcp: crashing on purpose\n")
        sys.stderr.flush()
        return 3

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if not isinstance(message, dict):
            continue
        method = message.get("method")
        msg_id = message.get("id")
        if garbage:
            # A server that answers nonsense: the client must report that
            # instead of hanging or believing it.
            sys.stdout.write("this is not json\n")
            sys.stdout.flush()
            continue
        if slow:
            time.sleep(2.0)
        if method == "initialize":
            ok(msg_id, {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "mock-mcp", "version": "1.0.0"},
            })
        elif method == "tools/list":
            ok(msg_id, {"tools": TOOLS})
        elif method == "tools/call":
            ok(msg_id, call_tool(message.get("params") or {}))
        elif method == "notifications/initialized":
            continue
        elif method is not None and msg_id is not None:
            fail(msg_id, -32601, "the mock server does not implement '%s'" % method)
    return 0


if __name__ == "__main__":
    sys.exit(main())
