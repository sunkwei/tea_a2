# scripts/install-autostart.ps1 -- start tea_agent when the user logs in.
#
# Windows has no user-scoped `systemctl --user enable`; the closest equivalent is
# a Task Scheduler task with a logon trigger and an InteractiveToken principal,
# which is what this script registers:
#
#   * runs inside *your* session (Web UI reachable, file tools see your profile
#     and mapped drives), unlike a Windows service, which lives in session 0;
#   * needs no administrator rights and stores no password;
#   * restarts on failure, has no execution time limit (PT0S) and ignores a
#     second instance - the three settings that make it behave like a service.
#
#   pwsh -File scripts/install-autostart.ps1                # register
#   pwsh -File scripts/install-autostart.ps1 -Start         # register + run now
#   pwsh -File scripts/install-autostart.ps1 -Status        # is it registered?
#   pwsh -File scripts/install-autostart.ps1 -Uninstall     # remove it
#
# Extra agent flags are passed through verbatim after the script's own options:
#   pwsh -File scripts\install-autostart.ps1 -Start --port 9000 --log-level debug
#
# A console-subsystem app started by Task Scheduler leaves a console window open
# for its whole lifetime, so the default action is wscript.exe running a tiny
# generated .vbs shim that starts the agent with SW_HIDE and re-launches it if it
# dies with a non-zero exit code. Pass -NoHide to run the exe directly (handy
# while debugging).

