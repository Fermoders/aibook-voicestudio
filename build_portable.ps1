param(
    [string]$Destination = ""
)

$ErrorActionPreference = "Stop"
$workspace = [IO.Path]::GetFullPath($PSScriptRoot).TrimEnd('\')
$distRoot = [IO.Path]::GetFullPath((Join-Path $workspace "dist")).TrimEnd('\')
if (-not $Destination) {
    $Destination = Join-Path $distRoot "AIBook Portable"
}
$destinationPath = [IO.Path]::GetFullPath($Destination).TrimEnd('\')
$allowedPrefix = $distRoot + [IO.Path]::DirectorySeparatorChar
if (-not $destinationPath.StartsWith($allowedPrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Destination must stay inside $distRoot"
}

$pythonSource = "C:\Users\Fer\AppData\Local\Programs\Python\Python311"
$sitePackagesSource = Join-Path $workspace ".venv\Lib\site-packages"
$modelSource = "C:\Users\Fer\AppData\Local\tts\tts_models--multilingual--multi-dataset--xtts_v2"
$ffmpegSource = "C:\Users\Fer\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-8.1.1-full_build"

foreach ($required in @($pythonSource, $sitePackagesSource, $modelSource, $ffmpegSource)) {
    if (-not (Test-Path -LiteralPath $required)) {
        throw "Required source is missing: $required"
    }
}

if (Test-Path -LiteralPath $destinationPath) {
    $resolvedDestination = (Resolve-Path -LiteralPath $destinationPath).Path
    if (-not $resolvedDestination.StartsWith($allowedPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to remove path outside dist: $resolvedDestination"
    }
    Remove-Item -LiteralPath $resolvedDestination -Recurse -Force
}

New-Item -ItemType Directory -Force -Path $destinationPath | Out-Null

function Copy-Tree {
    param(
        [Parameter(Mandatory)][string]$Source,
        [Parameter(Mandatory)][string]$Target,
        [string[]]$ExcludeDirectories = @(),
        [string[]]$ExcludeFiles = @()
    )

    New-Item -ItemType Directory -Force -Path $Target | Out-Null
    $arguments = @(
        $Source,
        $Target,
        "/E",
        "/COPY:DAT",
        "/DCOPY:DAT",
        "/R:2",
        "/W:1",
        "/NFL",
        "/NDL",
        "/NJH",
        "/NJS",
        "/NP"
    )
    if ($ExcludeDirectories.Count) {
        $arguments += "/XD"
        $arguments += $ExcludeDirectories
    }
    if ($ExcludeFiles.Count) {
        $arguments += "/XF"
        $arguments += $ExcludeFiles
    }
    & robocopy @arguments | Out-Null
    if ($LASTEXITCODE -ge 8) {
        throw "Robocopy failed ($LASTEXITCODE): $Source -> $Target"
    }
}

$runtimePython = Join-Path $destinationPath "runtime\python"
New-Item -ItemType Directory -Force -Path $runtimePython | Out-Null
foreach ($name in @(
    "python.exe",
    "pythonw.exe",
    "python3.dll",
    "python311.dll",
    "vcruntime140.dll",
    "vcruntime140_1.dll",
    "LICENSE.txt"
)) {
    Copy-Item -LiteralPath (Join-Path $pythonSource $name) -Destination $runtimePython
}
Copy-Tree -Source (Join-Path $pythonSource "DLLs") -Target (Join-Path $runtimePython "DLLs") -ExcludeDirectories @("__pycache__")
Copy-Tree -Source (Join-Path $pythonSource "tcl") -Target (Join-Path $runtimePython "tcl")
Copy-Tree -Source (Join-Path $pythonSource "Lib") -Target (Join-Path $runtimePython "Lib") -ExcludeDirectories @("site-packages", "__pycache__")
Copy-Tree -Source $sitePackagesSource -Target (Join-Path $runtimePython "Lib\site-packages") -ExcludeDirectories @("__pycache__") -ExcludeFiles @("*.pyc", "*.pyo")
Remove-Item -LiteralPath (Join-Path $runtimePython "Lib\site-packages\_virtualenv.pth") -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath (Join-Path $runtimePython "Lib\site-packages\_virtualenv.py") -Force -ErrorAction SilentlyContinue

$appTarget = Join-Path $destinationPath "app"
Copy-Tree -Source (Join-Path $workspace "aibook") -Target (Join-Path $appTarget "aibook") -ExcludeDirectories @("__pycache__") -ExcludeFiles @("*.pyc")
Copy-Item -LiteralPath (Join-Path $workspace "main.py") -Destination $appTarget
Copy-Item -LiteralPath (Join-Path $workspace "README.md") -Destination $appTarget
Copy-Item -LiteralPath (Join-Path $workspace "requirements.txt") -Destination $appTarget

$modelTarget = Join-Path $destinationPath "data\tts\tts_models--multilingual--multi-dataset--xtts_v2"
Copy-Tree -Source $modelSource -Target $modelTarget
New-Item -ItemType Directory -Force -Path (Join-Path $destinationPath "data\aibook"), (Join-Path $destinationPath "outputs") | Out-Null

$ffmpegTarget = Join-Path $destinationPath "runtime\ffmpeg"
New-Item -ItemType Directory -Force -Path $ffmpegTarget | Out-Null
Copy-Item -LiteralPath (Join-Path $ffmpegSource "bin\ffmpeg.exe") -Destination $ffmpegTarget
Copy-Item -LiteralPath (Join-Path $ffmpegSource "bin\ffprobe.exe") -Destination $ffmpegTarget
Copy-Item -LiteralPath (Join-Path $ffmpegSource "LICENSE") -Destination (Join-Path $ffmpegTarget "FFMPEG_LICENSE.txt")
Copy-Item -LiteralPath (Join-Path $ffmpegSource "README.txt") -Destination $ffmpegTarget

Copy-Item -LiteralPath (Join-Path $workspace "portable\AIBook.cmd") -Destination $destinationPath
Copy-Item -LiteralPath (Join-Path $workspace "portable\PORTABLE_README.txt") -Destination $destinationPath

$buildInfo = @(
    "Built: $([DateTime]::Now.ToString('yyyy-MM-dd HH:mm:ss zzz'))",
    "Python: 3.11.8",
    "Coqui TTS: 0.27.5",
    "PyTorch: 2.11.0+cu128",
    "Model: XTTS v2",
    "Platform: Windows 10/11 x64"
)
[IO.File]::WriteAllLines((Join-Path $destinationPath "BUILD_INFO.txt"), $buildInfo)

try {
    Invoke-WebRequest -Uri "https://coqui.ai/cpml.txt" -OutFile (Join-Path $destinationPath "XTTS_CPML.txt") -UseBasicParsing
} catch {
    [IO.File]::WriteAllText(
        (Join-Path $destinationPath "XTTS_CPML_URL.txt"),
        "https://coqui.ai/cpml.txt"
    )
}

$files = Get-ChildItem -LiteralPath $destinationPath -Recurse -File
$total = ($files | Measure-Object Length -Sum).Sum
Write-Host "Portable folder: $destinationPath"
Write-Host "Files: $($files.Count)"
Write-Host "Size GB: $([math]::Round($total / 1GB, 2))"
