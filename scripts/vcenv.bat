@echo off
rem ---------------------------------------------------------------------------
rem scripts\vcenv.bat -- set up the MSVC x64 build environment.
rem
rem Usage (from any cmd.exe):   call scripts\vcenv.bat
rem
rem This does NOT use VS's VsDevCmd.bat/vcvars64.bat on purpose: those abort on
rem Visual Studio installations that are missing optional components (bundled
rem CMake, MSBuild, Connection Manager), leaving VCToolsInstallDir unset. The
rem toolset and Windows SDK layouts we rely on here are stable across VS 2017+.
rem
rem IMPORTANT: no `setlocal` - the whole point is to modify the caller's env.
rem ---------------------------------------------------------------------------

rem ---------------------------------------------------------------------------
rem Force English compiler diagnostics (VSLANG=1033).
rem
rem Two reasons, both practical:
rem   1. A localized cl.exe prints its /showIncludes lines with a localized
rem      prefix (on a Chinese VS the English "Note: including file:" becomes a
rem      Chinese phrase). CMake hands that prefix to
rem      `cmake -E cmake_cl_compile_depends --filter-prefix=...` through the
rem      generated makefile, where the non-ASCII bytes do not survive the
rem      console code page. The filter then matches nothing and every single
rem      included-file line is echoed into the build log - hundreds of lines
rem      per translation unit.
rem   2. English diagnostics are searchable and can be pasted into an issue.
rem Set VSLANG yourself before calling this script to keep your locale.
rem ---------------------------------------------------------------------------
if not defined VSLANG set "VSLANG=1033"

if not "%VCToolsInstallDir%"=="" (
  echo [vcenv] MSVC environment already initialised.
  goto :done
)

set "VSWHERE=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe"
set "VSPATH="
if exist "%VSWHERE%" for /f "usebackq tokens=*" %%i in (`"%VSWHERE%" -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath 2^>nul`) do set "VSPATH=%%i"
if not defined VSPATH if exist "%VSWHERE%" for /f "usebackq tokens=*" %%i in (`"%VSWHERE%" -latest -products * -property installationPath 2^>nul`) do set "VSPATH=%%i"
if not defined VSPATH if exist "%ProgramFiles%\Microsoft Visual Studio" for /d %%d in ("%ProgramFiles%\Microsoft Visual Studio\*") do for /d %%e in ("%%d\*") do if exist "%%e\VC\Tools\MSVC" set "VSPATH=%%e"
if not defined VSPATH if exist "%ProgramFiles(x86)%\Microsoft Visual Studio" for /d %%d in ("%ProgramFiles(x86)%\Microsoft Visual Studio\*") do for /d %%e in ("%%d\*") do if exist "%%e\VC\Tools\MSVC" set "VSPATH=%%e"

if not defined VSPATH (
  echo [vcenv] ERROR: no Visual Studio installation with the C++ toolset was found.
  echo [vcenv]        Install "Desktop development with C++" or run this from a
  echo [vcenv]        Developer Command Prompt.
  goto :fail
)

set "VCTOOLS="
for /f "delims=" %%v in ('dir /b /ad /o-n "%VSPATH%\VC\Tools\MSVC" 2^>nul') do if not defined VCTOOLS set "VCTOOLS=%VSPATH%\VC\Tools\MSVC\%%v"
if not defined VCTOOLS (
  echo [vcenv] ERROR: no MSVC toolset under "%VSPATH%\VC\Tools\MSVC".
  goto :fail
)

set "SDKROOT="
if exist "%ProgramFiles(x86)%\Windows Kits\10\Include" set "SDKROOT=%ProgramFiles(x86)%\Windows Kits\10"
if not defined SDKROOT if exist "%ProgramFiles%\Windows Kits\10\Include" set "SDKROOT=%ProgramFiles%\Windows Kits\10"

set "SDKVER="
if defined SDKROOT for /f "delims=" %%v in ('dir /b /ad /o-n "%SDKROOT%\Include" 2^>nul') do if not defined SDKVER set "SDKVER=%%v"

set "HOSTARCH=x64"
if /i "%PROCESSOR_ARCHITECTURE%"=="ARM64" set "HOSTARCH=arm64"

set "PATH=%VCTOOLS%\bin\Host%HOSTARCH%\x64;%PATH%"
set "INCLUDE=%VCTOOLS%\include"
set "LIB=%VCTOOLS%\lib\x64"

if defined SDKVER (
  set "PATH=%SDKROOT%\bin\%SDKVER%\x64;%PATH%"
  set "INCLUDE=%INCLUDE%;%SDKROOT%\Include\%SDKVER%\ucrt;%SDKROOT%\Include\%SDKVER%\shared;%SDKROOT%\Include\%SDKVER%\um;%SDKROOT%\Include\%SDKVER%\winrt"
  set "LIB=%LIB%;%SDKROOT%\Lib\%SDKVER%\ucrt\x64;%SDKROOT%\Lib\%SDKVER%\um\x64"
) else (
  echo [vcenv] WARNING: no Windows 10/11 SDK Include directory found.
)

set "VCToolsInstallDir=%VCTOOLS%\"
echo [vcenv] toolset : %VCTOOLS%
if defined SDKVER echo [vcenv] winsdk  : %SDKROOT%\%SDKVER%

where cl >nul 2>nul
if errorlevel 1 (
  echo [vcenv] ERROR: cl.exe is still not on PATH after setup.
  goto :fail
)

:done
exit /b 0

:fail
exit /b 1
