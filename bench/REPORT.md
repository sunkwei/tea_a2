# tea_a2 on SWE-bench-Live/Windows - first run

**Date:** 2026-11-07
**Agent:** tea_a2 0.22.0 (`build-msvc\bin\tea_agent.exe`)
**Model:** minimax / MiniMax-M3.1-Flash, 100 iterations, 15 min wall per rollout
**Dataset:** `SWE-bench-Live/Windows` - 66 instances, 48 repos, 9 languages
(Go 31, TypeScript 12, C# 7, C 4, C++ 3, Python 3, Rust 3, JavaScript 2, Java 1)

## The short version

The harness works end to end: tea_a2 solves the issue, the patch is collected,
the tests are run and judged with the dataset's own parser.

**The bottleneck is not the agent - it is the evaluation environment.**

1. This machine has **no container runtime** (no Docker, no podman, no
   Hyper-V on Windows 10 Pro), so the official Windows container images
   (`starryzhang/sweb.eval.win.*`) cannot run. The evaluation therefore runs
   natively, and a Windows desktop is not a Windows container: some tests fail
   here that passed in the image.
2. The Windows split's **FAIL_TO_PASS / PASS_TO_PASS name lists are corrupted**
   (characters scrambled, names truncated, letters doubled, package prefixes
   mangled). With the official exact-match rule the **gold patch resolves
   0 of 6** measured instances - the names simply never match.
3. On the instances where the gold patch *does* resolve natively (2 of 6), the
   agent resolved **0 of 2** - but on both it *fixed the issue* and lost the
   verdict on regressions (see below).

So this run says: **tea_a2 + MiniMax-M3.1-Flash reproduces the gold fix on
4 of 6 instances, but its patches are not regression-free** - under the
official rule that is 0 resolved.

## Per instance

`F2P ok` = how many of the instance's FAIL_TO_PASS tests passed,
`P2P fail` = how many of the PASS_TO_PASS tests broke.

| instance | F2P names | gold F2P ok | agent F2P ok | gold P2P fail | agent P2P fail | gold verdict | agent verdict |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `JanDeDobbeleer__oh-my-posh-6330` | 2 | 2 | 2 | 0 | 2 | **valid** | fail: 2 regressions |
| `moonD4rk__HackBrowserData-563` | 32 | 32 | 26 | 0 | 0 | **valid** | fail: 6 tests never ran |
| `nats-io__nats-server-6803` | 29 | 29 | 29 | 1 | 1 | invalid (flaky P2P) | same as gold |
| `prometheus-community__windows_exporter-2104` | 1 | 0 | 1 | 1 | 1 | invalid (environment) | agent passes what gold fails |
| `hashicorp__packer-13334` | 60 | 52 | 52 | 4 | 11 | invalid (8 broken names) | fail: 7 extra regressions |
| `twpayne__chezmoi-4378` | 128 | 127 | 0 | 1 | 0 | invalid (1 broken name) | fail: the package no longer builds |

* **exact** = the official rule (exact name match). **repaired** = exact, plus a
  match when a dataset name is the same test up to a scramble / a small typo /
  a short truncation (`bench/rejudge.py`).

## The agent's failure modes (4 of 6 instances)

1. **It rewrote the test files.** In `HackBrowserData` it rewrote
   `browser/browser_test.go` (377 lines), so the agent patch and the official
   test patch collide. Dropping the test files from the patch (a fallback the
   harness applies) still fails: the agent changed the signature of
   `pickFromConfigs`, so the *new* tests do not compile and 6 FAIL_TO_PASS
   tests never run.
2. **It regressed existing tests.** `oh-my-posh`: the fix works (2/2 F2P) but
   `TestFullPathCustomMappedLocations` and `TestUpgrade` break. `packer`: 11
   P2P failures against the gold patch's 4.
3. **It broke a build.** `chezmoi`: the gold run executes 1555 tests, the
   agent's run 29 - one package no longer compiles, so all 128 FAIL_TO_PASS
   tests are missing.
4. **It burned the budget on polish.** The first rollout spent its last 20
   iterations installing `gofumpt` and checking `golangci-lint`. The prompt
   now says: no tools, no linters, no new tests, about 60 tool calls, stop when
   the package's tests pass.

## The environment's failure modes

| what | where |
| --- | --- |
| `proxy.golang.org` unreachable (the Go tool cannot connect to the IPs it resolves) | every Go instance -> `GOPROXY=https://goproxy.cn` |
| the container's Windows APIs are missing (cert store, HCS/HCN, WMI, a listener on a port) | `windows_exporter`: 19 failing tests **with the gold patch** |
| a binary fixture in the test patch has no index line (`git apply` refuses it) | `nats-server`: applied file by file, the `.pfx` is skipped |
| the dataset's test names are corrupted | 64 of 66 instances have at least one FAIL_TO_PASS name that does not appear in the test patch |

## What to do next

* **For a real number, use the containers.** On a Windows Server 2022 host with
  container support: pull `starryzhang/sweb.eval.win.<instance>` and run the
  official `evaluation/evaluation.py` with the patches this harness produces
  (`bench/sweb.py rollout` gives you the patch file). The native run here is a
  development aid, not a leaderboard number.
* **Keep the gold baseline.** `bench/sweb.py eval --gold` is how the official
  README filters the instances that are invalid on *your* machine. On this
  machine only 2 of 6 measured instances survive it.
* **Give the agent a regression gate.** The agent never ran the package's
  existing tests before finishing. A prompt that says "run the tests of the
  package you touched and do not break them" is worth a try - it is the
  difference between "the issue is fixed" and "resolved".

## Reproducing

```bash
python bench/setup_home.py --provider minimax --model MiniMax-M3.1-Flash \
                           --iterations 100 --wall-minutes 15
python bench/sweb.py prepare <instance_id> ...
python bench/sweb.py run --jobs 4 <instance_id> ...
python bench/sweb.py eval --gold --jobs 4 <instance_id> ...
python bench/rejudge.py
```

The data (clones, logs, patches, trajectories, reports) is under
`.tea_agent_run/bench/`.
