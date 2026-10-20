# tests/relink-while-running.ps1 -- the build must not require stopping the agent.
#
#   pwsh -File tests/relink-while-running.ps1
#   pwsh -File tests/relink-while-running.ps1 -Exe build-msvc\bin\tea_agent.exe
#
# Windows refuses to let the linker write over a running .exe (LNK1104 /
# "Permission denied"), which used to mean "stop the agent, build, start the
# agent". cmake/free-running-exe.cmake renames the old binary aside for the length
# of the link and deletes it afterwards, so this checks the whole loop:
#
#   1. start the agent the way autostart does (wscript + hidden window);
#   2. force a relink without touching the repository (mtime bump, restored);
#   3. the build must succeed *while the old instance keeps running*;
#   4. the new binary must be in place under the original name, and the old one
#      must be preserved as <exe>.prev-* (it cannot be deleted while mapped);
#   5. after the process exits, the next build cleans that leftover up.
#
# The build directory is derived from -Exe (<build>/bin/tea_agent.exe), and the
# toolchain must already be usable from the current environment: run this from the
# same shell you normally build in (scripts\build-mingw.bat users need
# .toolchain\mingw64\bin on PATH; MSVC users need scripts\vcenv.bat).

[CmdletBinding()]
param(
    [string]$Exe = "",
    [int]$Port = 8413
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
if ([string]::IsNullOrEmpty($Exe)) {
    foreach ($candidate in @(
            (Join-Path $root 'build-gcc\bin\tea_agent.exe'),
            (Join-Path $root 'build-mingw\bin\tea_agent.exe'),
            (Join-Path $root 'build-msvc\bin\tea_agent.exe'),
            (Join-Path $root 'build\bin\tea_agent.exe'))) {
        if (Test-Path $candidate) { $Exe = $candidate; break }
    }
}
if (-not (Test-Path $Exe)) {
    Write-Host "FAIL: tea_agent binary not found; build it first" -ForegroundColor Red
    exit 2
}
$Exe = (Resolve-Path $Exe).Path
$buildDir = Split-Path -Parent (Split-Path -Parent $Exe)
if (-not (Test-Path (Join-Path $buildDir 'CMakeCache.txt'))) {
    Write-Host "FAIL: $buildDir does not look like a CMake build directory" -ForegroundColor Red
    exit 2
}

if ($IsWindows -eq $false -and $env:OS -ne 'Windows_NT') {
    Write-Host "skipped: this behaviour is Windows-specific" -ForegroundColor Yellow
    exit 0
}

$scratch = Join-Path $PSScriptRoot '.verify\relink'
if (Test-Path $scratch) { Remove-Item -Recurse -Force $scratch }
$homeDir = Join-Path $scratch 'home'
New-Item -ItemType Directory -Force -Path $homeDir | Out-Null
Copy-Item (Join-Path $PSScriptRoot 'fixtures\provider.yaml') (Join-Path $homeDir 'provider.yaml')

$failures = @()
$checks = 0
function Check([string]$name, [bool]$ok, [string]$detail = "") {
    $script:checks++
    if ($ok) {
        Write-Host ("  [ok]   {0}" -f $name) -ForegroundColor Green
    } else {
        Write-Host ("  [FAIL] {0} {1}" -f $name, $detail) -ForegroundColor Red
        $script:failures += $name
    }
}

function Wait-Port([int]$port, [int]$timeoutSec = 20) {
    $deadline = (Get-Date).AddSeconds($timeoutSec)
    while ((Get-Date) -lt $deadline) {
        try {
            $c = New-Object System.Net.Sockets.TcpClient
            $c.Connect('127.0.0.1', $port)
            $c.Close()
            return $true
        } catch { Start-Sleep -Milliseconds 200 }
    }
    return $false
}

function New-Launcher([string]$exe, [string]$workDir) {
    # Same shape as the one scripts/install-autostart.ps1 generates: wscript is a
    # GUI-subsystem binary, so the agent gets no console and survives this script.
    $path = Join-Path $scratch 'run-hidden.vbs'
    $vbs = @"
Set shell = CreateObject("WScript.Shell")
shell.CurrentDirectory = "$workDir"
shell.Run """$exe"" --port $Port --tea-home ""$homeDir"" --no-console", 0, False
"@
    Set-Content -Path $path -Value $vbs -Encoding ASCII
    return $path
}

function Stop-Agent {
    Get-CimInstance Win32_Process -Filter "Name='tea_agent.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -like "*$homeDir*" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Milliseconds 700
}

function Invoke-Relink {
    $out = & cmake --build $buildDir --target tea_agent 2>&1 | Out-String
    return @{ code = $LASTEXITCODE; text = $out }
}

function Touch-Source([string]$path) {
    # Bump the mtime without changing a byte, so the repository stays clean but
    # the target has to recompile and relink.
    $old = (Get-Item $path).LastWriteTimeUtc
    (Get-Item $path).LastWriteTimeUtc = (Get-Date).ToUniversalTime()
    return $old
}

$busy = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($busy) {
    Write-Host "FAIL: port $Port is already in use; pass another -Port" -ForegroundColor Red
    exit 2
}

$sourceFile = Join-Path $root 'src\main.cpp'
$prevPattern = (Split-Path -Leaf $Exe) + '.prev*'
$agentPid = 0

try {
    Write-Host "`n== platform check ==" -ForegroundColor Cyan
    # Prove the premise first: without the PRE_LINK step, writing over the running
    # image is what fails. If this ever stops failing, the whole dance is pointless
    # and the test should be reconsidered rather than silently passing.
    # Our own instance is identified by its command line (the unique --tea-home
    # below), not by the exe path: a developer may well have a long-running
    # instance of the very same binary open, and that one is none of our business.
    $launcher = New-Launcher $Exe (Split-Path -Parent $Exe)
    & wscript.exe $launcher
    Check "agent is listening before the build" (Wait-Port $Port 25)
    $running = Get-CimInstance Win32_Process -Filter "Name='tea_agent.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -like "*$homeDir*" }
    Check "our own agent process found" ($null -ne $running) "count=$(@($running).Count)"
    if ($running) { $agentPid = @($running)[0].ProcessId }
    $overwriteBlocked = $false
    try {
        $fs = [IO.File]::Open($Exe, 'Append', 'Write', 'None')
        $fs.Close()
    } catch {
        $overwriteBlocked = $true
    }
    Check "a running image cannot be opened for writing (the premise)" $overwriteBlocked

    Write-Host "`n== rebuild while it keeps running ==" -ForegroundColor Cyan
    $prevBefore = @(Get-ChildItem -Path (Split-Path -Parent $Exe) -Filter $prevPattern -ErrorAction SilentlyContinue |
        ForEach-Object { $_.Name })
    $beforeTime = (Get-Item $Exe).LastWriteTimeUtc
    $beforeSize = (Get-Item $Exe).Length
    $oldMtime = Touch-Source $sourceFile
    try {
        $build = Invoke-Relink
    } finally {
        (Get-Item $sourceFile).LastWriteTimeUtc = $oldMtime
    }
    Check "build succeeds with the agent running" ($build.code -eq 0) `
        (($build.text -split "`n" | Select-String -Pattern 'error|Error' | Select-Object -First 3) -join '; ')
    Check "build reported freeing the running binary" ($build.text -match 'Freeing tea_agent')

    $afterTime = (Get-Item $Exe).LastWriteTimeUtc
    Check "a new binary was linked" ($afterTime -gt $beforeTime) `
        "before=$beforeTime after=$afterTime"
    # Only the copies this build produced count: another (unrelated) instance may
    # legitimately hold one of its own from an earlier build.
    $prevNow = @(Get-ChildItem -Path (Split-Path -Parent $Exe) -Filter $prevPattern -ErrorAction SilentlyContinue |
        ForEach-Object { $_.Name })
    $newLeftovers = @($prevNow | Where-Object { $prevBefore -notcontains $_ })
    Check "the old binary is preserved as .prev-*" ($newLeftovers.Count -eq 1) `
        "new=$($newLeftovers -join ',') all=$($prevNow.Count)"

    $stillAlive = Get-Process -Id $agentPid -ErrorAction SilentlyContinue
    Check "the old instance survived the build" ($null -ne $stillAlive)
    Check "the old instance still serves requests" `
        ((Invoke-RestMethod "http://127.0.0.1:$Port/api/health" -TimeoutSec 10).ok -eq $true)

    Write-Host "`n== the leftover is cleaned once the process is gone ==" -ForegroundColor Cyan
    Stop-Agent
    $stopped = -not (Get-Process -Id $agentPid -ErrorAction SilentlyContinue)
    $agentPid = 0
    Check "agent stopped" $stopped
    $oldMtime = Touch-Source $sourceFile
    try {
        $build2 = Invoke-Relink
    } finally {
        (Get-Item $sourceFile).LastWriteTimeUtc = $oldMtime
    }
    Check "second build succeeds" ($build2.code -eq 0)
    $prevAfter = @(Get-ChildItem -Path (Split-Path -Parent $Exe) -Filter $prevPattern -ErrorAction SilentlyContinue |
        ForEach-Object { $_.Name })
    $oursLeft = @($newLeftovers | Where-Object { $prevAfter -contains $_ })
    Check "the leftover from this build is cleaned up" ($oursLeft.Count -eq 0) `
        ("still there: " + ($oursLeft -join ','))
} catch {
    Write-Host "`nUNEXPECTED ERROR: $_" -ForegroundColor Red
    $failures += "exception"
} finally {
    Stop-Agent
    if ($agentPid -ne 0) { Stop-Process -Id $agentPid -Force -ErrorAction SilentlyContinue }
    if (Test-Path $scratch) { Remove-Item -Recurse -Force $scratch -ErrorAction SilentlyContinue }
}

Write-Host ""
if ($failures.Count -eq 0) {
    Write-Host "ALL $checks CHECKS PASSED" -ForegroundColor Green
    exit 0
}
Write-Host "$($failures.Count) of $checks CHECKS FAILED:" -ForegroundColor Red
$failures | ForEach-Object { Write-Host "  - $_" -ForegroundColor Red }
exit 1
