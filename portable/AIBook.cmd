@echo off
setlocal
set "ROOT=%~dp0"
cd /d "%ROOT%"

set "PYTHONHOME=%ROOT%runtime\python"
set "PYTHONPATH=%ROOT%app"
set "PYTHONNOUSERSITE=1"
set "AIBOOK_ROOT=%ROOT%"
set "AIBOOK_DATA_DIR=%ROOT%data\aibook"
set "AIBOOK_OUTPUT_DIR=%ROOT%outputs"
set "TTS_HOME=%ROOT%data"
set "HF_HOME=%ROOT%data\huggingface"
set "TORCH_HOME=%ROOT%data\torch"
set "PATH=%ROOT%runtime\ffmpeg;%ROOT%runtime\python;%ROOT%runtime\python\DLLs;%PATH%"

if not exist "%ROOT%runtime\python\pythonw.exe" (
  echo Portable Python runtime is missing.
  pause
  exit /b 1
)

if not exist "%ROOT%data\tts\tts_models--multilingual--multi-dataset--xtts_v2\model.pth" (
  echo XTTS v2 model is missing.
  pause
  exit /b 1
)

start "AIBook XTTS" /D "%ROOT%" "%ROOT%runtime\python\pythonw.exe" "%ROOT%app\main.py"
