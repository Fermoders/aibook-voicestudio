param([string]$Destination = "")

$ErrorActionPreference = "Stop"
$workspace = [IO.Path]::GetFullPath($PSScriptRoot).TrimEnd('\')
$dist = [IO.Path]::GetFullPath((Join-Path $workspace "dist")).TrimEnd('\')
if (-not $Destination) { $Destination = Join-Path $dist "AIBook VoiceStudio Ready" }
$target = [IO.Path]::GetFullPath($Destination).TrimEnd('\')
if (-not $target.StartsWith($dist + '\', [StringComparison]::OrdinalIgnoreCase)) {
    throw "Destination must remain inside $dist"
}
$running = @(Get-CimInstance Win32_Process | Where-Object {
    $_.ExecutablePath -and $_.ExecutablePath.StartsWith($target + '\', [StringComparison]::OrdinalIgnoreCase)
})
if ($running.Count) {
    throw "Destination is in use. Choose another -Destination or close that application. Running processes were preserved."
}
$python = Join-Path $workspace ".voice-venv\Scripts\python.exe"
$pythonBase = (& $python -c "import sys; from pathlib import Path; print(Path(sys._base_executable).parent)").Trim()
if ($LASTEXITCODE -ne 0) { throw "VoiceStudio environment is unavailable." }
& $python (Join-Path $workspace "voice_studio_main.py") --doctor
if ($LASTEXITCODE -ne 0) { throw "Source environment failed its health check." }

function Copy-Tree {
    param([string]$Source, [string]$Target, [string[]]$SkipDirectories = @(), [string[]]$SkipFiles = @())
    New-Item -ItemType Directory -Force -Path $Target | Out-Null
    $arguments = @($Source, $Target, "/E", "/COPY:DAT", "/DCOPY:DAT", "/R:2", "/W:1", "/NFL", "/NDL", "/NJH", "/NJS", "/NP")
    if ($SkipDirectories.Count) { $arguments += "/XD"; $arguments += $SkipDirectories }
    if ($SkipFiles.Count) { $arguments += "/XF"; $arguments += $SkipFiles }
    & robocopy @arguments | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "Copy failed: $Source -> $Target" }
}

$runtime = Join-Path $target "runtime\python"
New-Item -ItemType Directory -Force -Path $runtime | Out-Null
foreach ($file in @("python.exe", "pythonw.exe", "python3.dll", "python311.dll", "vcruntime140.dll", "vcruntime140_1.dll", "LICENSE.txt")) {
    Copy-Item -LiteralPath (Join-Path $pythonBase $file) -Destination $runtime -Force
}
Copy-Tree (Join-Path $pythonBase "DLLs") (Join-Path $runtime "DLLs") -SkipDirectories @("__pycache__")
Copy-Tree (Join-Path $pythonBase "tcl") (Join-Path $runtime "tcl")
Copy-Tree (Join-Path $pythonBase "Lib") (Join-Path $runtime "Lib") -SkipDirectories @("site-packages", "__pycache__")
# Static-link development libraries are not used by inference (about 2.6 GiB).
Copy-Tree (Join-Path $workspace ".voice-venv\Lib\site-packages") (Join-Path $runtime "Lib\site-packages") -SkipDirectories @("__pycache__") -SkipFiles @("*.pyc", "*.pyo", "*.lib", "_virtualenv.py", "_virtualenv.pth")
Copy-Item -LiteralPath (Join-Path $workspace "portable\python311._pth") -Destination $runtime -Force

$app = Join-Path $target "app"
Copy-Tree (Join-Path $workspace "aibook") (Join-Path $app "aibook") -SkipDirectories @("__pycache__") -SkipFiles @("*.pyc")
foreach ($file in @("main.py", "voice_studio_main.py", "requirements-voicestudio.txt")) {
    Copy-Item -LiteralPath (Join-Path $workspace $file) -Destination $app -Force
}
$vendor = Join-Path $app "vendor\VoiceStudio"
Copy-Tree (Join-Path $workspace "vendor\VoiceStudio\omnivoice") (Join-Path $vendor "omnivoice") -SkipDirectories @("__pycache__") -SkipFiles @("*.pyc")
foreach ($file in @("LICENSE", "LICENSE-NOTICE.md", "pyproject.toml", "README.md")) {
    Copy-Item -LiteralPath (Join-Path $workspace "vendor\VoiceStudio\$file") -Destination $vendor -Force
}
$model = Join-Path $target "data\models\OmniVoice"
Copy-Tree (Join-Path $workspace "data\models\OmniVoice") $model -SkipDirectories @(".cache") -SkipFiles @(".install.lock")
Copy-Tree (Join-Path $workspace "data\models\Whisper") (Join-Path $target "data\models\Whisper") -SkipDirectories @(".cache")
Copy-Tree (Join-Path $workspace "examples") (Join-Path $target "examples")
New-Item -ItemType Directory -Force -Path (Join-Path $target "data\voicestudio"), (Join-Path $target "outputs") | Out-Null
# Only generated presets are included. User references, drafts and history are not packaged.
if (Test-Path -LiteralPath (Join-Path $workspace "data\voicestudio\voices")) {
    $voices = Join-Path $target "data\voicestudio\voices"
    New-Item -ItemType Directory -Force -Path $voices | Out-Null
    $presetKeys = & $python -c "import hashlib; from aibook.voice_studio_config import PRESETS, MODEL_REVISION; [print('omnivoice_' + hashlib.sha256((name + '|' + MODEL_REVISION + '|preset-v1').encode()).hexdigest()[:32] + '.pt') for name in PRESETS]"
    foreach ($name in $presetKeys) {
        $source = Join-Path $workspace "data\voicestudio\voices\$name"
        if (Test-Path -LiteralPath $source) { Copy-Item -LiteralPath $source -Destination $voices -Force }
    }
}

$ffmpegTarget = Join-Path $target "runtime\ffmpeg"
New-Item -ItemType Directory -Force -Path $ffmpegTarget | Out-Null
foreach ($name in @("ffmpeg", "ffprobe")) {
    $command = Get-Command "$name.exe" -ErrorAction Stop
    Copy-Item -LiteralPath $command.Source -Destination (Join-Path $ffmpegTarget "$name.exe") -Force
}
$ffmpegPackage = (Get-Item -LiteralPath (Get-Command ffmpeg.exe).Source).Target
if ($ffmpegPackage) {
    $ffmpegRoot = Split-Path (Split-Path $ffmpegPackage -Parent) -Parent
    if (Test-Path -LiteralPath (Join-Path $ffmpegRoot "LICENSE")) {
        Copy-Item -LiteralPath (Join-Path $ffmpegRoot "LICENSE") -Destination (Join-Path $ffmpegTarget "LICENSE.txt") -Force
    }
}
Copy-Item -LiteralPath (Join-Path $workspace "README-VoiceStudio.md") -Destination (Join-Path $target "README.md") -Force
Copy-Item -LiteralPath (Join-Path $workspace "NOTICE-VoiceStudio.txt") -Destination (Join-Path $target "NOTICE.txt") -Force
$licenses = Join-Path $target "licenses"
New-Item -ItemType Directory -Force -Path $licenses | Out-Null
Copy-Item -LiteralPath (Join-Path $vendor "LICENSE") -Destination (Join-Path $licenses "VoiceStudio-AGPL-3.0.txt") -Force
foreach ($package in @(@("openai_whisper-20250625", "Whisper-MIT.txt"), @("stable_ts-2.19.1", "Stable-ts-MIT.txt"), @("miniaudio-1.71", "Miniaudio-MIT.txt"))) {
    Copy-Item -LiteralPath (Join-Path $workspace ".voice-venv\Lib\site-packages\$($package[0]).dist-info\licenses\LICENSE") -Destination (Join-Path $licenses $package[1]) -Force
}
Copy-Item -LiteralPath (Join-Path $workspace "aibook\assets\icons\LICENSE.txt") -Destination (Join-Path $licenses "Lucide-ISC.txt") -Force
Copy-Item -LiteralPath (Join-Path $model "audio_tokenizer\LICENSE") -Destination (Join-Path $licenses "Boson-Higgs-Audio-2.txt") -Force
Invoke-WebRequest -Uri "https://raw.githubusercontent.com/meta-llama/llama3/main/LICENSE" -OutFile (Join-Path $licenses "Meta-Llama-3.txt")
Invoke-WebRequest -Uri "https://www.apache.org/licenses/LICENSE-2.0.txt" -OutFile (Join-Path $licenses "Apache-2.0.txt")
Invoke-WebRequest -Uri "https://creativecommons.org/licenses/by-nc/4.0/legalcode.txt" -OutFile (Join-Path $licenses "CC-BY-NC-4.0.txt")

$source = Join-Path $target "source"
New-Item -ItemType Directory -Force -Path $source | Out-Null
foreach ($file in @("build_voicestudio_portable.ps1", "install_voicestudio.ps1", "install-windows.ps1", "download_voicestudio_models.py")) {
    Copy-Item -LiteralPath (Join-Path $workspace $file) -Destination $source -Force
}
Copy-Item -LiteralPath (Join-Path $workspace "portable\VoiceStudioLauncher.cs") -Destination $source -Force
Copy-Item -LiteralPath (Join-Path $workspace "portable\python311._pth") -Destination $source -Force
Copy-Tree (Join-Path $workspace "tests") (Join-Path $source "tests") -SkipDirectories @("__pycache__") -SkipFiles @("*.pyc")
Copy-Tree (Join-Path $workspace "scripts") (Join-Path $source "scripts") -SkipDirectories @("__pycache__") -SkipFiles @("*.pyc")
& $python -c "from PIL import Image; Image.open(r'vendor/VoiceStudio/docs/logo.png').save(r'$target\AIBook.ico', sizes=[(16,16),(32,32),(48,48),(256,256)])"
if ($LASTEXITCODE -ne 0) { throw "Icon creation failed." }
$compiler = Join-Path $env:WINDIR "Microsoft.NET\Framework64\v4.0.30319\csc.exe"
& $compiler /nologo /target:winexe /platform:x64 /reference:System.Windows.Forms.dll "/win32icon:$target\AIBook.ico" "/out:$target\AIBook VoiceStudio.exe" (Join-Path $source "VoiceStudioLauncher.cs")
if ($LASTEXITCODE -ne 0) { throw "Launcher compilation failed." }
$packages = & $python -c "import importlib.metadata as m, json; print(json.dumps({p: m.version(p) for p in ['torch', 'torchaudio', 'transformers', 'accelerate', 'soundfile', 'miniaudio', 'stable-ts', 'openai-whisper']}, indent=2))"
$resolved = & $python -c "import importlib.metadata as m; print('\n'.join(sorted(d.metadata['Name'] + '==' + d.version for d in m.distributions())))"
[IO.File]::WriteAllLines((Join-Path $target "RESOLVED_PACKAGES.txt"), $resolved)
$buildInfo = @("Built UTC: $([DateTime]::UtcNow.ToString('o'))", "VoiceStudio: v0.5.6", "Source commit: 3915a62cb482bb43117aaebab57d22745bd1cc21", "Model revision: c5fdb5ccb189668d56333f77ba2629f4cd7535f4", $packages)
[IO.File]::WriteAllLines((Join-Path $target "BUILD_INFO.txt"), $buildInfo)
Write-Host "Built: $target"
Write-Host "Start: $(Join-Path $target 'AIBook VoiceStudio.exe')"