[CmdletBinding()]
param(
    [string]$Exe = "",
    [string]$TaskName = "tea_agent",
    [int]$LogonDelaySeconds = 5,
    [string]$LauncherDir = "",
    [switch]$NoHide,
    [switch]$Start,
    [switch]$Uninstall,
    [switch]$Status,
    # Generate the launcher + task XML and print the plan, but do not register.
    [switch]$DryRun,
    # Anything left over is passed to tea_agent verbatim:
    #   pwsh -File scripts\install-autostart.ps1 -Start --port 9000 --log-level debug
    [Parameter(ValueFromRemainingArguments = $true)][string[]]$AgentFlags = @()
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot

function Resolve-AgentExe {
    param([string]$Path)
    if (-not [string]::IsNullOrEmpty($Path)) {
        if (-not (Test-Path $Path)) { throw "not found: $Path" }
        return (Resolve-Path $Path).Path
    }
    foreach ($candidate in @(
            'build-mingw\bin\tea_agent.exe',
            'build-gcc\bin\tea_agent.exe',
            'build-msvc\bin\tea_agent.exe',
            'build\bin\tea_agent.exe')) {
        $full = Join-Path $root $candidate
        if (Test-Path $full) { return (Resolve-Path $full).Path }
    }
    throw "tea_agent.exe not found; build it first (scripts\build-mingw.bat) or pass -Exe"
}

function Get-Task {
    param([string]$Name)
    try { return Get-ScheduledTask -TaskName $Name -ErrorAction Stop } catch { return $null }
}

# Where the generated .vbs shim lives: next to the agent home by default, so it
# survives rebuilds and is easy to find (`~\.tea_agent\autostart`).
function Get-LauncherDir {
    param([string]$Path)
    if (-not [string]::IsNullOrEmpty($Path)) { return [IO.Path]::GetFullPath($Path) }
    $homeDir = if ($env:TEA_AGENT_HOME) { $env:TEA_AGENT_HOME } else { Join-Path $env:USERPROFILE '.tea_agent' }
    return [IO.Path]::GetFullPath((Join-Path $homeDir 'autostart'))
}

function Quote([string]$s) {
    if ($s -match '[\s"]' -or $s -eq '') { return '"' + $s.Replace('"', '\"') + '"' }
    return $s
}

# VBScript string literal: delimiters are `"`, an embedded `"` is doubled.
function VbsString([string]$s) { return '"' + $s.Replace('"', '""') + '"' }

function EscapeXml([string]$s) { return [Security.SecurityElement]::Escape($s) }

# --- status / uninstall ----------------------------------------------------
$existing = Get-Task $TaskName

if ($Status) {
    if (-not $existing) {
        Write-Host "task '$TaskName' is not registered" -ForegroundColor Yellow
        exit 1
    }
    $info = Get-ScheduledTaskInfo -TaskName $TaskName
    Write-Host "task      : $TaskName ($($existing.State))" -ForegroundColor Green
    Write-Host "action    : $(($existing.Actions | ForEach-Object { "$($_.Execute) $($_.Arguments)" }) -join '; ')"
    Write-Host "trigger   : $(($existing.Triggers | ForEach-Object { $_.CimClass.CimClassName }) -join ', ')"
    Write-Host "last run  : $($info.LastRunTime)  result=$($info.LastTaskResult)"
    Write-Host "next run  : $($info.NextRunTime)"
    exit 0
}

if ($Uninstall) {
    if (-not $existing) {
        Write-Host "task '$TaskName' is not registered; nothing to do" -ForegroundColor Yellow
    } else {
        try { Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue } catch { }
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "removed task '$TaskName'" -ForegroundColor Green
    }
    $launcher = Join-Path (Get-LauncherDir $LauncherDir) 'start-hidden.vbs'
    if (Test-Path $launcher) {
        Remove-Item $launcher -Force
        Write-Host "removed launcher $launcher" -ForegroundColor Green
    }
    exit 0
}

# --- register --------------------------------------------------------------
$exePath = Resolve-AgentExe $Exe
$workDir = Split-Path -Parent $exePath

# No console to echo to, and the log file keeps the history.
$effectiveArgs = @($AgentFlags)
if ($effectiveArgs -notcontains '--no-console' -and $effectiveArgs -notcontains '--quiet') {
    $effectiveArgs += '--no-console'
}

$commandLine = Quote $exePath
foreach ($a in $effectiveArgs) { $commandLine += ' ' + (Quote $a) }

$LauncherDir = Get-LauncherDir $LauncherDir

if ($NoHide) {
    $actionExe = $exePath
    $actionArgs = ($effectiveArgs | ForEach-Object { Quote $_ }) -join ' '
} else {
    New-Item -ItemType Directory -Force -Path $LauncherDir | Out-Null
    $launcher = Join-Path $LauncherDir 'start-hidden.vbs'
    $vbs = @"
' tea_agent autostart shim -- generated by scripts/install-autostart.ps1
' Starts the agent with no visible console window. A non-zero exit is retried
' after 3s (service-like restart); a clean exit ends the loop.
Set shell = CreateObject("WScript.Shell")
shell.CurrentDirectory = $(VbsString $workDir)
Do
  rc = shell.Run($(VbsString $commandLine), 0, True)
  If rc = 0 Then Exit Do
  WScript.Sleep 3000
Loop
"@
    Set-Content -Path $launcher -Value $vbs -Encoding ASCII
    $actionExe = Join-Path $env:WINDIR 'System32\wscript.exe'
    $actionArgs = Quote $launcher
}

$user = [Security.Principal.WindowsIdentity]::GetCurrent().Name

$xml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Starts tea_agent at user logon (generated by scripts/install-autostart.ps1).</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>$(EscapeXml $user)</UserId>
      <Delay>PT$([int]$LogonDelaySeconds)S</Delay>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>$(EscapeXml $user)</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>999</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>$(EscapeXml $actionExe)</Command>
      <Arguments>$(EscapeXml $actionArgs)</Arguments>
      <WorkingDirectory>$(EscapeXml $workDir)</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"@

# Keep the task XML beside the launcher: %TEMP% is not always writable (locked
# down accounts, sandboxes), while the launcher directory always is.
$scratchDir = if ($NoHide) { $workDir } else { $LauncherDir }
$xmlFile = Join-Path $scratchDir ("task-" + [guid]::NewGuid().ToString('N') + ".xml")

# Cheap sanity check before shelling out: a malformed document would come back
# from schtasks as a useless "cannot find the path specified".
$null = [xml]$xml

if ($DryRun) {
    Write-Host "dry run: nothing registered" -ForegroundColor Yellow
    Write-Host "  action    : $actionExe $actionArgs"
    Write-Host "  agent     : $commandLine"
    Write-Host "  workdir   : $workDir"
    if (-not $NoHide) { Write-Host "  launcher  : $launcher"; Write-Host "--- $launcher ---"; Get-Content $launcher }
    # Written out as UTF-16, exactly like the real registration does, so the file
    # can be inspected (or hand-edited and imported) without retyping anything.
    Set-Content -Path $xmlFile -Value $xml -Encoding Unicode
    Write-Host "  task xml  : $xmlFile"
    Write-Host "--- task xml ---"
    Write-Host $xml
    exit 0
}
try {
    # schtasks wants the XML as Unicode (UTF-16 with BOM).
    Set-Content -Path $xmlFile -Value $xml -Encoding Unicode
    $output = & schtasks.exe /Create /TN $TaskName /XML $xmlFile /F 2>&1
    if ($LASTEXITCODE -ne 0) {
        $detail = ($output -join ' ').Trim()
        Write-Host "schtasks failed ($LASTEXITCODE): $detail" -ForegroundColor Red
        Write-Host ""
        Write-Host "The task XML is valid; the failure comes from Task Scheduler itself. Common causes:"
        Write-Host "  * this shell cannot reach the Task Scheduler service (RPC denied, service disabled,"
        Write-Host "    or a restricted/sandboxed account) - try a normal PowerShell window;"
        Write-Host "  * a task with this name was created by another user and needs elevation"
        Write-Host "    (schtasks /Delete /TN $TaskName from an elevated prompt)."
        throw "could not register task '$TaskName'"
    }
} finally {
    if (Test-Path $xmlFile) { Remove-Item $xmlFile -Force }
}

$registered = Get-Task $TaskName
if (-not $registered) { throw "task '$TaskName' was not created" }

Write-Host "registered task '$TaskName'" -ForegroundColor Green
Write-Host "  user      : $user"
Write-Host "  trigger   : at logon (delay ${LogonDelaySeconds}s)"
Write-Host "  action    : $actionExe $actionArgs"
Write-Host "  agent     : $commandLine"
Write-Host "  workdir   : $workDir"
if (-not $NoHide) { Write-Host "  launcher  : $launcher" }
Write-Host ""
Write-Host "inspect : Get-ScheduledTask -TaskName $TaskName | Format-List"
Write-Host "stop    : Stop-ScheduledTask -TaskName $TaskName"
Write-Host "remove  : pwsh -File scripts/install-autostart.ps1 -Uninstall"

if ($Start) {
    Start-ScheduledTask -TaskName $TaskName
    Start-Sleep -Seconds 2
    $info = Get-ScheduledTaskInfo -TaskName $TaskName
    Write-Host "started; state=$((Get-Task $TaskName).State) lastResult=$($info.LastTaskResult)" -ForegroundColor Green
}
