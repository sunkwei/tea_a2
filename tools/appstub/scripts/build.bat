@echo off
rem ---------------------------------------------------------------------------
rem build.bat -- build appstub with MSVC (Windows), inside the tea_a2 tree.
rem
rem   scripts\build.bat              -> Release into <repo>\build-msvc  (NMake)
rem   scripts\build.bat out ninja    -> Release into <repo>\out         (Ninja)
rem
rem Only the appstub target is built; the agent itself is built by the
rem repository's own scripts\build.bat.
rem ---------------------------------------------------------------------------
setlocal
set "ROOT=%~dp0..\.."
pushd "%ROOT%" || exit /b 1

call "%ROOT%\scripts\vcenv.bat"
if errorlevel 1 (
  popd
  exit /b 1
)

set "VSLANG=1033"

set "BUILD_DIR=%ROOT%\build-msvc"
set "GEN=NMake Makefiles"
if not "%~1"=="" set "BUILD_DIR=%ROOT%\%~1"
if /i "%~2"=="ninja" set "GEN=Ninja"

cmake -S "%ROOT%" -B "%BUILD_DIR%" -G "%GEN%" -DCMAKE_BUILD_TYPE=Release
if errorlevel 1 (
  popd
  exit /b 1
)

cmake --build "%BUILD_DIR%" --target appstub
if errorlevel 1 (
  popd
  exit /b 1
)

echo.
echo [build] ok -^> %BUILD_DIR%\bin\appstub.exe
popd
endlocal
