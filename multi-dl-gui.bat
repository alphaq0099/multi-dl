@echo off
setlocal

:: Determine the directory where this script and MULTI_DOWNLOADER are located
set "PROJECT_DIR=%~dp0"

:: Prefer the project virtual environment Python if available
if exist "%PROJECT_DIR%.venv\Scripts\python.exe" (
    set "PYTHON_BIN=%PROJECT_DIR%.venv\Scripts\python.exe"
) else (
    set "PYTHON_BIN=python"
)

:: Launch multi-dl-gui.py
start "" "%PYTHON_BIN%" "%PROJECT_DIR%multi-dl-gui.py" %*

endlocal
