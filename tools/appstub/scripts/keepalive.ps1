# keepalive.ps1 -- start appstub if it is not running.
#
# The scheduled task that runs this every few minutes is the safety net for
# "appstub died somehow" (a crash, a session switch, an antivirus hiccup).
# The task itself runs as the logged-on user (InteractiveToken), so an appstub
# started from here gets a notification-area icon - a task running as SYSTEM
# would put it in session 0, where there is no tray at all.
#
# Usage:  powershell -NoProfile -ExecutionPolicy Bypass -File keepalive.ps1

param(
    [string]$Exe = "",
    [string]$Config = "",
    [int]$Quiet = 1
)

$ErrorActionPreference = 'Stop'
if (-not $Exe) { $Exe = Join-Path $env:LOCALAPPDATA 'appstub\appstub.exe' }
if (-not $Config) { $Config = Join-Path $env:LOCALAPPDATA 'appstub\config.json' }

function Say([string]$text) {
    if (-not $Quiet) { Write-Host $text }
}

# The single-instance mutex inside appstub is the real guard, but a cheap
# process check keeps the task from starting a second copy every ten minutes.
$running = Get-Process -Name 'appstub' -ErrorAction SilentlyContinue
if ($running) {
    Say ("appstub is running (pid " + $running[0].Id + ")")
    exit 0
}

if (-not (Test-Path $Exe)) {
    Say ("not found: " + $Exe)
    exit 1
}

Say ("starting " + $Exe)
Start-Process -FilePath $Exe -ArgumentList ("--config `"$Config`"")
Start-Sleep -Milliseconds 800
$now = Get-Process -Name 'appstub' -ErrorAction SilentlyContinue
if ($now) {
    Say ("started (pid " + $now[0].Id + ")")
    exit 0
}
Say "it did not come up"
exit 1
