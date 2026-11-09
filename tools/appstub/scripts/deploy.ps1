# deploy.ps1 -- publish appstub to the place it actually runs from.
#
#   pwsh -File tools\appstub\scripts\deploy.ps1
#   pwsh -File tools\appstub\scripts\deploy.ps1 -Exe build-mingw\bin\appstub.exe
#
# Default target: %LOCALAPPDATA%\appstub (appstub.exe + config.json there).
# That directory is also where appstub writes its log, and where the autostart
# task points, so a rebuild only has to be re-deployed - never re-registered.

[CmdletBinding()]
param(
    [string]$Exe = "",
    [string]$Config = "",
    [string]$TargetDir = ""
)

$ErrorActionPreference = 'Stop'
# The repository root: this script lives in <repo>\tools\appstub\scripts.
$root = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $PSScriptRoot))

if (-not $Exe) { $Exe = 'build-mingw\bin\appstub.exe' }
if (-not $Config) { $Config = 'tools\appstub\config\appstub.json' }
if (-not $TargetDir) { $TargetDir = Join-Path $env:LOCALAPPDATA 'appstub' }

$exePath = Join-Path $root $Exe
$configPath = Join-Path $root $Config
foreach ($p in @($exePath, $configPath)) {
    if (-not (Test-Path $p)) { throw "not found: $p" }
}

if (-not (Test-Path $TargetDir)) { New-Item -ItemType Directory -Force -Path $TargetDir | Out-Null }

# A running appstub.exe cannot be replaced (Windows locks the image), so stop it
# first. The autostart task may bring it back - that is fine, it will pick up
# the new binary.
#
# The stop is *graceful*: taskkill without /F posts WM_CLOSE, and the app
# deletes its tray icon when the window is destroyed. A killed process leaves
# the icon behind as a ghost in the notification area - and a ghost only goes
# away when the mouse happens to hover it.
$running = Get-Process -Name 'appstub' -ErrorAction SilentlyContinue
if ($running) {
    Write-Host "stopping the running appstub ..." -ForegroundColor Yellow
    foreach ($proc in $running) {
        & taskkill.exe /PID $proc.Id 2>$null | Out-Null
    }
    $deadline = (Get-Date).AddSeconds(6)
    while ((Get-Date) -lt $deadline) {
        if (-not (Get-Process -Name 'appstub' -ErrorAction SilentlyContinue)) { break }
        Start-Sleep -Milliseconds 200
    }
    $stubborn = Get-Process -Name 'appstub' -ErrorAction SilentlyContinue
    if ($stubborn) {
        Write-Host "it did not go away - killing it (the tray icon may stay behind as a ghost)" -ForegroundColor Yellow
        $stubborn | Stop-Process -Force
        Start-Sleep -Milliseconds 500
    }
}

Copy-Item $exePath (Join-Path $TargetDir 'appstub.exe') -Force
Copy-Item $configPath (Join-Path $TargetDir 'config.json') -Force

Write-Host ("deployed to {0}" -f $TargetDir) -ForegroundColor Green
Get-ChildItem $TargetDir | ForEach-Object {
    Write-Host ("  {0,-16} {1,10:N0} bytes" -f $_.Name, $_.Length) -ForegroundColor DarkGray
}
Write-Host ("log: {0}" -f (Join-Path $TargetDir 'appstub.log')) -ForegroundColor DarkGray
