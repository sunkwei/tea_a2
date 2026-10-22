#!/usr/bin/env bash
# scripts/test-all.sh -- the whole suite, in parallel, with ONE browser.
#
#   scripts/test-all.sh                  # everything
#   scripts/test-all.sh ui               # only the browser tests
#   scripts/test-all.sh smoke_todo       # one test (substring match)
#   JOBS=4 scripts/test-all.sh           # fewer parallel jobs
#
# Why a script:
#
#   * the browser tests are the slow half: a fresh Chrome pays a ~25 s penalty
#     on its first page load in some Linux containers (WSL2). One browser for
#     the whole run pays it once; the tests run in parallel on top of that.
#     The Windows runner (scripts/test-all.ps1) does the same thing with a
#     browser of its own;
#   * one entry point for humans and for agents, with a summary at the end.
#
# Exit code 0 when everything passed, 1 otherwise.

set -u
root=$(cd "$(dirname "$0")/.." && pwd)
cd "$root"

JOBS=${JOBS:-8}
ONLY=${1:-}
EXE=${EXE:-}
CHROME=${CHROME_BIN:-}

if [ -z "$EXE" ]; then
    for candidate in build/bin/tea_agent build-static/bin/tea_agent; do
        if [ -x "$candidate" ]; then EXE="$candidate"; break; fi
    done
fi
if [ ! -x "$EXE" ]; then
    echo "no binary found; build first (cmake --build build -j8)" >&2
    exit 2
fi
if [ -z "$CHROME" ]; then
    for candidate in "$HOME/.cache/ms-playwright/chromium-1223/chrome-linux64/chrome" \
                     /usr/bin/google-chrome /usr/bin/chromium /usr/bin/chromium-browser; do
        if [ -x "$candidate" ]; then CHROME="$candidate"; break; fi
    done
fi

WORK=$(mktemp -d "${TMPDIR:-/tmp}/tea-suite-XXXXXX")
LOGDIR="$WORK/logs"
RESULTS="$WORK/results.tsv"
mkdir -p "$LOGDIR"
: > "$RESULTS"

cleanup() {
    if [ -n "${CHROME_PID:-}" ]; then kill "$CHROME_PID" 2>/dev/null; fi
}
trap cleanup EXIT INT TERM

# ---- the tests to run --------------------------------------------------------
LIST=""
add() { case "$1" in *"$ONLY"*) LIST="$LIST $1";; esac; }
for f in tests/smoke_*.py; do [ -e "$f" ] && add "$f"; done
UI_LIST=""
if [ -n "$CHROME" ]; then
    for f in tests/ui_*.mjs; do [ -e "$f" ] && UI_LIST="$UI_LIST $f"; done
else
    echo "no chrome found: the browser tests are skipped" >&2
fi
LIST="$LIST $UI_LIST"
if [ -z "$(echo $LIST)" ]; then
    echo "nothing to run (filter: '$ONLY')" >&2
    exit 2
fi

# ---- one browser for all the browser tests ---------------------------------
CDP_PORT=""
if [ -n "$UI_LIST" ]; then
    CDP_PORT=$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()')
    "$CHROME" --headless=new --disable-gpu --no-sandbox --window-size=1440,900 \
        --disable-features=RendererCodeIntegrity \
        "--remote-debugging-port=$CDP_PORT" "--user-data-dir=$WORK/chrome" \
        about:blank >/dev/null 2>&1 &
    CHROME_PID=$!
    for _ in $(seq 1 100); do
        if curl -s --max-time 1 "http://127.0.0.1:$CDP_PORT/json/version" >/dev/null 2>&1; then break; fi
        sleep 0.1
    done
    export TEA_TEST_CDP="127.0.0.1:$CDP_PORT"
    echo "browser: $CHROME (cdp $CDP_PORT) - one browser for every browser test"
fi

