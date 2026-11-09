# install-autostart.ps1 -- start appstub when the user logs in.
#
#   pwsh -File scripts\install-autostart.ps1              # register
#   pwsh -File scripts\install-autostart.ps1 -Status      # is it registered?
#   pwsh -File scripts\install-autostart.ps1 -Start       # register + start now
#   pwsh -File scripts\install-autostart.ps1 -Uninstall   # remove it
#
# Windows has no user-scoped `systemctl --user enable`; the closest equivalent
# is a Task Scheduler task with a logon trigger and an InteractiveToken
# principal: it runs inside *your* session (so the children see your profile,
# your drives and your tea home), needs no administrator rights and stores no
# password.
#
# appstub.exe is a GUI program, so no hidden-window shim is needed (unlike the
# console tea_agent.exe, which the tea_a2 repository starts through a VBS).
#
# If the ScheduledTasks module is not available, the Run key is used instead:
#   HKCU\Software\Microsoft\Windows\CurrentVersion\Run\appstub

[CmdletBinding()]
param(
    [string]$TaskName = "appstub",
    [string]$Exe = "",
    [string]$Config = "",
    [int]$LogonDelaySeconds = 5,
    # A second task that checks every few minutes whether appstub is still
    # there and starts it if not (the safety net for "it died somehow").
    [string]$KeepAliveTaskName = "appstub-keepalive",
    [int]$KeepAliveMinutes = 10,
    [switch]$NoKeepAlive,
    [switch]$Start,
    [switch]$Uninstall,
    [switch]$Status,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot

if (-not $Exe) { $Exe = Join-Path $env:LOCALAPPDATA 'appstub\appstub.exe' }
if (-not $Config) { $Config = Join-Path $env:LOCALAPPDATA 'appstub\config.json' }

function Get-Task {
    param([string]$Name)
    try { return Get-ScheduledTask -TaskName $Name -ErrorAction Stop } catch { return $null }
}

function Get-RunEntry {
    param([string]$Name)
    $key = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
    try { return (Get-ItemProperty -Path $key -Name $Name -ErrorAction Stop).$Name } catch { return $null }
}

$account = "$env:USERDOMAIN\$env:USERNAME"
$arguments = "--config `"$Config`""

if ($Status) {
    $task = Get-Task $TaskName
    if ($task) {
        Write-Host "task '$TaskName': state=$($task.State) principal=$($task.Principal.UserId) logon=$($task.Principal.LogonType)" -ForegroundColor Cyan
        foreach ($a in $task.Actions) { Write-Host ("  action: {0} {1}" -f $a.Execute, $a.Arguments) -ForegroundColor DarkGray }
    } else {
        Write-Host "task '$TaskName': not registered" -ForegroundColor Yellow
    }
    $keep = Get-Task $KeepAliveTaskName
    if ($keep) {
        Write-Host "task '$KeepAliveTaskName': state=$($keep.State) (every $KeepAliveMinutes minutes)" -ForegroundColor Cyan
        foreach ($a in $keep.Actions) { Write-Host ("  action: {0} {1}" -f $a.Execute, $a.Arguments) -ForegroundColor DarkGray }
    } else {
        Write-Host "task '$KeepAliveTaskName': not registered" -ForegroundColor Yellow
    }
    $run = Get-RunEntry $TaskName
    if ($run) { Write-Host "Run key: $run" -ForegroundColor Cyan }
    exit 0
}

if ($Uninstall) {
    $task = Get-Task $TaskName
    if ($task) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "removed the task '$TaskName'" -ForegroundColor Yellow
    }
    $key = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
    if (Get-RunEntry $TaskName) {
        Remove-ItemProperty -Path $key -Name $TaskName -ErrorAction SilentlyContinue
        Write-Host "removed the Run key '$TaskName'" -ForegroundColor Yellow
    }
    if (Get-Task $KeepAliveTaskName) {
        Unregister-ScheduledTask -TaskName $KeepAliveTaskName -Confirm:$false
        Write-Host "removed the task '$KeepAliveTaskName'" -ForegroundColor Yellow
    }
    exit 0
}

if (-not (Test-Path $Exe)) {
    Write-Host "appstub.exe not found: $Exe" -ForegroundColor Red
    Write-Host "build it first (scripts\build-mingw.bat) and deploy it (scripts\deploy.ps1)." -ForegroundColor Yellow
    exit 1
}

Write-Host "appstub  : $Exe"
Write-Host "config   : $Config"
Write-Host "account  : $account (logon trigger, no password, no administrator rights)"

if ($DryRun) {
    Write-Host ""
    Write-Host "[dry run] would register:" -ForegroundColor Cyan
    Write-Host "  task '$TaskName': logon trigger (+$LogonDelaySeconds), action `"$Exe`" $arguments"
    Write-Host "  fallback: Run key '$TaskName' = `"$Exe`" $arguments"
    exit 0
}

$registered = $false
try {
    $action = New-ScheduledTaskAction -Execute $Exe -Argument $arguments
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $account
    $trigger.Delay = "PT$($LogonDelaySeconds)S"
    $principal = New-ScheduledTaskPrincipal -UserId $account -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
        -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -Principal $principal -Settings $settings `
        -Description "appstub: starts the programs listed in its config (logon trigger)." `
        -Force | Out-Null
    Write-Host "registered the task '$TaskName'" -ForegroundColor Green
    $registered = $true
} catch {
    Write-Host ("the ScheduledTasks module is not usable here: " + $_.Exception.Message) -ForegroundColor Yellow
}

if (-not $registered) {
    $key = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
    if (-not (Test-Path $key)) { New-Item -Path $key -Force | Out-Null }
    New-ItemProperty -Path $key -Name $TaskName -Value "`"$Exe`" $arguments" -PropertyType String -Force | Out-Null
    Write-Host "registered the Run key '$TaskName' instead" -ForegroundColor Green
}

