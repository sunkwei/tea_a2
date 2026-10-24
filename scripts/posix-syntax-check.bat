@echo off
rem ---------------------------------------------------------------------------
rem Syntax-check a POSIX-only source file on a Windows machine.
rem
rem The POSIX half of the code (pty, fork, poll...) cannot be compiled here:
rem there is no Linux toolchain and no WSL distro. This gives the compiler
rem just enough of the POSIX world to *parse* the file - the point is to catch
rem typos, wrong argument counts and missing includes, not to run anything.
rem
rem   scripts\posix-syntax-check.bat src\shell_posix.cpp
rem
rem The stub headers live in scripts\posix_stub\ next to this script.
rem ---------------------------------------------------------------------------
setlocal
set "ROOT=%~dp0.."
pushd "%ROOT%" || exit /b 1

set "MINGW=%ROOT%\.toolchain\mingw64"
if defined MINGW_HOME if exist "%MINGW_HOME%\bin\g++.exe" set "MINGW=%MINGW_HOME%"
if defined MINGW set "PATH=%MINGW%\bin;%PATH%"
where g++ >nul 2>nul
if errorlevel 1 (
  echo [posix-check] g++ not found; install MinGW-w64 or set MINGW_HOME
  popd
  exit /b 1
)

set "TEMP=%ROOT%\.tmp"
set "TMP=%ROOT%\.tmp"
if not exist "%TEMP%" mkdir "%TEMP%" 2>nul

set "SRC=%~1"
if "%SRC%"=="" set "SRC=src\shell_posix.cpp"

g++ -std=c++17 -fsyntax-only -Wall -Wextra -Wno-unused-parameter ^
    -include "%ROOT%\scripts\posix_stub\prelude.h" ^
    -I "%ROOT%\scripts\posix_stub" ^
    -I "%ROOT%\src" -I "%ROOT%\3rd\nlohmann_json" ^
    "%ROOT%\%SRC%"
if errorlevel 1 (
  echo [posix-check] FAILED
  popd
  exit /b 1
)
echo [posix-check] %SRC% parses cleanly
popd
endlocal
