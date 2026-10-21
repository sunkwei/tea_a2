@echo off
rem ---------------------------------------------------------------------------
rem tea_agent autostart helper (Windows cmd wrapper)
rem
rem   scripts\install-autostart.bat              register "start at logon"
rem   scripts\install-autostart.bat -Start       register + run now
rem   scripts\install-autostart.bat -Status      is it registered?
rem   scripts\install-autostart.bat -Uninstall   remove it
rem   scripts\install-autostart.bat -DryRun      show what would be registered
rem
rem Anything else on the command line is passed to tea_agent verbatim, e.g.
rem   scripts\install-autostart.bat -Start --port 9000 --workspace C:\work\proj
rem
rem See scripts\install-autostart.ps1 for the details; this is only a shim so
rem cmd.exe users do not have to remember the pwsh invocation.
rem ---------------------------------------------------------------------------
setlocal
set "SCRIPT=%~dp0install-autostart.ps1"
if not exist "%SCRIPT%" (
  echo [autostart] missing "%SCRIPT%"
  exit /b 1
)

where pwsh >nul 2>nul
if errorlevel 1 (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT%" %*
) else (
  pwsh -NoProfile -File "%SCRIPT%" %*
)
exit /b %errorlevel%
