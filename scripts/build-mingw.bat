@echo off
rem ---------------------------------------------------------------------------
rem tea_agent build helper (MinGW-w64 / GCC / Ninja)
rem
rem   scripts\build-mingw.bat              -> Release build into .\build-mingw
rem   scripts\build-mingw.bat build-gcc14  -> build into .\build-gcc14
rem
rem The toolchain is looked up in this order:
rem   1. %MINGW_HOME%
rem   2. .toolchain\mingw64      (see tools\install-mingw.bat)
rem   3. g++ already on PATH
rem ---------------------------------------------------------------------------
setlocal
set "ROOT=%~dp0.."
pushd "%ROOT%" || exit /b 1

set "MINGW="
if defined MINGW_HOME if exist "%MINGW_HOME%\bin\g++.exe" set "MINGW=%MINGW_HOME%"
if not defined MINGW if exist "%ROOT%\.toolchain\mingw64\bin\g++.exe" set "MINGW=%ROOT%\.toolchain\mingw64"
if defined MINGW set "PATH=%MINGW%\bin;%PATH%"

where g++ >nul 2>nul
if errorlevel 1 (
  echo [mingw] g++ not found. Install MinGW-w64, or set MINGW_HOME, or run:
  echo [mingw]   node tools\fetch.mjs ^<winlibs-zip-url^> .toolchain\mingw64.zip
  echo [mingw]   tar -xf .toolchain\mingw64.zip -C .toolchain
  popd
  exit /b 1
)

for /f "delims=" %%v in ('g++ -dumpversion') do set "GCCVER=%%v"
echo [mingw] using g++ %GCCVER%

set "BUILD_DIR=%ROOT%\build-mingw"
if not "%~1"=="" set "BUILD_DIR=%ROOT%\%~1"

cmake -S "%ROOT%" -B "%BUILD_DIR%" -G Ninja -DCMAKE_BUILD_TYPE=Release ^
      -DCMAKE_C_COMPILER=gcc -DCMAKE_CXX_COMPILER=g++
if errorlevel 1 (
  popd
  exit /b 1
)

cmake --build "%BUILD_DIR%" --parallel
if errorlevel 1 (
  popd
  exit /b 1
)

echo.
echo [mingw] ok -^> %BUILD_DIR%\bin\tea_agent.exe
popd
endlocal
