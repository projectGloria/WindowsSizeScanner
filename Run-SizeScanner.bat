@echo off
setlocal
cd /d "%~dp0"

:: Check for pythonw to run without console window, fallback to python
where pythonw >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    start "" pythonw main.py %*
) else (
    start "" python main.py %*
)
