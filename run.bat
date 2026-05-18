@echo off
setlocal
cd /d "%~dp0"

REM Each launch kills any process already listening on the configured
REM port, so double-clicking run.bat always replaces the running server
REM with a fresh one (picks up code changes without manual Ctrl+C).
set PORT=8766

echo [table-tennis-studio] looking for old server on port %PORT% ...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":%PORT% " ^| findstr "LISTENING"') do (
    echo   killing PID %%a
    taskkill /F /PID %%a >nul 2>&1
)

REM Always clear caches on startup so behaviour is reproducible: stale
REM refframes (different YOLO model output, different ROI corners) and
REM stale Python bytecode (old code with the same .pyc name) are the
REM two most common causes of "I just changed code but nothing
REM updated" symptoms.
echo [table-tennis-studio] clearing caches (refframes + __pycache__) ...
if exist "temp\refframes" rmdir /S /Q "temp\refframes" >nul 2>&1
for /d /r %%d in (__pycache__) do (
    if exist "%%d" rmdir /S /Q "%%d" >nul 2>&1
)

REM Use the project's venv if present, otherwise fall back to whatever
REM python is on PATH.
if exist "venv\Scripts\python.exe" (
    set "PY=venv\Scripts\python.exe"
) else (
    set "PY=python"
)

echo [table-tennis-studio] starting on http://127.0.0.1:%PORT%
start "" "http://127.0.0.1:%PORT%"
"%PY%" -m backend.server
endlocal
