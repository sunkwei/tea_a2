#!/usr/bin/env python3
"""The extension layer as an MCP server: a2's tools can grow at runtime.

a2 starts this as a child process (it is an MCP server in `mcp.yaml`) and
talks to it over stdin/stdout with newline-delimited JSON-RPC. The tools it
offers are:

  * the four meta tools - save a tool, reload, list the versions, roll back;
  * every tool the saved modules expose.

The point of the MCP seam: **a2 needs no C++ change for a new tool**. The
module is Python, it runs in *this* process, and the call happens inside
a2's loop - so the turn's budget, the trajectory and the stop semantics are
a2's, unchanged. A module that hangs hangs this server, not the agent.

Usage (as a2 starts it):

    python3 -m a2_ext_py.toolkit.mcp_server --modules ~/.tea_agent/ext \\
            --agent-url http://127.0.0.1:8282

`--agent-url` is optional: with it, a reload also asks a2 to re-read the
tool list (`POST /api/mcp/refresh`), so the new tool is usable in the next
turn. Without it, the caller has to do it.
"""

import argparse
import json
import os
import os.path as osp
import sys
import threading
import time
import urllib.error
import urllib.request

# The package lives in <sched>/a2_ext_py/... - make it importable when this
# file is run as a script (the agent starts it with a path, not with -m).
sys.path.insert(0, osp.dirname(osp.dirname(osp.dirname(osp.abspath(__file__)))))

from a2_ext_py.toolkit.loader import Toolkit, ToolError  # noqa: E402
from a2_ext_py.toolkit.store import (list_versions, rollback_tool,  # noqa: E402
                                     save_tool)

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "a2ext"
SERVER_VERSION = "1.0.0"

META_TOOLS = [
    {
        "name": "toolkit_save",
        "description": ("Save a tool function, so it can be used from now on. "
                        "The code must define the function and its schema; "
                        "call toolkit_reload afterwards to make it usable."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string",
                         "description": "the tool name; it must start with toolkit_"},
                "meta": {"type": "object",
                         "description": "the tool's schema (name, description, inputSchema)"},
                "pycode": {"type": "string",
                           "description": "the python code of the tool"},
                "version": {"type": "string",
                            "description": "the version (optional; the old one is bumped)"},
            },
            "required": ["name", "meta", "pycode"],
        },
    },
    {
        "name": "toolkit_reload",
        "description": ("Reload every tool function, and make the saved ones "
                        "usable. Reports the files that did not load and why."),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "toolkit_list_versions",
        "description": "List the versions of a tool that can be rolled back to.",
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string",
                                     "description": "the tool name"}},
            "required": ["name"],
        },
    },
    {
        "name": "toolkit_rollback",
        "description": "Roll a tool back to an earlier version.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "the tool name"},
                "version": {"type": "string", "description": "the version, e.g. 1.0.0"},
            },
            "required": ["name", "version"],
        },
    },
]


def send(message):
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def ok(msg_id, result):
    send({"jsonrpc": "2.0", "id": msg_id, "result": result})


def fail(msg_id, code, message):
    send({"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}})


def text_result(text, is_error=False):
    return {"content": [{"type": "text", "text": text}], "isError": bool(is_error)}


class Server:
    def __init__(self, module_dir=None, agent_url=""):
        self.toolkit = Toolkit(module_dir)
        self.agent_url = (agent_url or "").rstrip("/")

    # ---- the tools --------------------------------------------------------
    def schemas(self):
        """The meta tools plus everything the modules offer."""
        return META_TOOLS + self.toolkit.schemas()

    def call(self, name, arguments):
        arguments = arguments or {}
        if name == "toolkit_save":
            status, message = save_tool(name=arguments.get("name") or "",
                                        meta=arguments.get("meta"),
                                        pycode=arguments.get("pycode") or "",
                                        module_dir=self.toolkit.module_dir,
                                        version=str(arguments.get("version") or ""))
            if status == 0:
                # The file is on disk; the tool is usable after a reload.
                return text_result(message + ". Call toolkit_reload to use it.")
            return text_result(message, is_error=True)
        if name == "toolkit_reload":
            result = self.toolkit.reload()
            self.ask_agent_to_refresh()
            lines = ["reloaded: %d tool(s) available." % len(result["valid"])]
            for name in sorted(result["valid"]):
                lines.append("  - " + name)
            if result["invalid"]:
                lines.append("these files did not load:")
                for bad in result["invalid"]:
                    lines.append("  - %s: %s" % (bad.get("name") or "?", bad.get("reason")))
            return text_result("\n".join(lines))
        if name == "toolkit_list_versions":
            versions = list_versions(str(arguments.get("name") or ""),
                                     module_dir=self.toolkit.module_dir)
            if not versions:
                return text_result("%s has no earlier version." % arguments.get("name"))
            return text_result("%s: %s" % (arguments.get("name"), ", ".join(versions)))
        if name == "toolkit_rollback":
            status, message = rollback_tool(str(arguments.get("name") or ""),
                                            str(arguments.get("version") or ""),
                                            module_dir=self.toolkit.module_dir)
            if status == 0:
                self.toolkit.reload()
                self.ask_agent_to_refresh()
                return text_result(message)
            return text_result(message, is_error=True)
        # A tool from a module.
        try:
            return text_result(self.toolkit.call(name, arguments))
        except ToolError as exc:
            return text_result(str(exc), is_error=True)
        except Exception as exc:  # noqa: BLE001 - a broken tool is data, not a crash
            return text_result("%s: %s" % (type(exc).__name__, exc), is_error=True)

    # ---- the agent --------------------------------------------------------
    def ask_agent_to_refresh(self):
        """Ask a2 to re-read the tool list (in the background).

        The refresh restarts the MCP servers - including this one - so it
        must not happen while the answer is still on its way out. A short
        delay and a separate thread do that.
        """
        if not self.agent_url:
            return

        def worker():
            time.sleep(0.3)
            try:
                req = urllib.request.Request(
                    self.agent_url + "/api/mcp/refresh", data=b"{}",
                    headers={"Content-Type": "application/json"}, method="POST")
                urllib.request.urlopen(req, timeout=15).read()
            except (urllib.error.URLError, OSError, ValueError):
                pass       # the agent may be down; the reload still happened

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()


def main(argv=None):
    parser = argparse.ArgumentParser(description="a2's extension layer as an MCP server")
    parser.add_argument("--modules", default="", help="where the tool modules live")
    parser.add_argument("--agent-url", default=os.environ.get("TEA_AGENT_URL", ""),
                        help="the agent (so a reload can refresh its tool list)")
    args = parser.parse_args(argv)

    server = Server(module_dir=args.modules or None, agent_url=args.agent_url)

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
        if method == "initialize":
            ok(msg_id, {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": True}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            })
        elif method == "tools/list":
            ok(msg_id, {"tools": server.schemas()})
        elif method == "tools/call":
            params = message.get("params") or {}
            ok(msg_id, server.call(params.get("name") or "", params.get("arguments") or {}))
        elif method == "notifications/initialized":
            continue
        elif method is not None and msg_id is not None:
            fail(msg_id, -32601, "a2ext does not implement '%s'" % method)
    return 0


if __name__ == "__main__":
    sys.exit(main())
