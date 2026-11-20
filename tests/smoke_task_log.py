#!/usr/bin/env python3
"""The queue's log: repeats are throttled, the file is rotated by day.

一场设备上的事故换来的两件事（2026-11-20 从 172.16.1.31 同步回来的改动）：
队列每两秒问一次有没有活，「nothing queued」这种一模一样的行一天能写满
几 MB —— 日志读完要很久，真正的事件反而被埋掉。于是：

  * `log_throttled(key, message, interval)`：同一行（同一个 key）最多每
    `interval` 秒写一次；`interval=0` 关掉节流，`force=True` 一定写；
  * `LogRotator`：一天一个文件，只留最近几天。它**不改写自己的输出**：
    systemd 把 stdout 重定向进了这个文件，所以轮转是「拷贝 + 截断」，
    文件的 inode 不变，进程继续往同一个文件里写，不重启、不丢行。
    如果 stdout 指的不是这个文件，它一根手指都不碰（只警告一次）。

Usage: python3 tests/smoke_task_log.py
"""

import contextlib
import os
import sys
import tempfile
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCHED = os.path.join(REPO_ROOT, "sched")
if SCHED not in sys.path:
    sys.path.insert(0, SCHED)

import task_server as ts  # noqa: E402

TODAY = time.strftime("%Y-%m-%d")


def main():
    failures = []
    checks = []

    def check(name, ok, detail=""):
        checks.append(name)
        print(("PASS " if ok else "FAIL ") + name + (("  " + str(detail)) if not ok else ""))
        if not ok:
            failures.append(name)

    # ---- log_throttled ---------------------------------------------------
    sink = tempfile.TemporaryFile(mode="w+")
    with contextlib.redirect_stdout(sink):
        ts._LOG_THROTTLE.clear()
        ts._LOG_THROTTLE_LOCK = getattr(ts, "_LOG_LOCK", None)
        first = ts.log_throttled("same-line", "one", interval=60)
        second = ts.log_throttled("same-line", "two", interval=60)
        other = ts.log_throttled("other-line", "three", interval=60)
        off1 = ts.log_throttled("same-line", "four", interval=0)
        off2 = ts.log_throttled("same-line", "five", interval=0)
        forced = ts.log_throttled("same-line", "six", interval=60, force=True)
    sink.seek(0)
    printed = sink.read()
    check("the first line of a key is written", first is True)
    check("the same key is throttled inside the interval", second is False)
    check("a different key has its own throttle", other is True)
    check("interval=0 turns the throttle off", off1 is True and off2 is True)
    check("force writes the line anyway", forced is True)
    check("the throttled lines really are not printed",
          printed.count("one") == 1 and "two" not in printed and printed.count("five") == 1,
          repr(printed))

    # ---- the rotation: copy + truncate -----------------------------------
    folder = tempfile.mkdtemp(prefix="tea_log_")
    path = os.path.join(folder, "a2_task.log")
    live = open(path, "a+")
    live.write("2026-11-19  line one\n")
    live.flush()
    rot = ts.LogRotator(path, keep_days=5, interval=60)
    with contextlib.redirect_stdout(live):
        rotated = rot.rotate("2026-11-19")
    archive = path + ".2026-11-19"
    check("rotate archives the lines under their day", rotated is True and os.path.exists(archive))
    if os.path.exists(archive):
        with open(archive, encoding="utf-8") as f:
            check("the archive holds what the file held", "line one" in f.read())
    # 轮转之后马上会写一行「the log was rotated」，所以文件不是 0 字节：
    # 要验的是它不再是旧内容、而且**还是同一个 inode**（stdout 继续写得进去）。
    with open(path, encoding="utf-8") as f:
        live_text = f.read()
    check("the live file is emptied, not replaced",
          "line one" not in live_text and "rotated" in live_text
          and os.fstat(live.fileno()).st_ino == os.stat(path).st_ino,
          repr(live_text[:120]))

    # 同一天再轮转一次（服务重启过）：并进同一个归档，而不是覆盖
    live.write("2026-11-19  line two\n")
    live.flush()
    with contextlib.redirect_stdout(live):
        rot.rotate("2026-11-19")
    with open(archive, encoding="utf-8") as f:
        both = f.read()
    check("the same day is appended to the same archive",
          "line one" in both and "line two" in both, repr(both))

    # ---- 输出不是这个文件时：不碰 -----------------------------------------
    live.write("keep me\n")
    live.flush()
    elsewhere = tempfile.TemporaryFile(mode="w+")
    with contextlib.redirect_stdout(elsewhere):
        refused = rot.rotate("2026-11-19")
    check("a log file that is not stdout is left alone",
          refused is False and os.path.getsize(path) > 0)

    # ---- 只留最近几天 -----------------------------------------------------
    old = path + ".2000-01-01"
    with open(old, "w", encoding="utf-8") as f:
        f.write("ancient\n")
    today_file = path + "." + TODAY
    with open(today_file, "w", encoding="utf-8") as f:
        f.write("today\n")
    rot.prune()
    check("archives outside the window are deleted", not os.path.exists(old))
    check("today's archive stays", os.path.exists(today_file))

    # ---- tick：同一天不动，跨天要轮转 -------------------------------------
    with contextlib.redirect_stdout(live):
        same_day = rot.tick()
    check("tick does nothing while the day has not changed", same_day is False)

    stale = tempfile.mkdtemp(prefix="tea_log_stale_")
    stale_path = os.path.join(stale, "a2_task.log")
    with open(stale_path, "w", encoding="utf-8") as f:
        f.write("yesterday\n")
    three_days = time.time() - 3 * 86400
    os.utime(stale_path, (three_days, three_days))
    stale_rot = ts.LogRotator(stale_path, keep_days=5)
    stale_day = time.strftime("%Y-%m-%d", time.localtime(three_days))
    with open(stale_path, "a", encoding="utf-8") as handle:
        with contextlib.redirect_stdout(handle):
            stale_rot.tick()
    with open(stale_path, encoding="utf-8") as f:
        stale_live = f.read()
    check("tick archives a file that still holds an older day",
          os.path.exists(stale_path + "." + stale_day) and "yesterday" not in stale_live,
          stale_day)

    live.close()
    print("")
    print("%d/%d checks passed" % (len(checks) - len(failures), len(checks)))
    if failures:
        print("FAILED: " + ", ".join(failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
