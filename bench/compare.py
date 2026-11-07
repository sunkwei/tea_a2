"""Compare the gold patch with the agent patch, instance by instance.

    python .tea_agent_run/bench/compare.py
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, "bench"))
import sweb  # noqa: E402

rows = {r["instance_id"]: r for r in sweb.load_instances(sweb.DEFAULT_DATA)}


def read(path):
    try:
        return json.load(open(path, encoding="utf-8"))
    except Exception:
        return None


def cnt(report, kind, key):
    return len(((report or {}).get(kind) or {}).get(key) or [])


print("%-42s %5s | %-28s | %-28s"
      % ("instance", "F2P", "gold  ok/f2p/p2p/repaired", "agent ok/f2p/p2p/repaired"))
summary = []
for iid in sorted(os.listdir(sweb.WORK) if os.path.isdir(sweb.WORK) else []):
    d = os.path.join(sweb.WORK, iid)
    agent = read(os.path.join(d, "report.json"))
    gold = read(os.path.join(d, "gold.json"))
    if not agent and not gold:
        continue
    inst = rows.get(iid, {})
    f2p = len(inst.get("FAIL_TO_PASS") or [])

    def side(r):
        if not r:
            return "-"
        return "%-5s %-5s %-5s %-5s" % (
            r.get("resolved"),
            cnt(r, None, "f2p_success") if False else len(r.get("f2p_success") or []),
            len(r.get("p2p_failure") or []),
            (r.get("repaired") or {}).get("resolved"),
        )
    print("%-42s %5d | %-28s | %-28s" % (iid, f2p, side(gold), side(agent)))
    summary.append((iid, agent, gold))

print()
valid = [i for i, a, g in summary if g and (g.get("repaired") or {}).get("resolved")]
valid_exact = [i for i, a, g in summary if g and g.get("resolved")]
agent_ok = [i for i, a, g in summary if a and (a.get("repaired") or {}).get("resolved")]
print("instances measured            : %d" % len(summary))
print("gold resolves (exact names)   : %d  %s" % (len(valid_exact), valid_exact))
print("gold resolves (names repaired): %d  %s" % (len(valid), valid))
print("agent resolves (repaired)     : %d  %s" % (len(agent_ok), agent_ok))
if valid:
    hits = [i for i in agent_ok if i in valid]
    print("agent on the valid subset     : %d / %d" % (len(hits), len(valid)))