export EXE LOGDIR RESULTS
run_one() {
    local f="$1" name rc start end
    name=$(basename "$f")
    start=$(date +%s)
    case "$f" in
        # This one wants the fully static build (it checks that the binary has no
        # dynamic dependencies).
        *smoke_single_file.py)
            if [ -x build-static/bin/tea_agent ]; then
                python3 "$f" build-static/bin/tea_agent >"$LOGDIR/$name.log" 2>&1
            else
                python3 "$f" "$EXE" >"$LOGDIR/$name.log" 2>&1
            fi
            ;;
        *.py) python3 "$f" "$EXE" >"$LOGDIR/$name.log" 2>&1 ;;
        *.mjs) node "$f" "$EXE" >"$LOGDIR/$name.log" 2>&1 ;;
    esac
    rc=$?
    end=$(date +%s)
    printf '%s\t%s\t%s\n' "$rc" "$((end - start))" "$name" >> "$RESULTS"
}
export -f run_one

echo "running $(echo $LIST | wc -w) test(s) with $JOBS job(s)..."
START=$(date +%s)
# shellcheck disable=SC2086
printf '%s\n' $LIST | xargs -P "$JOBS" -I{} bash -c 'run_one "$@"' _ {}
END=$(date +%s)

# A test that failed while the machine was busy gets a second chance, alone.
# Some of the browser tests wait for a UI state with a fixed sleep; under load
# that sleep is not enough, and the test then fails for a reason that has
# nothing to do with the code.
RETRY=$(awk -F'\t' '$1 != 0 { print $3 }' "$RESULTS")
if [ -n "$RETRY" ]; then
    echo ""
    echo "retrying the failed test(s), one at a time: $(echo $RETRY)"
    for name in $RETRY; do
        f=""
        for cand in $LIST; do
            if [ "$(basename "$cand")" = "$name" ]; then f="$cand"; break; fi
        done
        [ -n "$f" ] || continue
        start=$(date +%s)
        case "$f" in
            *smoke_single_file.py)
                if [ -x build-static/bin/tea_agent ]; then
                    python3 "$f" build-static/bin/tea_agent >"$LOGDIR/$name.retry.log" 2>&1
                else
                    python3 "$f" "$EXE" >"$LOGDIR/$name.retry.log" 2>&1
                fi
                ;;
            *.py) python3 "$f" "$EXE" >"$LOGDIR/$name.retry.log" 2>&1 ;;
            *.mjs) node "$f" "$EXE" >"$LOGDIR/$name.retry.log" 2>&1 ;;
        esac
        rc=$?
        end=$(date +%s)
        if [ "$rc" = "0" ]; then
            printf '0\t%s\t%s\n' "$((end - start))" "$name" >> "$RESULTS"
            mv "$LOGDIR/$name.retry.log" "$LOGDIR/$name.log"
            echo "  $name passed on the retry"
        else
            printf '%s\t%s\t%s\n' "$rc" "$((end - start))" "$name" >> "$RESULTS"
        fi
    done
fi

# ---- summary ----------------------------------------------------------------
FAILED=0
TOTAL=0
while IFS=$'\t' read -r rc secs name; do
    TOTAL=$((TOTAL + 1))
    note=""
    if [ -f "$LOGDIR/$name.log" ]; then
        note=$(grep -oE '[0-9]+/[0-9]+ checks passed|ALL PASS' "$LOGDIR/$name.log" | tail -1)
    fi
    if [ "$rc" = "0" ]; then
        printf '  \033[32mok\033[0m   %-38s %4ss  %s\n' "$name" "$secs" "$note"
    else
        FAILED=$((FAILED + 1))
        printf '  \033[31mFAIL\033[0m %-38s %4ss\n' "$name" "$secs"
        grep -E '^\s*FAIL|FAILED' "$LOGDIR/$name.log" | head -5 | sed 's/^/       /'
    fi
done < "$RESULTS"

printf '\n%d/%d tests passed in %ds  (logs: %s)\n' \
    "$((TOTAL - FAILED))" "$TOTAL" "$((END - START))" "$LOGDIR"
[ "$FAILED" = "0" ] || exit 1
exit 0
