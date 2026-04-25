@echo off
setlocal
cd /d "%~dp0"

REM Use the active python on PATH unless a venv is set up alongside the project.
if exist "venv\Scripts\python.exe" (
    set "PY=venv\Scripts\python.exe"
) else (
    set "PY=python"
)

echo [pingpong-studio] starting on http://127.0.0.1:8765
start "" "http://127.0.0.1:8765"
"%PY%" -m backend.server
endlocal
