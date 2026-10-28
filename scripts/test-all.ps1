# scripts/test-all.ps1 -- run the whole test suite with one command.
#
#   pwsh -File scripts\test-all.ps1
#   pwsh -File scripts\test-all.ps1 -Exe build-msvc\bin\tea_agent.exe
#   pwsh -File scripts\test-all.ps1 -SkipUi
#
# Why a script instead of a long command line:
#
#   * one entry point for humans and for agents - the same command works on
#     Windows, in CI, and from a shell that does not care about quoting;
#   * the binary is picked here, so the caller does not have to repeat (and
#     mistype) a path to a file that is rebuilt anyway;
#   * every test gets the environment it needs (the toolchains' TEMP, CHROME_BIN),
#     which is otherwise a per-command chore;
#   * the summary at the end says what passed, so nobody has to scroll.
#
# Exit code 0 when everything passed, 1 otherwise.

[CmdletBinding()]
param(
    [string]$Exe = "",
    [switch]$SkipUi,
    [switch]$SkipPython,
    [string]$Only = ""
)

$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $PSScriptRoot

# ---- which binary ------------------------------------------------------------
if (-not $Exe) {
    foreach ($candidate in @(
            'build-mingw\bin\tea_agent.exe',
            'build-msvc\bin\tea_agent.exe',
            'build-gcc\bin\tea_agent.exe',
            'build\bin\tea_agent.exe')) {
        if (Test-Path (Join-Path $root $candidate)) { $Exe = $candidate; break }
    }
}
if (-not $Exe -or -not (Test-Path (Join-Path $root $Exe))) {
    Write-Host "no binary found; build first (scripts\build-mingw.bat)" -ForegroundColor Red
    exit 2
}
$Exe = (Resolve-Path (Join-Path $root $Exe)).Path
Write-Host "binary: $Exe" -ForegroundColor Cyan

# ---- the environment the tests need -----------------------------------------
# The toolchains write their temporary files through TEMP/TMP, and a locked-down
# %TEMP% makes gcc fail with "Cannot create temporary file ... Permission denied".
$tmp = Join-Path $root '.tmp'
if (-not (Test-Path $tmp)) { New-Item -ItemType Directory -Force -Path $tmp | Out-Null }
$env:TEMP = $tmp
$env:TMP = $tmp
if (-not $env:CHROME_BIN) {
    foreach ($chrome in @(
            "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe",
            'C:\Program Files\Google\Chrome\Application\chrome.exe',
            'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe')) {
        if (Test-Path $chrome) { $env:CHROME_BIN = $chrome; break }
    }
}
$hasChrome = [bool]$env:CHROME_BIN

# Script scope: a function that does `$results += ...` would otherwise assign to a
# *local* copy and the summary would come out empty.
$script:results = @()
function Run-Step([string]$name, [scriptblock]$action) {
    if ($Only -and $name -notlike "*$Only*") { return }
    Write-Host ""
    Write-Host "== $name ==" -ForegroundColor Cyan
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $out = & $action 2>&1 | Out-String
    $sw.Stop()
    $bad = @($out -split "`n" | Where-Object { $_ -match '^\s*FAIL\b' -or $_ -match 'CHECKS FAILED' })
    $passed = [regex]::Match($out, '(\d+)/(\d+) checks passed')
    $summary = if ($passed.Success) { "$($passed.Groups[1].Value)/$($passed.Groups[2].Value) checks" }
               elseif ($out -match 'ALL PASS') { 'all passed' }
               else { '' }
    if ($bad.Count -gt 0) {
        $script:results += [pscustomobject]@{ name = $name; ok = $false; note = $summary; seconds = [int]$sw.Elapsed.TotalSeconds }
        Write-Host "  FAILED ($([int]$sw.Elapsed.TotalSeconds)s) $summary" -ForegroundColor Red
        $bad | Select-Object -First 8 | ForEach-Object { Write-Host "    $($_.Trim())" -ForegroundColor Red }
    } else {
        $script:results += [pscustomobject]@{ name = $name; ok = $true; note = $summary; seconds = [int]$sw.Elapsed.TotalSeconds }
        Write-Host "  ok ($([int]$sw.Elapsed.TotalSeconds)s) $summary" -ForegroundColor Green
    }
}

