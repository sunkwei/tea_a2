# tests/autostart.ps1 -- verifies the Windows "start at logon" helper.
#
#   pwsh -File tests/autostart.ps1
#   pwsh -File tests/autostart.ps1 -Exe build-gcc\bin\tea_agent.exe
#
# It does NOT touch Task Scheduler: registering tasks needs an interactive,
# non-restricted session and CI usually is not one. Instead it checks the two
# halves that can break silently:
#
#   1. `-DryRun` produces a launcher and a task XML with the properties that make
#      the task behave like `systemctl --user enable --now` (logon trigger,
#      InteractiveToken principal, no execution time limit, single instance,
#      restart on failure);
#   2. that generated .vbs shim really starts the agent, with no console window,
#      and leaves it listening on the requested port.
#
# Exit code 0 when everything passes.

[CmdletBinding()]
param(
    [string]$Exe = "",
    [int]$Port = 8412
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

$scratch = Join-Path $PSScriptRoot '.verify\autostart'
if (Test-Path $scratch) { Remove-Item -Recurse -Force $scratch }
$launcherDir = Join-Path $scratch 'launcher'
$homeDir = Join-Path $scratch 'home'
New-Item -ItemType Directory -Force -Path $launcherDir, $homeDir | Out-Null
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

function Stop-OurAgent {
    # Kill the shim first: while it runs it re-launches the agent on a non-zero exit.
    Get-CimInstance Win32_Process -Filter "Name='wscript.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -like '*start-hidden.vbs*' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Get-CimInstance Win32_Process -Filter "Name='tea_agent.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -like "*$homeDir*" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Milliseconds 500
}

$busy = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($busy) {
    Write-Host "FAIL: port $Port is already in use; pass another -Port" -ForegroundColor Red
    exit 2
}

try {
    Write-Host "`n== install-autostart -DryRun ==" -ForegroundColor Cyan
    $out = & pwsh -NoProfile -File (Join-Path $root 'scripts\install-autostart.ps1') `
        -Exe $Exe -LauncherDir $launcherDir -DryRun --port $Port --tea-home $homeDir 2>&1 | Out-String
    $code = $LASTEXITCODE
    Check "dry run exits 0" ($code -eq 0) "exit=$code output=$($out.Trim())"
    Check "dry run registers nothing" ($out -match 'nothing registered')

    $launcher = Join-Path $launcherDir 'start-hidden.vbs'
    Check "launcher generated" (Test-Path $launcher)
    if (Test-Path $launcher) {
        $vbs = Get-Content $launcher -Raw
        Check "launcher starts the agent hidden" ($vbs -match ', 0, True') $vbs
        Check "launcher uses the requested exe" ($vbs -match [regex]::Escape($Exe))
        Check "launcher passes the requested port" ($vbs -match "--port $Port")
        Check "launcher retries a crashed agent" ($vbs -match 'WScript\.Sleep 3000')
        Check "launcher runs in the exe directory" `
            ($vbs -match ('shell\.CurrentDirectory = ' + [regex]::Escape('"' + (Split-Path -Parent $Exe) + '"')))
    }

    # The XML is written next to the launcher in dry-run mode; read it from disk
    # rather than from the console text (console wrapping would corrupt it).
    $xmlFile = Get-ChildItem -Path $launcherDir -Filter 'task-*.xml' -ErrorAction SilentlyContinue |
        Select-Object -First 1
    Check "dry run writes the task XML" ($null -ne $xmlFile)
    $xmlOk = $false
    $xmlText = ""
    if ($xmlFile) {
        $xmlText = Get-Content $xmlFile.FullName -Raw
        try { $null = [xml]$xmlText; $xmlOk = $true } catch { }
    }
    Check "task XML is well formed" $xmlOk
    Check "task triggers at logon" ($xmlText -match '<LogonTrigger>')
    Check "task runs in the interactive session" ($xmlText -match '<LogonType>InteractiveToken</LogonType>')
    Check "task has no execution time limit" ($xmlText -match '<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>')
    Check "task ignores a second instance" ($xmlText -match '<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>')
    Check "task restarts after a failure" ($xmlText -match '<RestartOnFailure>')
    Check "task action is the hidden launcher" ($xmlText -match 'wscript\.exe')

    Write-Host "`n== the generated shim really starts the agent ==" -ForegroundColor Cyan
    # wscript is a GUI-subsystem binary, so this returns immediately while the
    # shim keeps running in the background.
    & wscript.exe $launcher
    $up = Wait-Port $Port 25
    Check "agent listens on port $Port" $up
    if ($up) {
        $health = Invoke-RestMethod "http://127.0.0.1:$Port/api/health" -TimeoutSec 10
        Check "agent answers /api/health" ($health.ok -eq $true)
    }
} catch {
    Write-Host "`nUNEXPECTED ERROR: $_" -ForegroundColor Red
    $failures += "exception"
} finally {
    Stop-OurAgent
}

Write-Host ""
if ($failures.Count -eq 0) {
    Write-Host "ALL $checks CHECKS PASSED" -ForegroundColor Green
    exit 0
}
Write-Host "$($failures.Count) of $checks CHECKS FAILED:" -ForegroundColor Red
$failures | ForEach-Object { Write-Host "  - $_" -ForegroundColor Red }
exit 1
