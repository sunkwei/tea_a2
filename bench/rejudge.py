"""Judge the saved test logs again, with a stricter name matching.

The dataset's FAIL_TO_PASS / PASS_TO_PASS names are sometimes corrupted:
characters are scrambled ("TeestEnvVariable"), a name is truncated
("...ProfilePa") or a letter is doubled ("file_not_founnd"). The official
rule (exact match) then fails an instance that the gold patch does solve.

This script re-judges the logs that are already on disk (no test run) with:

  exact    - the official rule
  repaired - exact, plus a match when the names are the same up to a
             scramble / a small typo / a short truncation

    python .tea_agent_run/bench/rejudge.py
"""
import difflib
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, "bench"))
import sweb  # noqa: E402


def match(dataset_name, parsed_names):
    """The parsed test name that the dataset name means, or None."""
    if dataset_name in parsed_names:
        return dataset_name
    best_key, best_score = None, 0.0
    for key in parsed_names:
        # a short truncation: the dataset name is the beginning of the real one
        if len(dataset_name) >= 0.8 * len(key) and key.startswith(dataset_name):
            score = len(dataset_name) / float(len(key))
            if score > best_score:
                best_key, best_score = key, score
            continue
        # a scramble or a typo: same shape, similar letters
        if abs(len(key) - len(dataset_name)) <= 2:
            ratio = difflib.SequenceMatcher(None, dataset_name, key).ratio()
            if ratio >= 0.85 and ratio > best_score:
                best_key, best_score = key, ratio
    return best_key


def judge(inst, log_text):
    status = sweb.parse_log(inst, log_text)
    f2p = list(inst.get("FAIL_TO_PASS") or [])
    p2p = list(inst.get("PASS_TO_PASS") or [])
    out = {}
    for label, use_match in (("exact", False), ("repaired", True)):
        f2p_ok, f2p_bad, f2p_gone = [], [], []
        for name in f2p:
            key = name if not use_match else (match(name, status) or name)
            value = status.get(key)
            if value is None:
                f2p_gone.append(name)
            elif "pass" in str(value).lower():
                f2p_ok.append(name)
            else:
                f2p_bad.append(name)
        p2p_bad = []
        for name in p2p:
            key = name if not use_match else (match(name, status) or name)
            value = status.get(key)
            if value is not None and "fail" in str(value).lower():
                p2p_bad.append(name)
        out[label] = {
            "resolved": bool(not f2p_bad and not f2p_gone and not p2p_bad and f2p),
            "f2p_success": f2p_ok,
            "f2p_failure": f2p_bad,
            "f2p_missing": f2p_gone,
            "p2p_failure": p2p_bad,
        }
    out["status_counts"] = {
        "total": len(status),
        "pass": sum(1 for v in status.values() if "pass" in str(v).lower()),
        "fail": sum(1 for v in status.values() if "fail" in str(v).lower()),
    }
    return out


def main():
    rows = {r["instance_id"]: r for r in sweb.load_instances(sweb.DEFAULT_DATA)}
    print("%-42s %5s | %-24s | %-24s"
          % ("instance", "F2P", "gold exact/repaired", "agent exact/repaired"))
    result = {}
    for iid in sorted(os.listdir(sweb.WORK)):
        d = os.path.join(sweb.WORK, iid)
        line = {"gold": None, "agent": None}
        for kind in ("gold", "agent"):
            log = os.path.join(d, "out", kind, "print.log")
            if not os.path.exists(log):
                continue
            text = open(log, encoding="utf-8-sig", errors="replace").read()
            if not text.strip():
                continue
            line[kind] = judge(rows[iid], text)
            with open(os.path.join(d, "%s.rejudged.json" % kind), "w", encoding="utf-8") as fh:
                json.dump(line[kind], fh, ensure_ascii=False, indent=2)
        if not line["gold"] and not line["agent"]:
            continue
        result[iid] = line
        f2p = len(rows[iid].get("FAIL_TO_PASS") or [])

        def side(x):
            if not x:
                return "-"
            return "%-5s %-5s" % (x["exact"]["resolved"], x["repaired"]["resolved"])
        print("%-42s %5d | %-24s | %-24s"
              % (iid, f2p, side(line["gold"]), side(line["agent"])))
    valid = [i for i, v in result.items()
             if v["gold"] and v["gold"]["repaired"]["resolved"]]
    exact_valid = [i for i, v in result.items()
                   if v["gold"] and v["gold"]["exact"]["resolved"]]
    agent_ok = [i for i, v in result.items()
                if v["agent"] and v["agent"]["repaired"]["resolved"]]
    hits = [i for i in agent_ok if i in valid]
    print()
    print("measured instances              : %d" % len(result))
    print("gold resolves, exact names      : %d  %s" % (len(exact_valid), exact_valid))
    print("gold resolves, names repaired   : %d  %s" % (len(valid), valid))
    print("agent resolves, names repaired  : %d  %s" % (len(agent_ok), agent_ok))
    print("agent on the valid subset       : %d / %d" % (len(hits), len(valid)))
    with open(os.path.join(sweb.BENCH, "rejudge.json"), "w", encoding="utf-8") as fh:
        json.dump({"instances": result, "valid": valid, "agent_ok": agent_ok}, fh,
                  ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
