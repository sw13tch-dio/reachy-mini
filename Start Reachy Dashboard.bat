@echo off
title Reachy Mini Dashboard
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo.
    echo   Python is not installed on this computer.
    echo.
    echo   Install it from https://www.python.org/downloads/
    echo   IMPORTANT: tick "Add Python to PATH" during setup.
    echo.
    pause
    exit /b 1
)

python -c "import bleak" >nul 2>nul
if errorlevel 1 (
    echo   First run - installing one small add-on ^(bleak^)...
    echo   This only happens once.
    echo.
    python -m pip install --quiet bleak
)

python reachy_dash.py %*

echo.
echo   The dashboard has stopped.
pause
