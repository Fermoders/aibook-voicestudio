@echo off
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
if not exist ".voice-venv\Scripts\pythonw.exe" (
    echo Run install_voicestudio.ps1 first.
    pause
    exit /b 1
)
start "AIBook VoiceStudio" /D "%~dp0" ".voice-venv\Scripts\pythonw.exe" "voice_studio_main.py"
