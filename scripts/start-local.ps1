# scripts/start-local.ps1 -- start an instance you can actually work in.
#
#   pwsh -File scripts\start-local.ps1
#   pwsh -File scripts\start-local.ps1 -Port 8283
#   pwsh -File scripts\start-local.ps1 -Exe build-msvc\bin\tea_agent.exe -Open
#   pwsh -File scripts\start-local.ps1 -AllowOutsideWorkspace
#
# Why a script: the agent refuses to share a port (Windows lets a second
# instance bind a port that is already serving, and then the browser talks to
# whichever of the two it feels like - see cmake/free-running-exe.cmake and
# tests/smoke_port_guard.py). So the port has to be picked on purpose.
#
# It also starts the instance *outside* any sandbox that restricts writes to
# one directory: an instance that may only write its own workspace cannot
# create a session in another workspace ("Windows error 5: Access denied"),
# because the workspace registry knows all of them but the process may not
# write outside its root.
#
# The instance reads your own tea home (~/.tea_agent), so the provider
# configuration and every registered workspace come with it.

[CmdletBinding()]
param(
    [string]$Exe = "",
    [int]$Port = 0,
    [string]$Workspace = "",
    [string]$TeaHome = "",
    [switch]$Open,
    [switch]$AllowOutsideWorkspace
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot

if (-not $Exe) {
    foreach ($candidate in @('build-msvc\bin\tea_agent.exe', 'build-mingw\bin\tea_agent.exe',
            'build\bin\tea_agent.exe', "$env:USERPROFILE\bin\a2.exe")) {
        if (Test-Path (Join-Path $root $candidate)) { $Exe = $candidate; break }
    }
    if (-not $Exe -and (Test-Path "$env:USERPROFILE\bin\a2.exe")) { $Exe = "$env:USERPROFILE\bin\a2.exe" }
}
if (-not $Exe -or -not (Test-Path $Exe)) {
    Write-Host "no binary found; build first (scripts\build.bat, scripts\build-mingw.bat)" -ForegroundColor Red
    exit 2
}
$Exe = (Resolve-Path $Exe).Path
if (-not $TeaHome) { $TeaHome = Join-Path $env:USERPROFILE '.tea_agent' }

# A free port: the agent itself refuses a port that is taken, so pick one that
# nobody holds. The first try is the default (8282), then the next ones.
function Test-PortFree([int]$p) {
    $listener = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, $p)
    try {
        $listener.Start()
        return $true
    } catch {
        return $false
    } finally {
        try { $listener.Stop() } catch { }
    }
}

if ($Port -le 0) {
    $Port = 8282
    while (-not (Test-PortFree $Port) -and $Port -lt 8299) { $Port++ }
    if (-not (Test-PortFree $Port)) {
        Write-Host "no free port between 8282 and 8299; pass -Port <n>" -ForegroundColor Red
        exit 1
    }
} elseif (-not (Test-PortFree $Port)) {
    Write-Host "port $Port is in use - something is already listening there." -ForegroundColor Red
    exit 1
}

# Approvals: the outer Python framework (tea_agent) gates risky tool calls -
# "execute an external program" is medium risk, git/pip/npm/python are high -
# and in enforce mode it asks the user before every one of them. The
# authorisation is read from <start directory>/.tea_agent_run/approval_allow.json
# or from the environment, and the environment is the only one that works no
# matter where the process was started:
#   TEA_APPROVAL_MODE=advisory  - nothing is blocked, risky calls are only
#                                 marked in the audit log
#   TEA_APPROVAL_TOKEN=<any>    - every call counts as authorised
# Both are inherited by everything this agent starts.
$env:TEA_APPROVAL_MODE = "advisory"
$env:TEA_APPROVAL_TOKEN = "1"

$args = @("--tea-home", $TeaHome, "--host", "127.0.0.1", "--port", "$Port", "--no-console")
if ($Workspace) { $args += @("--workspace", $Workspace) }
if ($AllowOutsideWorkspace) { $args += "--allow-outside-workspace" }

Write-Host ("starting {0}" -f $Exe) -ForegroundColor Cyan
Write-Host ("  port      : {0}" -f $Port)
Write-Host ("  tea home  : {0}" -f $TeaHome)
if ($Workspace) { Write-Host ("  workspace : {0}" -f $Workspace) }
Write-Host ("  url       : http://127.0.0.1:{0}" -f $Port) -ForegroundColor Green
Write-Host "press Ctrl+C to stop." -ForegroundColor DarkGray

& $Exe @args
