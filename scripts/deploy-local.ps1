# scripts/deploy-local.ps1 -- publish a built binary to the launcher path.
#
#   pwsh -File scripts\deploy-local.ps1 -Exe build-msvc\bin\tea_agent.exe
#   pwsh -File scripts\deploy-local.ps1 -Exe build-mingw\bin\tea_agent.exe -Stop
#   pwsh -File scripts\deploy-local.ps1 -Exe build-msvc\bin\tea_agent.exe -Target C:\tools\a2.exe
#
# Why a script and not "Copy-Item": the target is usually *running* (an agent
# cannot replace its own executable - Windows locks the image of a running
# process), and the copy then dies with "Access is denied". This script says
# so in words, and with -Stop it stops that instance first.
#
# A symbolic link would be nicer (the launcher always points at the fresh
# build), but creating one needs SeCreateSymbolicLinkPrivilege: an elevated
# shell, or a token that has the privilege. `whoami /priv | findstr symlink`
# tells you whether the privilege is there; without it, `New-Item -ItemType
# SymbolicLink` and `mklink` both fail with "administrator privilege
# required", Developer Mode's registry flag notwithstanding.

[CmdletBinding()]
param(
    [string]$Exe = "",
    [string]$Target = "",
    [switch]$Stop
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot

if (-not $Exe) {
    foreach ($candidate in @('build-msvc\bin\tea_agent.exe', 'build-mingw\bin\tea_agent.exe',
            'build\bin\tea_agent.exe')) {
        if (Test-Path (Join-Path $root $candidate)) { $Exe = $candidate; break }
    }
}
if (-not $Exe -or -not (Test-Path (Join-Path $root $Exe))) {
    Write-Host "no built binary found; build first (scripts\build.bat, scripts\build-mingw.bat)" -ForegroundColor Red
    exit 2
}
if (-not $Target) { $Target = Join-Path $env:USERPROFILE 'bin\a2.exe' }

$src = (Resolve-Path (Join-Path $root $Exe)).Path
$targetDir = Split-Path -Parent $Target
if (-not (Test-Path $targetDir)) {
    New-Item -ItemType Directory -Force -Path $targetDir | Out-Null
}
$image = Split-Path -Leaf $Target

# The image of a running process cannot be replaced. tasklist is used on
# purpose: a sandboxed shell may not see every process in the CIM view, and
# "the file is locked" is the ground truth anyway.
function Test-TargetBusy {
    $out = & tasklist /fi "imagename eq $image" /nh 2>$null
    return [bool]($out | Where-Object { $_ -match [regex]::Escape($image) })
}

if (Test-TargetBusy) {
    if (-not $Stop) {
        Write-Host "the target is in use: '$image' is running, and a running image cannot be replaced." -ForegroundColor Yellow
        Write-Host "stop it (taskkill /IM $image /F) and re-run, or re-run with -Stop." -ForegroundColor Yellow
        Write-Host "note: stopping it ends the session that is using it." -ForegroundColor Yellow
        exit 1
    }
    Write-Host "stopping '$image' ..." -ForegroundColor Yellow
    & taskkill /IM $image /F 2>&1 | Out-Null
    Start-Sleep -Milliseconds 700
}

try {
    Copy-Item $src $Target -Force
} catch {
    Write-Host ("copy failed: " + $_.Exception.Message) -ForegroundColor Red
    Write-Host "the file is probably still locked by a running instance - try: taskkill /IM $image /F" -ForegroundColor Red
    exit 1
}
$info = Get-Item $Target
Write-Host ("deployed {0} -> {1} ({2:N0} bytes)" -f $src, $info.FullName, $info.Length) -ForegroundColor Green
