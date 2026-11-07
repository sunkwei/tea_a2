"""The loader: find the modules, run them, keep their tools.

A module is a file that follows the convention (see the package doc). The
loader runs each file in a restricted namespace and picks up two names out of
it: the function and its schema.

What it refuses to do quietly:

  * a file whose name does not start with `toolkit_` is not a tool (skipped,
    and said so);
  * a file without a callable function or a callable `meta_` is **invalid** -
    it is reported with the reason, not dropped;
  * a file that raises while loading is invalid, with the error text;
  * the order is sorted, so the tool list is the same on every start (the
    order is part of the prompt cache).
"""

import os
import os.path as osp

TOOL_PREFIX = "toolkit_"


class ToolError(Exception):
    """A tool could not be loaded or could not be called."""


class Toolkit:
    """The loaded tools: name -> (function, schema)."""

    def __init__(self, module_dir=None):
        self.module_dir = osp.abspath(os.path.expanduser(
            module_dir or os.environ.get("TEA_EXT_DIR")
            or osp.join("~", ".tea_agent", "ext")))
        os.makedirs(self.module_dir, exist_ok=True)
        self.funcs = {}       # name -> callable
        self.metas = {}       # name -> schema (MCP shape)
        self.invalid = []     # [{"name":…, "reason":…}]
        self.user_tools = set()
        self.reload()

    # ---- loading ----------------------------------------------------------
    def reload(self):
        """Load every module again. Returns what happened.

        The result is honest: `valid` says which tools are there now, and
        `invalid` says which files did not make it and why.
        """
        funcs = {}
        metas = {}
        invalid = []
        user_tools = set()

        try:
            names = sorted(os.listdir(self.module_dir))
        except OSError as exc:
            return {"valid": {}, "invalid": [{"name": "", "reason": str(exc)}]}

        for filename in names:
            if not filename.endswith(".py"):
                continue
            if not filename.startswith(TOOL_PREFIX):
                continue
            name = filename[:-3]
            path = osp.join(self.module_dir, filename)
            try:
                with open(path, encoding="utf-8") as f:
                    code = f.read()
            except OSError as exc:
                invalid.append({"name": name, "reason": "cannot read: %s" % exc})
                continue

            # A restricted namespace: the module gets the builtins and
            # nothing else of ours. It may import what it needs itself.
            #
            # Globals and locals are the *same* dict on purpose. With two
            # dicts, a module level import lands in the locals - and neither a
            # function (its __globals__ is the globals dict) nor a class body
            # can see it there. So "a module may import what it needs" did
            # not work: every module died with "NameError: name 'os' is not
            # defined".
            namespace = {"__builtins__": __builtins__}
            local = namespace
            try:
                exec(compile(code, path, "exec"), namespace, namespace)  # noqa: S102
            except Exception as exc:  # noqa: BLE001 - a broken module is data, not a crash
                invalid.append({"name": name, "reason": "%s: %s" % (type(exc).__name__, exc)})
                continue

            func = local.get(name)
            meta_func = local.get("meta_" + name)
            if not callable(func):
                invalid.append({"name": name, "reason": "%s is not callable" % name})
                continue
            if not callable(meta_func):
                invalid.append({"name": name, "reason": "meta_%s is missing" % name})
                continue
            try:
                meta = meta_func()
            except Exception as exc:  # noqa: BLE001
                invalid.append({"name": name, "reason": "meta_%s failed: %s" % (name, exc)})
                continue
            if not isinstance(meta, dict) or not meta.get("name"):
                invalid.append({"name": name, "reason": "meta_%s has no name" % name})
                continue

            funcs[name] = func
            metas[name] = meta
            user_tools.add(name)

        self.funcs = funcs
        self.metas = metas
        self.invalid = invalid
        self.user_tools = user_tools
        return {"valid": {k: metas[k] for k in sorted(metas)}, "invalid": invalid}

    # ---- the tools --------------------------------------------------------
    def schemas(self):
        """Every tool's schema, in a stable order."""
        return [self.metas[name] for name in sorted(self.metas)]

    def call(self, name, arguments):
        """Call one tool. Returns its result as text.

        The arguments are checked against the function's signature before the
        call, and the error names the legal parameters - a model that passed
        the wrong keyword must be able to correct itself in one try.
        """
        if name not in self.funcs:
            raise ToolError("there is no tool called %r" % name)
        func = self.funcs[name]
        args = dict(arguments or {})
        try:
            import inspect
            signature = inspect.signature(func)
        except (TypeError, ValueError):
            signature = None
        if signature is not None:
            try:
                signature.bind(**args)
            except TypeError as exc:
                names = [n for n, p in signature.parameters.items()
                         if p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)]
                unknown = sorted(set(args) - set(names))
                hints = ["call %s failed: %s" % (name, exc)]
                if unknown:
                    hints.append("unsupported arguments %s; this tool takes: %s"
                                 % (unknown, ", ".join(names) or "(none)"))
                raise ToolError(" | ".join(hints)) from None
        result = func(**args)
        return result if isinstance(result, str) else repr(result)
