param([switch]$SkipModels)

$ErrorActionPreference = "Stop"
$workspace = [IO.Path]::GetFullPath($PSScriptRoot)
$env:UV_CACHE_DIR = Join-Path $workspace ".cache\uv"
$env:HF_HOME = Join-Path $workspace ".cache\huggingface"
$env:HF_HUB_DISABLE_TELEMETRY = "1"
$python = Join-Path $workspace ".voice-venv\Scripts\python.exe"
$vendor = Join-Path $workspace "vendor\VoiceStudio"
$commit = "3915a62cb482bb43117aaebab57d22745bd1cc21"
if (-not (Test-Path -LiteralPath $vendor)) {
    & git clone --depth 1 --branch v0.5.6 https://github.com/debpalash/VoiceStudio.git $vendor
    if ($LASTEXITCODE -ne 0) { throw "Could not obtain VoiceStudio source." }
}
$actual = (& git -C $vendor rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or $actual -ne $commit) {
    throw "VoiceStudio checkout is not the expected pinned revision. Existing files were preserved."
}

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw "uv is required to prepare the development environment."
}
if (-not (Test-Path -LiteralPath $python)) {
    & uv venv --python 3.11 --no-python-downloads (Join-Path $workspace ".voice-venv")
    if ($LASTEXITCODE -ne 0) { throw "Could not create the Python 3.11 environment." }
}
& uv pip install --python $python --index-url https://download.pytorch.org/whl/cu128 torch==2.8.0 torchaudio==2.8.0
if ($LASTEXITCODE -ne 0) { throw "Could not install the CUDA runtime." }
& uv pip install --python $python -r (Join-Path $workspace "requirements-voicestudio.txt")
if ($LASTEXITCODE -ne 0) { throw "Could not install VoiceStudio dependencies." }
if (-not $SkipModels) {
    & $python (Join-Path $workspace "download_voicestudio_models.py")
    if ($LASTEXITCODE -ne 0) { throw "Model preparation failed." }
}
Write-Host "AIBook VoiceStudio is ready. Run start_voicestudio.cmd."
