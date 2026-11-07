"""How healthy is the dataset's FAIL_TO_PASS list?

The new tests live in the test_patch, so the test name (the last component
of a FAIL_TO_PASS name) has to appear there. A name whose test part does not
appear in the patch is corrupted - the Windows split has scrambled names
("TeestEnvVariable"), doubled letters ("file_not_founnd"), truncated names
("...ProfilePa") and mangled package prefixes ("github.coom/...").

    python bench/check_names.py
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "bench"))
import sweb  # noqa: E402

rows = sweb.load_instances(sweb.DEFAULT_DATA)

print("%-42s %5s %5s  %s" % ("instance", "F2P", "bad", "first broken names"))
broken_instances = []
total = 0
bad_total = 0
for inst in sorted(rows, key=lambda r: r["instance_id"]):
    patch = inst.get("test_patch") or ""
    f2p = inst.get("FAIL_TO_PASS") or []
    bad = []
    for name in f2p:
        tail = re.split(r"[./\\\\]", name.replace("\\\\", "/"))[-1]
        if not tail or len(tail) < 4:
            continue
        if tail not in patch:
            bad.append(name)
    total += len(f2p)
    bad_total += len(bad)
    if bad:
        broken_instances.append(inst["instance_id"])
    print("%-42s %5d %5d  %s"
          % (inst["instance_id"], len(f2p), len(bad), ", ".join(b[:38] for b in bad[:2])))
print()
print("instances with a corrupted F2P name : %d / %d" % (len(broken_instances), len(rows)))
print("corrupted F2P names                  : %d / %d" % (bad_total, total))
