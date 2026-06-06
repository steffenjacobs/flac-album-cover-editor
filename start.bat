@echo off
REM FLAC Album Cover Editor launcher (Windows).
REM Creates a local virtualenv on first run, installs deps, starts the server,
REM and opens the UI in your browser.

setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment...
    py -3 -m venv .venv || python -m venv .venv
    if errorlevel 1 (
        echo Failed to create venv. Is Python 3 installed and on PATH?
        pause
        exit /b 1
    )
    echo Installing dependencies...
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
)

echo Starting FLAC Album Cover Editor at http://127.0.0.1:8765
start "" "http://127.0.0.1:8765"
".venv\Scripts\python.exe" server.py

endlocal
