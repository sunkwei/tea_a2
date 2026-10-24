# scripts/make-link.ps1 -- create a symbolic link without administrator rights.
#
#   pwsh -File scripts\make-link.ps1 -Link C:\Users\Hetin\bin\a2.exe `
#                                     -Target ..\..\work\git\tea_a2\build-msvc\bin\tea_agent.exe
#
# Why not `New-Item -ItemType SymbolicLink`: Windows treats link creation as a
# privilege (SeCreateSymbolicLinkPrivilege) because a link is a reparse point -
# a program can write through it to a place your token may not reach. Only
# administrators get that privilege by default, and neither New-Item nor
# mklink passes SYMBOLIC_LINK_FLAG_ALLOW_UNPRIVILEGED_CREATE (0x2), so both
# demand an elevated shell even with Developer Mode on.
#
# The flag is the documented way for a *normal* user token to create a link
# (Windows 10 1607+, Developer Mode enabled). This calls CreateSymbolicLinkW
# with it. A target that is relative is resolved against the link's own
# directory, exactly like mklink.
#
# If this still fails with "privilege not held", the token really has no
# privilege at all (a restricted/sandboxed token removes it instead of
# disabling it) - then use an elevated shell, or scripts\deploy-local.ps1
# (copy the binary instead of linking it).

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Link,
    [Parameter(Mandatory = $true)][string]$Target,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'

$code = @"
using System;
using System.Runtime.InteropServices;
public static class TeaSymlink {
  [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
  [return: MarshalAs(UnmanagedType.Bool)]
  public static extern bool CreateSymbolicLink(string link, string target, uint flags);
}
"@
if (-not ('TeaSymlink' -as [type])) { Add-Type -TypeDefinition $code }

$linkDir = Split-Path -Parent (Join-Path (Get-Location) $Link)
if (-not $linkDir) { $linkDir = (Get-Location).Path }
$linkPath = if ([System.IO.Path]::IsPathRooted($Link)) { $Link } else { Join-Path (Get-Location) $Link }
$linkDir = Split-Path -Parent $linkPath
if (-not (Test-Path $linkDir)) {
    Write-Host "the link's directory does not exist: $linkDir" -ForegroundColor Red
    exit 2
}

# A relative target is relative to the link's directory (mklink semantics).
$targetPath = if ([System.IO.Path]::IsPathRooted($Target)) { $Target } else { Join-Path $linkDir $Target }
if (-not (Test-Path $targetPath)) {
    Write-Host "the target does not exist: $targetPath" -ForegroundColor Red
    exit 2
}

if (Test-Path $linkPath) {
    if (-not $Force) {
        Write-Host "something is already there: $linkPath (use -Force to replace it)" -ForegroundColor Yellow
        exit 1
    }
    Remove-Item $linkPath -Force
}

# SYMBOLIC_LINK_FLAG_ALLOW_UNPRIVILEGED_CREATE = 0x2
$ok = [TeaSymlink]::CreateSymbolicLink($linkPath, $targetPath, 0x2)
$err = [Runtime.InteropServices.Marshal]::GetLastWin32Error()
if (-not $ok -or -not (Test-Path $linkPath)) {
    Write-Host ("failed: win32={0} ({1})" -f $err, [ComponentModel.Win32Exception]::new($err).Message) -ForegroundColor Red
    Write-Host "no link was created. An elevated shell always works; scripts\deploy-local.ps1 copies instead." -ForegroundColor Red
    exit 1
}

$item = Get-Item $linkPath -Force
Write-Host ("linked {0} -> {1}" -f $item.FullName, $item.Target) -ForegroundColor Green
