@echo off
rem ---------------------------------------------------------------------------
rem tea_agent build helper (Windows / MSVC)
rem
rem   scripts\build.bat                 -> Release into .\build-msvc   (NMake)
rem   scripts\build.bat out ninja       -> Release into .\out          (Ninja)
rem
rem Works from a plain cmd.exe or PowerShell window: it sets up the MSVC
rem environment itself via scripts\vcenv.bat.
rem
rem NMake is the default because some Ninja builds (the "jobserver-pipe" variant
rem shipped by the `ninja` PyPI package) deadlock in restricted environments.
rem
rem For a GCC build on Windows use scripts\build-mingw.bat instead.
rem ---------------------------------------------------------------------------
setlocal
set "ROOT=%~dp0.."
pushd "%ROOT%" || exit /b 1

call "%~dp0vcenv.bat"
if errorlevel 1 (
  popd
  exit /b 1
)

set "BUILD_DIR=%ROOT%\build-msvc"
set "GEN=NMake Makefiles"
if not "%~1"=="" set "BUILD_DIR=%ROOT%\%~1"
if /i "%~2"=="ninja" set "GEN=Ninja"

cmake -S "%ROOT%" -B "%BUILD_DIR%" -G "%GEN%" -DCMAKE_BUILD_TYPE=Release
if errorlevel 1 (
  popd
  exit /b 1
)

cmake --build "%BUILD_DIR%"
if errorlevel 1 (
  popd
  exit /b 1
)

echo.
echo [build] ok -^> %BUILD_DIR%\bin\tea_agent.exe
popd
endlocal
