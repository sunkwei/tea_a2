#!/usr/bin/env python3
"""Build the tea home the bench agents use.

It copies ONE provider out of the user's own provider.yaml into
`.tea_agent_run/bench/home/provider.yaml` (that directory is gitignored) and
writes the machine settings next to it.

The web tools (`supports_search` / `supports_fetch`) are stripped on purpose:
the SWE-bench protocol forbids looking the upstream fix up on the internet,
so the agent must not have a search tool at all.

    python bench/setup_home.py --provider minimax --model MiniMax-M3.1-Flash
"""
import argparse
import os

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOME = os.path.join(ROOT, ".tea_agent_run", "bench", "home")
SRC = os.path.expanduser("~/.tea_agent/provider.yaml")

SETTINGS = {
    "max_iterations": 100,
    "max_wall_ms": 45 * 60 * 1000,
    "max_tool_calls": 0,
    "max_total_tokens": 0,
    "max_tool_output": 131072,
    "keep_turns": 10,
    "command_timeout_ms": 600000,
    "stream": True,
    "enable_compaction": True,
    "compaction_keep_recent": 4,
    "tool_output_max_lines": 400,
    "web_tool_max_calls_per_turn": 0,
    "overload_retries": 3,
    "overload_backoff_ms": 60000,
    "overload_max_total_ms": 600000,
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default="minimax")
    ap.add_argument("--model", default=None)
    ap.add_argument("--iterations", type=int, default=100)
    ap.add_argument("--wall-minutes", type=int, default=45)
    args = ap.parse_args()

    cfg = yaml.safe_load(open(SRC, encoding="utf-8"))
    providers = cfg.get("providers") or {}
    if args.provider not in providers:
        raise SystemExit("provider %s not in %s" % (args.provider, sorted(providers)))
    pconf = dict(providers[args.provider])
    # the agent must not be able to look the fix up on the internet
    pconf.pop("supports_search", None)
    pconf.pop("supports_fetch", None)
    models = pconf.get("models") or {}
    if args.model:
        if args.model not in models:
            raise SystemExit("model %s not in %s" % (args.model, sorted(models)))
        chosen = args.model
    else:
        chosen = next(iter(models))
    # the model that is used is the first one in the list
    pconf["models"] = {chosen: models[chosen]}
    pconf.pop("api_keys", None)

    os.makedirs(HOME, exist_ok=True)
    out = {"providers": {args.provider: pconf}}
    with open(os.path.join(HOME, "provider.yaml"), "w", encoding="utf-8") as fh:
        yaml.safe_dump(out, fh, allow_unicode=True, sort_keys=False)

    settings = dict(SETTINGS)
    settings["max_iterations"] = args.iterations
    settings["max_wall_ms"] = args.wall_minutes * 60 * 1000
    with open(os.path.join(HOME, "tea_agent.yaml"), "w", encoding="utf-8") as fh:
        yaml.safe_dump({"settings": settings}, fh, allow_unicode=True, sort_keys=False)

    print("provider : %s" % args.provider)
    print("model    : %s" % chosen)
    print("tea home : %s" % HOME)


if __name__ == "__main__":
    main()
