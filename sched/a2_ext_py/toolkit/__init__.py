"""The toolkit: a2's mechanism layer can grow, at runtime.

The idea (borrowed from the Python tea_agent's `tlk.py`): **a tool is a file**.

    ~/.tea_agent/ext/toolkit_hello.py

        def toolkit_hello(name="world"):
            "Say hello."
            return "hello, " + name

        def meta_toolkit_hello():
            return {"name": "toolkit_hello",
                    "description": "Say hello.",
                    "inputSchema": {"type": "object",
                                    "properties": {"name": {"type": "string"}}}}

Two conventions and nothing else:

  * the file is named after the tool (`toolkit_<name>.py`);
  * it exposes the function `toolkit_<name>()` and its schema
    `meta_toolkit_<name>()`.

`reload()` scans the directory, runs every file in a restricted namespace and
collects the two. A file that does not fit is **reported, not skipped
silently** - "I set it and nothing happened" is the worst answer.

The modules live in the **user's** directory (`~/.tea_agent/ext/`), not next
to this framework: an upgrade must not eat them, and the model's file tools
cannot reach them (the workspace fence stops at the workspace).

The tools are handed to a2 over MCP (stdio), so the extension code runs in
*this* process, not in a2's: a bad module takes the extension server down,
not the agent. And because the call happens inside a2's loop, the turn's
budget, the trajectory and the stop semantics are a2's - unchanged.
"""

from .loader import Toolkit, ToolError
from .store import save_tool, list_versions, rollback_tool

__all__ = ["Toolkit", "ToolError", "save_tool", "list_versions", "rollback_tool"]
