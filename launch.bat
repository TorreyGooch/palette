@echo off
cd /d "%~dp0"

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

"%PY_EXE%" run.py
pause
