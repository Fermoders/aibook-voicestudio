@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
  echo AIBook is not installed. Run install_aibook.cmd first.
  pause
  exit /b 1
)
start "AIBook XTTS" ".venv\Scripts\pythonw.exe" "%~dp0main.py"
