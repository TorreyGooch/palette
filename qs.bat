@echo off
setlocal
set "PYTHONPATH=%~dp0;%PYTHONPATH%"
set "PY_EXE="
where py >nul 2>nul && set "PY_EXE=py"
if not defined PY_EXE (
  for /d %%D in ("%LocalAppData%\Python\pythoncore-*") do (
    if exist "%%D\python.exe" set "PY_EXE=%%D\python.exe"
  )
)
if not defined PY_EXE set "PY_EXE=python"
"%PY_EXE%" -m quotesource %*
