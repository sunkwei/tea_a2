# tea_a2 on SWE-bench-Live/Windows

A harness that runs **tea_a2** on the
[SWE-bench-Live/Windows](https://huggingface.co/datasets/SWE-bench-Live/Windows)
split and judges the patches with the dataset's own `log_parser`.

The official protocol (microsoft/SWE-bench-Live) evaluates every instance in a
**Windows container** (`starryzhang/sweb.eval.win.*`). A machine without a
container runtime cannot do that - this harness then does the same thing
**natively**: it clones the repository at `base_commit`, lets tea_a2 work in it,
collects `git diff HEAD` as the patch, applies `test_patch` + the agent patch in
a second work tree, runs the instance's own `rebuild_cmds` / `test_cmds` /
`print_cmds`, parses the log with the instance's `log_parser` and judges
FAIL_TO_PASS / PASS_TO_PASS with the official rule.

## Files

| file | what it does |
| --- | --- |
| `sweb.py` | the harness: `prepare` / `rollout` / `eval` / `run` / `report` |
| `setup_home.py` | builds the tea home the bench agents use (one provider, no web tools) |
| `smoke_agent.py` | checks that the bench agent answers a turn at all |
| `rejudge.py` | re-judges the saved logs with a repaired name matching |
| `compare.py`, `detail.py` | gold vs agent, per instance |
| `check_names.py` | how healthy the dataset's FAIL_TO_PASS names are |

Everything the run produces (clones, logs, patches, reports) lands in
`.tea_agent_run/bench/` - that directory is gitignored.

## Usage

```bash
# 0) the dataset (once)
python -c "import pandas; pandas.read_parquet(...)"   # see REPORT.md
#    -> .tea_agent_run/bench/instances.jsonl

# 1) the bench tea home: one provider out of your own provider.yaml
python bench/setup_home.py --provider minimax --model MiniMax-M3.1-Flash \
                           --iterations 100 --wall-minutes 15

# 2) one instance, end to end
python bench/sweb.py run moonD4rk__HackBrowserData-563

# 3) the same with the dataset's own patch (the reference run)
python bench/sweb.py eval --gold moonD4rk__HackBrowserData-563

# 4) a batch
python bench/sweb.py run --jobs 4 <instance_id> ...

# 5) the tables
python bench/sweb.py report
python bench/rejudge.py
```

`run` is resumable: a rollout that already produced a patch is not repeated
(`--force` makes it repeat).

## What the agent may do

The prompt is the protocol's: the issue text plus general working instructions.
The bench tea home has **no web tools** (`supports_search` / `supports_fetch`
are stripped), so the agent cannot look the upstream fix up, and the repository
is a single-commit shallow clone, so there is no later commit to find.

## Notes

* `GOPROXY=https://goproxy.cn` - `proxy.golang.org` is unreachable from this
  network (the Go tool cannot connect to the IPs it resolves).
* The caches (`npm`, `go`, `dotnet`, `pip`) are redirected into the work tree,
  so the run does not depend on a writable user profile.
* `/testbed` and `C:\testbed` in the dataset's commands are rewritten to the
  work tree; `C:\t` and `C:\gsd-test-tmp` become the instance's temp dir.
