@echo off
rem Serve PALETTE on this machine's Tailscale address only — reachable from
rem your other tailnet devices, not from local wi-fi or the internet.
cd /d "%~dp0"

set "PALETTE_HOST=tailscale"

rem Same corpus wiring as launch.bat — see the comment there.
if "%PALETTE_LOCAL%"=="1" (
  set "QS_REMOTE="
) else (
  if not defined QS_REMOTE set "QS_REMOTE=http://100.102.79.115:7862"
)

set "PY_EXE="
where py >nul 2>nul && set "PY_EXE=py"
if not defined PY_EXE (
  for /d %%D in ("%LocalAppData%\Python\pythoncore-*") do (
    if exist "%%D\python.exe" set "PY_EXE=%%D\python.exe"
  )
)
if not defined PY_EXE set "PY_EXE=python"

"%PY_EXE%" run.py
pause
