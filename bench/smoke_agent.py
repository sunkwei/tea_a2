#!/usr/bin/env python3
"""Smoke test: does the bench tea home answer a turn at all?

    python bench/smoke_agent.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sweb  # noqa: E402

WORKDIR = os.path.join(sweb.BENCH, "smoke-ws")


def main():
    os.makedirs(WORKDIR, exist_ok=True)
    port = sweb.free_port()
    log = os.path.join(sweb.LOGDIR, "smoke.agent.log")
    proc, health = sweb.start_agent(sweb.DEFAULT_AGENT, WORKDIR, port, log, os.environ.copy())
    print("health:", {k: health.get(k) for k in ("ok", "version", "provider", "model",
                                                "tea_home_writable", "config_loaded")})
    print("limits:", health.get("limits"))
    try:
        wsid = sweb.workspace_id(port, WORKDIR)
        print("workspace:", wsid)
        answer = sweb.http("http://127.0.0.1:%d/api/chat" % port,
                           {"workspace_id": wsid,
                            "message": "Run `git --version` with the shell tool and then "
                                       "answer with exactly the version string.",
                            "async": True}, timeout=60)
        turn = answer.get("turn_id")
        print("turn:", turn)
        for _ in range(60):
            time.sleep(3)
            info = sweb.http("http://127.0.0.1:%d/api/turns/%s" % (port, turn), timeout=30)
            if info.get("state") != "running":
                break
        print("state  :", info.get("state"))
        print("result :", (info.get("result") or "")[:400])
        print("usage  :", info.get("usage"))
    finally:
        sweb.stop_agent(proc)


if __name__ == "__main__":
    main()
