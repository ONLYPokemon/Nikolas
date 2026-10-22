@echo off
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo Python is not installed. Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH" during setup.
  pause
  exit /b
)
python server.py
pause
