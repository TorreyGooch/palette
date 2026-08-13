@echo off
cd /d "%~dp0"

rem The corpus lives on the GPU server; this machine keeps the media. Without
rem this, the Quotes page reports an empty corpus instead of saying it moved.
rem Set PALETTE_LOCAL=1 to run fully self-contained (needs a local corpus).
if "%PALETTE_LOCAL%"=="1" (
  set "QS_REMOTE="
) else (
  if not defined QS_REMOTE set "QS_REMOTE=http://100.102.79.115:7862"
)

rem The Microsoft Store "python" alias shadows the real install in cmd.exe,
rem so try the py launcher first, then a known install path, then plain python.
set "PY_EXE="

where py >nul 2>nul && set "PY_EXE=py"

if not defined PY_EXE (
  if exist "%LocalAppData%\Python\pythoncore-3.14-64\python.exe" (
    set "PY_EXE=%LocalAppData%\Python\pythoncore-3.14-64\python.exe"
  )
)

if not defined PY_EXE (
  for /d %%D in ("%LocalAppData%\Python\pythoncore-*") do (
    if exist "%%D\python.exe" set "PY_EXE=%%D\python.exe"
  )
)

if not defined PY_EXE set "PY_EXE=python"

rem Say which corpus this window is talking to. Two palette instances that
rem look identical but hold different libraries is an easy way to get lost.
if defined QS_REMOTE (
  echo Corpus: %QS_REMOTE%  ^(remote^)
) else (
  echo Corpus: local
)
echo Media:  this machine
echo.

"%PY_EXE%" run.py
pause