# ---- the suite ----------------------------------------------------------------
Run-Step 'end-to-end (tests/e2e.ps1)' {
    & pwsh -NoProfile -File (Join-Path $root 'tests\e2e.ps1') -Exe $Exe
}

if (-not $SkipPython) {
    $smokes = @('smoke_config_quickstart.py', 'smoke_concurrency.py', 'smoke_task_runner.py',
                 'smoke_artifacts.py',
                 'smoke_agent_doc.py', 'smoke_prompt_queue.py', 'smoke_system_prompt.py',
                 'smoke_todo.py', 'smoke_tool_images.py', 'smoke_usage_cache.py',
                 'smoke_provider_usage.py', 'smoke_console_level.py', 'smoke_vision.py',
                 'smoke_web_tools.py', 'smoke_run_archive.py', 'smoke_multi_session.py',
                 'smoke_export_html.py', 'smoke_destroy.py', 'smoke_merge_work_area.py')
    foreach ($s in $smokes) {
        if ($Only -and $s -notlike "*$Only*") { continue }
        Run-Step $s { & python (Join-Path $root "tests\$s") $Exe }
    }
}

Run-Step 'windows autostart (tests/autostart.ps1)' {
    & pwsh -NoProfile -File (Join-Path $root 'tests\autostart.ps1') -Exe $Exe
}
Run-Step 'relink while running (tests/relink-while-running.ps1)' {
    & pwsh -NoProfile -File (Join-Path $root 'tests\relink-while-running.ps1') -Exe $Exe
}

if (-not $SkipUi) {
    if ($hasChrome) {
        $ui = @('ui_setup_quickstart.mjs', 'ui_setup_flow.mjs', 'ui_system_prompt.mjs',
                 'ui_provider_reset.mjs', 'ui_workspace_access.mjs', 'ui_metrics_layout.mjs',
                 'ui_tab_blink.mjs',
                 'ui_sidebar.mjs', 'ui_status.mjs', 'ui_todo.mjs', 'ui_queue.mjs',
                 'ui_turn_index.mjs', 'ui_stream_guard.mjs', 'ui_stream_resume.mjs',
                 'ui_second_client.mjs', 'ui_two_clients.mjs', 'ui_workspace_add.mjs',
                 'ui_workspace_delete.mjs',
                 'ui_agent_doc.mjs', 'ui_tool_images.mjs', 'ui_artifacts.mjs',
                 'ui_crumbs.mjs', 'ui_destroy.mjs', 'ui_files_download.mjs',
                 'ui_session_export.mjs')
        foreach ($u in $ui) {
            if ($Only -and $u -notlike "*$Only*") { continue }
            Run-Step $u { & node (Join-Path $root "tests\$u") $Exe }
        }
    } else {
        Write-Host ""
        Write-Host "== browser tests skipped (no chrome) ==" -ForegroundColor Yellow
    }
}

# ---- summary -----------------------------------------------------------------
Write-Host ""
$failed = @($results | Where-Object { -not $_.ok })
$color = if ($failed.Count -eq 0) { 'Green' } else { 'Red' }
Write-Host ("{0}/{1} steps passed" -f ($results.Count - $failed.Count), $results.Count) -ForegroundColor $color
$results | ForEach-Object {
    Write-Host ("  {0,-5} {1,-46} {2,4}s {3}" -f $(if ($_.ok) { 'ok' } else { 'FAIL' }), $_.name, $_.seconds, $_.note)
}
if ($failed.Count -gt 0) { exit 1 }
exit 0
