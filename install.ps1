param(
    [switch]$AcceptNonCommercialLicense
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

$python = Get-Command py -ErrorAction Stop
if (-not (Test-Path -LiteralPath ".venv\Scripts\python.exe")) {
    & $python.Source -3.11 -m venv .venv
}

& ".venv\Scripts\python.exe" -m pip install --upgrade pip
& ".venv\Scripts\python.exe" -m pip install torch==2.11.0+cu128 torchaudio==2.11.0+cu128 --index-url https://download.pytorch.org/whl/cu128
& ".venv\Scripts\python.exe" -m pip install -r requirements.txt

if ($AcceptNonCommercialLicense) {
    & ".venv\Scripts\python.exe" main.py --accept-cpml
}

& ".venv\Scripts\python.exe" -c "from TTS.api import TTS; import torch; print('XTTS dependencies OK; CUDA:', torch.cuda.is_available())"
Write-Host "Installation complete. Run start_aibook.cmd"
