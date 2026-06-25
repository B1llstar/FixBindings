@echo off
:: Run as Administrator is required for low-level keyboard hooks in games
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Requesting administrator privileges...
    powershell -Command "Start-Process '%~f0' -Verb RunAs"
    exit /b
)

cd /d "%~dp0"

:: Install dependencies if needed
pip show keyboard >nul 2>&1 || pip install -r requirements.txt

python fix_bindings.py
pause
