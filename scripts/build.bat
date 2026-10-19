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

rem ---------------------------------------------------------------------------
rem Check that cl.exe reports includes in English.
rem
rem CMake hands the /showIncludes prefix to
rem     cmake -E cmake_cl_compile_depends --filter-prefix=<prefix>
rem on every compile line. A localized prefix does not survive the console code
rem page, so nothing matches and every included header is echoed into the log
rem (and the .obj.d files stay empty). vcenv.bat sets VSLANG=1033 to make cl.exe
rem speak English; this probe proves it took effect on this machine instead of
rem leaving you to wonder why the log is full of included-file lines.
rem ---------------------------------------------------------------------------
set "PROBE_C=%TEMP%\tea_include_probe.c"
set "PROBE_OBJ=%TEMP%\tea_include_probe.obj"
set "PROBE_LOG=%TEMP%\tea_include_probe.log"
> "%PROBE_C%" echo #include ^<stdio.h^>
>> "%PROBE_C%" echo int main(void) { return 0; }
cl /nologo /showIncludes /c /Fo"%PROBE_OBJ%" "%PROBE_C%" > "%PROBE_LOG%" 2>&1
set "SHOWINCLUDES_EN="
findstr /c:"Note: including file" "%PROBE_LOG%" >nul 2>nul && set "SHOWINCLUDES_EN=1"
if not defined SHOWINCLUDES_EN (
  echo [build] WARNING: cl.exe does not report includes in English on this machine.
  echo [build]          The build log will echo every included header, because CMake
  echo [build]          cannot filter a localized /showIncludes prefix. Set VSLANG=1033
  echo [build]          in the environment ^(vcenv.bat tries to^) and reconfigure.
)
del "%PROBE_C%" "%PROBE_OBJ%" "%PROBE_LOG%" >nul 2>nul

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
