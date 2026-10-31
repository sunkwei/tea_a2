@echo off
rem ---------------------------------------------------------------------------
rem grant-low-write.cmd -- let a sandboxed agent write where it needs to.
rem
rem   Right-click -> "Run as administrator", then accept the UAC prompt.
rem
rem Why: a sandbox (or any low-integrity process) may read a directory and
rem still not be allowed to write there - "Windows error 5: Access is denied" -
rem even though the ACL says the account has full control.  What blocks it is
rem the integrity level: Windows refuses a write from a low-integrity process
rem to a medium-integrity object ("no write up").
rem
rem   * the label of the directories is lowered to Low, so a low-integrity
rem     process may write there;
rem   * the low-integrity SID gets modify rights there, because a directory
rem     that only had rights for your account would still refuse the sandbox.
rem
rem   The agent itself keeps running as your normal account - only this helper
rem   is elevated, and only for the length of this script.
rem
rem   grant-low-write.cmd                  -> the tea home and the project tree
rem   grant-low-write.cmd D:\some\dir ...  -> these directories instead
rem
rem The other way out is to start the agent with normal integrity; see
rem scripts/start-local.ps1.
rem ---------------------------------------------------------------------------
setlocal
if "%~1"=="" (
  set "TARGETS=%USERPROFILE%\.tea_agent %USERPROFILE%\work\git"
) else (
  set "TARGETS=%*"
)

echo [grant] opening these directories for low-integrity processes:
for %%T in (%TARGETS%) do echo           %%~T
echo.

for %%T in (%TARGETS%) do (
  if not exist "%%~T" (
    echo [grant] skip %%~T - not there
  ) else (
    echo [grant] %%~T
    icacls "%%~T" /setintegritylevel "(OI)(CI)L" /T /Q >nul
    icacls "%%~T" /grant "S-1-16-4096:(OI)(CI)(M)" /T /Q >nul
    icacls "%%~T" | findstr /i "Mandatory Label"
  )
)

echo.
echo [grant] done - restart the agent and the workspace list should say writable.
echo [grant] (the agent keeps running as your own account - nothing is elevated)
pause
