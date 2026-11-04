"""The write side: save a tool, roll it back, list its versions.

This is the "self-evolution" part, and it is the part that runs code, so it
is deliberately strict. Before a line is written:

  * the name must be `toolkit_<something>` (a convention, and a cheap way to
    keep the namespace clean);
  * the schema must be a schema, and its name must be the tool's name;
  * the code must **parse** (`ast.parse`) - a syntax error is caught here,
    not at the next reload;
  * the code must actually define that function;
  * the missing third-party imports are reported (nothing is installed
    behind the user's back in server mode).

Every save keeps the previous version (`<name>.v<old>.bak.py`), so a tool
that turns out to be broken can be rolled back - and the versions are how
"it has been working for three months" becomes visible.
"""

import ast
import json
import os
import os.path as osp
import re
import shutil
import time

TOOL_PREFIX = "toolkit_"
VERSION_RE = re.compile(r"^#\s*version:\s*([0-9.]+)\s*$", re.M)


def default_module_dir():
    return osp.abspath(os.path.expanduser(
        os.environ.get("TEA_EXT_DIR") or osp.join("~", ".tea_agent", "ext")))


def audit_path():
    """Where the saves are recorded (outside the module directory)."""
    home = osp.expanduser("~")
    return osp.join(home, ".tea_agent", "ext-audit.jsonl")


def _audit(entry):
    """One line in the audit log: who saved what, when, and the code."""
    try:
        path = audit_path()
        os.makedirs(osp.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass       # the audit must never break a save


def normalize_meta(name, meta):
    """The schema in the MCP shape, from either the MCP or the OpenAI shape.

    The model writes what it knows: an OpenAI function schema is the common
    case, and it is accepted here so the caller does not have to translate.
    """
    if not isinstance(meta, dict):
        return None, "meta must be an object"
    if meta.get("type") == "function" and isinstance(meta.get("function"), dict):
        meta = meta["function"]
    if not meta.get("name"):
        return None, "meta.name is required"
    if meta["name"] != name:
        return None, "meta.name %r does not match the tool name %r" % (meta["name"], name)
    schema = {
        "name": meta["name"],
        "description": str(meta.get("description") or ""),
        "inputSchema": meta.get("inputSchema") or meta.get("parameters") or
                      {"type": "object", "properties": {}},
    }
    if not isinstance(schema["inputSchema"], dict):
        return None, "meta.inputSchema must be an object"
    schema["inputSchema"].setdefault("type", "object")
    schema["inputSchema"].setdefault("properties", {})
    return schema, ""


def missing_dependencies(pycode):
    """The third-party modules the code needs but cannot find."""
    try:
        tree = ast.parse(pycode)
    except SyntaxError:
        return []
    wanted = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                wanted.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            wanted.add(node.module.split(".")[0])
    import importlib.util
    import sys
    stdlib = getattr(sys, "stdlib_module_names", set())
    missing = []
    for name in sorted(wanted):
        if name in stdlib or name in sys.modules:
            continue
        try:
            if importlib.util.find_spec(name) is None:
                missing.append(name)
        except (ImportError, ValueError):
            missing.append(name)
    return missing


def save_tool(name, meta, pycode, module_dir=None, version=""):
    """Write the tool. Returns (status, message); 0 means it worked.

    The status codes: 0 ok, 2 the name or the schema is wrong, 3 the code is
    wrong, 4 the file could not be written.
    """
    if not isinstance(name, str) or not name:
        return 2, "the tool needs a name"
    if not name.startswith(TOOL_PREFIX):
        return 2, ("the tool name must start with %r (it is the namespace of "
                   "the extension layer)" % TOOL_PREFIX)
    if not name.isidentifier():
        return 2, "the tool name must be a plain identifier"
    if not isinstance(pycode, str) or not pycode.strip():
        return 3, "the tool needs code"

    schema, err = normalize_meta(name, meta)
    if schema is None:
        return 2, err

    try:
        tree = ast.parse(pycode)
    except SyntaxError as exc:
        return 3, "the code does not parse: %s" % exc

    defines = [node for node in ast.walk(tree)
               if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
               and node.name == name]
    if not defines:
        return 3, "the code does not define %s()" % name

    module_dir = osp.abspath(os.path.expanduser(module_dir or default_module_dir()))
    try:
        os.makedirs(module_dir, exist_ok=True)
    except OSError as exc:
        return 4, "cannot create %s: %s" % (module_dir, exc)

    filename = osp.join(module_dir, name + ".py")
    old_version = ""
    old_content = ""
    if osp.exists(filename):
        try:
            with open(filename, encoding="utf-8") as f:
                old_content = f.read()
        except OSError:
            old_content = ""
        match = VERSION_RE.search(old_content)
        if match:
            old_version = match.group(1)

    if not version:
        if old_version:
            parts = old_version.split(".")
            try:
                parts[-1] = str(int(parts[-1]) + 1)
            except ValueError:
                parts.append("1")
            version = ".".join(parts)
        else:
            version = "1.0.0"

    # The old version is kept: a tool that breaks can be rolled back.
    if old_content and old_version:
        backup = osp.join(module_dir, "%s.v%s.bak.py" % (name, old_version))
        try:
            with open(backup, "w", encoding="utf-8") as f:
                f.write(old_content)
        except OSError:
            pass

    body = [
        "## a2 extension tool, saved %s" % time.asctime(),
        "# version: %s" % version,
        "",
        pycode.rstrip(),
        "",
        "",
        "def meta_%s() -> dict:" % name,
        "    return " + json.dumps(schema, ensure_ascii=False),
        "",
    ]
    try:
        with open(filename, "w", encoding="utf-8") as f:
            f.write("\n".join(body))
    except OSError as exc:
        return 4, "cannot write %s: %s" % (filename, exc)

    missing = missing_dependencies(pycode)
    _audit({"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "tool": name, "version": version, "path": filename,
            "missing_dependencies": missing,
            "code": pycode})
    message = "saved %s (v%s)" % (name, version)
    if missing:
        message += "; missing dependencies: %s" % ", ".join(missing)
    return 0, message


def list_versions(name, module_dir=None):
    """The versions of a tool that can be rolled back to."""
    module_dir = osp.abspath(os.path.expanduser(module_dir or default_module_dir()))
    versions = []
    try:
        files = os.listdir(module_dir)
    except OSError:
        return versions
    for filename in files:
        if filename.startswith(name + ".v") and filename.endswith(".bak.py"):
            versions.append(filename[len(name) + 2:-len(".bak.py")])
    versions.sort(key=lambda v: [int(x) for x in v.split(".")] if v.split(".")[0].isdigit() else [0])
    return versions


def rollback_tool(name, version, module_dir=None):
    """Put an older version back. Returns (status, message)."""
    module_dir = osp.abspath(os.path.expanduser(module_dir or default_module_dir()))
    backup = osp.join(module_dir, "%s.v%s.bak.py" % (name, version))
    if not osp.exists(backup):
        return 1, "there is no version %s of %s" % (version, name)
    current = osp.join(module_dir, name + ".py")
    if osp.exists(current):
        try:
            shutil.copy2(current, osp.join(module_dir, "%s.current.bak.py" % name))
        except OSError:
            pass
    try:
        shutil.copy2(backup, current)
    except OSError as exc:
        return 4, "cannot restore %s: %s" % (name, exc)
    return 0, "rolled %s back to v%s" % (name, version)
