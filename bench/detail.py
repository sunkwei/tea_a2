"""One table for the report: per instance, gold vs agent."""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, "bench"))
import sweb  # noqa: E402

rows = {r["instance_id"]: r for r in sweb.load_instances(sweb.DEFAULT_DATA)}
data = json.load(open(os.path.join(sweb.BENCH, "rejudge.json"), encoding="utf-8"))

for iid, v in sorted(data["instances"].items()):
    inst = rows[iid]
    f2p = inst.get("FAIL_TO_PASS") or []
    print("=" * 78)
    print("%s  (%s, %s)" % (iid, inst["repo"], inst["language"]))
    print("  F2P names: %d, P2P names: %d" % (len(f2p), len(inst.get("PASS_TO_PASS") or [])))
    for kind in ("gold", "agent"):
        r = v.get(kind)
        if not r:
            print("  %-5s : (no log)" % kind)
            continue
        print("  %-5s : exact=%-5s repaired=%-5s  tests pass/fail/total=%d/%d/%d"
              % (kind, r["exact"]["resolved"], r["repaired"]["resolved"],
                 r["status_counts"]["pass"], r["status_counts"]["fail"],
                 r["status_counts"]["total"]))
        print("          f2p ok=%d fail=%d missing=%d, p2p failed=%d"
              % (len(r["repaired"]["f2p_success"]), len(r["repaired"]["f2p_failure"]),
                 len(r["repaired"]["f2p_missing"]), len(r["repaired"]["p2p_failure"])))
        if r["repaired"]["f2p_failure"]:
            print("          f2p failed : %s" % ", ".join(r["repaired"]["f2p_failure"][:4]))
        if r["repaired"]["f2p_missing"]:
            print("          f2p gone   : %s" % ", ".join(r["repaired"]["f2p_missing"][:4]))
        if r["repaired"]["p2p_failure"]:
            print("          p2p failed : %s" % ", ".join(r["repaired"]["p2p_failure"][:4]))