# --- the keep-alive task ---------------------------------------------------
# Every few minutes: is appstub still there? If not, start it again. It runs
# as the logged-on user (InteractiveToken) - a task that ran as SYSTEM would
# put appstub in session 0, where there is no notification area.
if (-not $NoKeepAlive) {
    $keepScript = Join-Path $PSScriptRoot 'keepalive.ps1'
    if (-not (Test-Path $keepScript)) { $keepScript = Join-Path $root 'scripts\keepalive.ps1' }
    try {
        $keepAction = New-ScheduledTaskAction `
            -Execute 'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe' `
            -Argument ("-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$keepScript`"")
        # The repetition has to be asked for at creation time: a trigger
        # built without it has no Repetition object to set the interval on.
        $keepTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
            -RepetitionInterval (New-TimeSpan -Minutes $KeepAliveMinutes) `
            -RepetitionDuration (New-TimeSpan -Days 1)
        $keepTrigger.Repetition.StopAtDurationEnd = $false
        $keepPrincipal = New-ScheduledTaskPrincipal -UserId $account -LogonType Interactive -RunLevel Limited
        $keepSettings = New-ScheduledTaskSettingsSet `
            -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
            -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero)
        Register-ScheduledTask -TaskName $KeepAliveTaskName -Action $keepAction -Trigger $keepTrigger `
            -Principal $keepPrincipal -Settings $keepSettings `
            -Description "appstub keep-alive: start appstub if it is not running (every $KeepAliveMinutes minutes)." `
            -Force | Out-Null
        Write-Host "registered the keep-alive task '$KeepAliveTaskName' (every $KeepAliveMinutes minutes)" -ForegroundColor Green
    } catch {
        Write-Host ("the keep-alive task could not be registered: " + $_.Exception.Message) -ForegroundColor Yellow
    }
}

if ($Start) {
    $task = Get-Task $TaskName
    if ($task) {
        Start-ScheduledTask -TaskName $TaskName
        Write-Host "started the task '$TaskName'" -ForegroundColor Green
    } else {
        Start-Process -FilePath $Exe -ArgumentList $arguments
        Write-Host "started appstub" -ForegroundColor Green
    }
}

Write-Host ""
Write-Host "check it with:  pwsh -File scripts\install-autostart.ps1 -Status" -ForegroundColor DarkGray
exit 0
