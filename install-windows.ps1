# Requires Windows 10/11 x64 and Windows PowerShell 5.1 or newer.
[CmdletBinding()]
param(
    [string]$InstallDir = "",
    [string]$CacheDir = "",
    [string]$ManifestPath = "",
    [switch]$NoLaunch,
    [switch]$NoShortcut,
    [switch]$KeepDownloads,
    [switch]$ForceReinstall
)

$ErrorActionPreference = "Stop"
$repository = "Fermoders/aibook-voicestudio"
$version = "1.2.0"
$releaseBase = "https://github.com/$repository/releases/download/v$version"
$application = "AIBookVoiceStudio"
$gib = [int64]1073741824

function Initialize-InstallerModules {
    foreach ($name in @('Microsoft.PowerShell.Utility', 'Microsoft.PowerShell.Management', 'Microsoft.PowerShell.Security', 'CimCmdlets')) {
        Import-Module (Join-Path $PSHOME "Modules\$name\$name.psd1") -ErrorAction Stop
    }
}

function Get-SafeChildPath {
    param([string]$Root, [string]$Relative)
    if (-not $Relative -or $Relative.Contains('\') -or $Relative.Contains(':') -or [IO.Path]::IsPathRooted($Relative)) {
        throw "Unsafe relative installation path."
    }
    foreach ($part in $Relative.Split('/')) {
        if (-not $part -or $part -eq '.' -or $part -eq '..' -or $part.IndexOfAny([IO.Path]::GetInvalidFileNameChars()) -ge 0) {
            throw "Unsafe relative installation path."
        }
    }
    $base = [IO.Path]::GetFullPath($Root).TrimEnd('\')
    $path = [IO.Path]::GetFullPath((Join-Path $base $Relative.Replace('/', '\')))
    if (-not $path.StartsWith($base + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw "Installation path escaped its root."
    }
    return $path
}

function Test-VerifiedFile {
    param([string]$Path, [int64]$Size, [string]$Sha256)
    return (Test-Path -LiteralPath $Path -PathType Leaf) -and
        (Get-Item -LiteralPath $Path).Length -eq $Size -and
        (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash -eq $Sha256
}

function Get-PendingDownloadBytes {
    param($Manifest, [string]$Root)
    $pending = 0L
    foreach ($asset in $Manifest.assets) {
        $path = Get-SafeChildPath $Root $asset.name
        if (Test-VerifiedFile $path $asset.size $asset.sha256) { continue }
        $partialBytes = 0L
        if (Test-Path -LiteralPath ($path + '.part') -PathType Leaf) {
            $partialBytes = [math]::Min([int64]$asset.size, (Get-Item -LiteralPath ($path + '.part')).Length)
        }
        $pending += [int64]$asset.size - $partialBytes
    }
    return $pending
}

function Move-InstallDirectory {
    param([string]$Source, [string]$Destination, [ValidateRange(1, 30)][int]$Attempts = 30)
    $from = [IO.Path]::GetFullPath($Source).TrimEnd('\')
    $to = [IO.Path]::GetFullPath($Destination).TrimEnd('\')
    if ($from -notmatch '^[A-Za-z]:\\' -or $to -notmatch '^[A-Za-z]:\\' -or
        $from -eq [IO.Path]::GetPathRoot($from).TrimEnd('\') -or
        $to -eq [IO.Path]::GetPathRoot($to).TrimEnd('\') -or
        [IO.Path]::GetPathRoot($from) -ne [IO.Path]::GetPathRoot($to) -or
        $from -eq $to -or $to.StartsWith($from + '\', [StringComparison]::OrdinalIgnoreCase) -or
        $from.StartsWith($to + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Installation directories must be distinct local folders on the same drive.'
    }
    for ($attempt = 1; $attempt -le $Attempts; $attempt++) {
        if ([IO.Directory]::Exists($to) -or [IO.File]::Exists($to)) {
            throw "Installation destination already exists; it was preserved: $to"
        }
        try {
            # Do not let Move-Item fall back to a partial copy/delete on Windows.
            [IO.Directory]::Move($from, $to)
            return
        } catch {
            $failure = $_.Exception
            while ($failure.InnerException) { $failure = $failure.InnerException }
            $code = $failure.HResult -band 0xffff
            if ($code -notin @(5, 32, 33) -or $attempt -eq $Attempts) {
                throw [IO.IOException]::new("Cannot rename installation folder '$from' to '$to' (Windows error $code). Close programs using these folders and retry. Downloaded files were retained; folder permissions were not changed.", $failure)
            }
            Write-Host "Windows has not released the installation folder (error $code); retry $attempt of $Attempts."
            Start-Sleep -Milliseconds ([int][math]::Min(2000, 250 * [math]::Pow(2, $attempt - 1)))
        }
    }
}

function Get-RemoteFile {
    param([string]$Uri, [string]$Path, [int64]$Size = 0, [string]$Sha256 = "")
    if ($Size -gt 0 -and (Test-VerifiedFile $Path $Size $Sha256)) { return }
    $partial = $Path + '.part'
    for ($attempt = 1; $attempt -le 3; $attempt++) {
        $request = $response = $inputStream = $outputStream = $null
        try {
            $offset = 0L
            if (Test-Path -LiteralPath $partial -PathType Leaf) {
                $offset = (Get-Item -LiteralPath $partial).Length
                if ($Size -eq 0 -or $offset -ge $Size) {
                    Remove-Item -LiteralPath $partial -Force
                    $offset = 0L
                }
            }
            $request = [Net.Http.HttpRequestMessage]::new([Net.Http.HttpMethod]::Get, $Uri)
            if ($offset) { $request.Headers.Range = [Net.Http.Headers.RangeHeaderValue]::new($offset, $null) }
            $response = $http.SendAsync($request, [Net.Http.HttpCompletionOption]::ResponseHeadersRead).GetAwaiter().GetResult()
            [void]$response.EnsureSuccessStatusCode()
            if ($offset -and [int]$response.StatusCode -eq 206) {
                if ($response.Content.Headers.ContentRange.From -ne $offset) { throw "Invalid resumed byte range." }
                $mode = [IO.FileMode]::Append
            } else {
                $offset = 0L
                $mode = [IO.FileMode]::Create
            }
            $inputStream = $response.Content.ReadAsStreamAsync().GetAwaiter().GetResult()
            $outputStream = [IO.FileStream]::new($partial, $mode, [IO.FileAccess]::Write, [IO.FileShare]::None)
            $buffer = New-Object byte[] (2 * 1024 * 1024)
            $received = $offset
            $lastProgress = [DateTime]::UtcNow
            while (($count = $inputStream.Read($buffer, 0, $buffer.Length)) -gt 0) {
                $outputStream.Write($buffer, 0, $count)
                $received += $count
                if ($Size -gt 0 -and $received -gt $Size) { throw "Download exceeded its manifest size." }
                if ($Size -gt 0 -and ([DateTime]::UtcNow - $lastProgress).TotalSeconds -ge 1) {
                    Write-Progress -Activity "Downloading AIBook VoiceStudio" -Status ([IO.Path]::GetFileName($Path)) -PercentComplete ([math]::Min(100, 100 * $received / $Size))
                    $lastProgress = [DateTime]::UtcNow
                }
            }
            $outputStream.Dispose()
            $outputStream = $null
            if ($Size -gt 0 -and -not (Test-VerifiedFile $partial $Size $Sha256)) {
                Remove-Item -LiteralPath $partial -Force
                throw "Download checksum verification failed."
            }
            Move-Item -LiteralPath $partial -Destination $Path -Force
            return
        } catch {
            if ($attempt -eq 3) { throw "Download failed for $([IO.Path]::GetFileName($Path)). Retry the command; incomplete downloads are resumable." }
            Write-Host "Download interrupted; retry $attempt of 3."
            Start-Sleep -Seconds (2 * $attempt)
        } finally {
            if ($outputStream) { $outputStream.Dispose() }
            if ($inputStream) { $inputStream.Dispose() }
            if ($response) { $response.Dispose() }
            if ($request) { $request.Dispose() }
            Write-Progress -Activity "Downloading AIBook VoiceStudio" -Completed
        }
    }
}

function Read-InstallManifest {
    param([string]$Path)
    $manifest = Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($manifest.schema -ne 1 -or $manifest.application -ne $application -or
        $manifest.repository -ne $repository -or $manifest.version -ne $version -or
        $manifest.tag -ne "v$version" -or $manifest.platform -ne 'windows-x64' -or
        $manifest.source_commit -notmatch '^[0-9a-f]{40}$') {
        throw "Unexpected installation manifest."
    }
    $assetNames = @{}
    $assetBytes = 0L
    foreach ($asset in $manifest.assets) {
        if ($asset.name -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]{0,100}\.(zip|bin)$' -or
            $assetNames.ContainsKey($asset.name) -or $asset.size -le 0 -or $asset.size -ge 2 * $gib -or
            $asset.sha256 -notmatch '^[0-9a-f]{64}$' -or $asset.kind -notin @('zip', 'part')) {
            throw "Invalid release asset."
        }
        $assetNames[$asset.name] = $asset
        $assetBytes += [int64]$asset.size
    }
    $files = @{}
    $fileBytes = 0L
    foreach ($file in $manifest.files) {
        [void](Get-SafeChildPath ([IO.Path]::GetTempPath()) $file.path)
        if ($file.path -notmatch '^(app/|runtime/|source/|examples/|licenses/|data/models/|AIBook VoiceStudio\.exe$|AIBook\.ico$|README\.md$|NOTICE\.txt$|BUILD_INFO\.txt$|RESOLVED_PACKAGES\.txt$)' -or
            $files.ContainsKey($file.path) -or $file.size -lt 0 -or $file.sha256 -notmatch '^[0-9a-f]{64}$') {
            throw "Invalid release file or personal-data path."
        }
        $files[$file.path] = $file
        $fileBytes += [int64]$file.size
    }
    $usedParts = @{}
    foreach ($join in $manifest.joins) {
        if (-not $files.ContainsKey($join.path) -or -not $join.parts.Count) { throw "Invalid split file." }
        $joinedBytes = 0L
        foreach ($name in $join.parts) {
            if (-not $assetNames.ContainsKey($name) -or $assetNames[$name].kind -ne 'part' -or $usedParts.ContainsKey($name)) {
                throw "Invalid split-file part."
            }
            $usedParts[$name] = $true
            $joinedBytes += [int64]$assetNames[$name].size
        }
        if ($joinedBytes -ne $files[$join.path].size) { throw "Incomplete split file." }
    }
    if ($assetBytes -ne $manifest.download_bytes -or $fileBytes -ne $manifest.installed_bytes -or
        -not $files.ContainsKey('runtime/python/python.exe') -or
        -not $files.ContainsKey('source/scripts/check_windows_installation.py') -or
        -not $files.ContainsKey('AIBook VoiceStudio.exe') -or -not $manifest.packages.torch) {
        throw "Incomplete installation manifest."
    }
    $script:ExpectedFiles = $files
    return $manifest
}

function Test-VCRuntime {
    $entry = Get-ItemProperty -LiteralPath 'HKLM:\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64' -ErrorAction SilentlyContinue
    if (-not $entry -or $entry.Installed -ne 1) { return $false }
    return [version]($entry.Version.TrimStart('v')) -ge [version]'14.38.33135.0'
}

function Install-VCRuntime {
    if (Test-VCRuntime) { Write-Host 'Microsoft Visual C++ x64 Runtime is available.'; return }
    $path = Join-Path $cache 'VC_redist.x64.exe'
    Get-RemoteFile 'https://aka.ms/vc14/vc_redist.x64.exe' $path
    $signature = Get-AuthenticodeSignature -LiteralPath $path
    if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'O=Microsoft Corporation(?:,|$)') {
        throw "Visual C++ installer does not have a valid Microsoft signature."
    }
    Write-Host 'Installing Microsoft Visual C++ Runtime. Windows may request UAC approval.'
    $process = Start-Process -FilePath $path -ArgumentList '/install', '/quiet', '/norestart' -Verb RunAs -WindowStyle Hidden -Wait -PassThru
    if ($process.ExitCode -notin @(0, 3010) -or -not (Test-VCRuntime)) { throw "Visual C++ Runtime installation failed or was cancelled." }
    if ($process.ExitCode -eq 3010) { Write-Host 'Windows reports that a restart may be required.' }
}

function Assert-NoRunningApplication {
    param([string]$Root)
    $prefix = $Root.TrimEnd('\') + '\'
    $running = @(Get-CimInstance Win32_Process | Where-Object {
        $_.ExecutablePath -and $_.ExecutablePath.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)
    })
    if ($running.Count) { throw "Close this AIBook VoiceStudio instance before installing. Running processes were preserved." }
}

function Expand-VerifiedArchive {
    param([string]$Path, [string]$Root)
    $archive = [IO.Compression.ZipFile]::OpenRead($Path)
    try {
        foreach ($entry in $archive.Entries) {
            if ($entry.FullName.EndsWith('/')) { continue }
            $relative = $entry.FullName
            if (-not $ExpectedFiles.ContainsKey($relative) -or $entry.Length -ne $ExpectedFiles[$relative].size) {
                throw "Archive contains an unexpected file."
            }
            $destination = Get-SafeChildPath $Root $relative
            [void][IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($destination))
            $source = $output = $null
            try {
                $source = $entry.Open()
                $output = [IO.FileStream]::new($destination, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
                $source.CopyTo($output, 2 * 1024 * 1024)
            } finally {
                if ($output) { $output.Dispose() }
                if ($source) { $source.Dispose() }
            }
        }
    } finally { $archive.Dispose() }
}

function Join-VerifiedParts {
    param($Manifest, [string]$Root)
    foreach ($join in $Manifest.joins) {
        $path = Get-SafeChildPath $Root $join.path
        [void][IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($path))
        $output = [IO.FileStream]::new($path, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
        try {
            foreach ($name in $join.parts) {
                $source = [IO.File]::OpenRead((Get-SafeChildPath $cache $name))
                try { $source.CopyTo($output, 4 * 1024 * 1024) } finally { $source.Dispose() }
            }
        } finally { $output.Dispose() }
    }
}

function Invoke-InstallationCheck {
    param([string]$Root, [string]$Manifest)
    $variables = @('PATH', 'PYTHONNOUSERSITE', 'PYTHONUTF8', 'AIBOOK_ROOT', 'AIBOOK_DATA_DIR', 'AIBOOK_OUTPUT_DIR', 'AIBOOK_VOICESTUDIO_MODEL_DIR')
    $previous = @{}
    foreach ($name in $variables) { $previous[$name] = [Environment]::GetEnvironmentVariable($name, 'Process') }
    try {
        $env:PYTHONNOUSERSITE = '1'
        $env:PYTHONUTF8 = '1'
        $env:AIBOOK_ROOT = $Root
        $env:AIBOOK_DATA_DIR = Join-Path $cache 'health-check-data'
        $env:AIBOOK_OUTPUT_DIR = Join-Path $cache 'health-check-output'
        $env:AIBOOK_VOICESTUDIO_MODEL_DIR = Join-Path $Root 'data\models\OmniVoice'
        $env:PATH = (Join-Path $Root 'runtime\ffmpeg') + ';' + (Join-Path $Root 'runtime\python') + ';' + (Join-Path $env:WINDIR 'System32')
        & (Join-Path $Root 'runtime\python\python.exe') -I (Join-Path $Root 'source\scripts\check_windows_installation.py') --root $Root --manifest $Manifest --report (Join-Path $cache 'installation-check.json')
        if ($LASTEXITCODE -ne 0) { throw "Installation dependency or file verification failed. Existing application data was preserved." }
    } finally {
        foreach ($name in $variables) { [Environment]::SetEnvironmentVariable($name, $previous[$name], 'Process') }
    }
}

function Set-ApplicationShortcut {
    param([string]$Root)
    if ($NoShortcut) { return }
    $folder = [Environment]::GetFolderPath('Programs')
    [void][IO.Directory]::CreateDirectory($folder)
    $shell = New-Object -ComObject WScript.Shell
    try {
        $shortcut = $shell.CreateShortcut((Join-Path $folder 'AIBook VoiceStudio.lnk'))
        try {
            $shortcut.TargetPath = Join-Path $Root 'AIBook VoiceStudio.exe'
            $shortcut.WorkingDirectory = $Root
            $shortcut.IconLocation = (Join-Path $Root 'AIBook.ico') + ',0'
            $shortcut.Description = 'AIBook VoiceStudio'
            $shortcut.Save()
        } finally { [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($shortcut) }
    } finally { [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($shell) }
}

if ($env:OS -ne 'Windows_NT' -or -not [Environment]::Is64BitProcess -or $PSVersionTable.PSVersion -lt [version]'5.1') {
    throw 'Use 64-bit PowerShell 5.1 or newer on Windows 10/11 x64.'
}
Initialize-InstallerModules
if (-not $InstallDir) { $InstallDir = Join-Path $env:LOCALAPPDATA 'AIBookVoiceStudio' }
$target = [IO.Path]::GetFullPath($InstallDir).TrimEnd('\')
if ($target -notmatch '^[A-Za-z]:\\' -or $target -eq [IO.Path]::GetPathRoot($target).TrimEnd('\')) { throw 'Choose a local application folder, not a drive root.' }
$parent = [IO.Path]::GetDirectoryName($target)
$defaultCache = -not $CacheDir
if (-not $CacheDir) { $CacheDir = Join-Path $env:LOCALAPPDATA "AIBookVoiceStudioSetup\v$version" }
$cache = [IO.Path]::GetFullPath($CacheDir).TrimEnd('\')
if ($cache -eq $target -or $cache.StartsWith($target + '\', [StringComparison]::OrdinalIgnoreCase) -or $target.StartsWith($cache + '\', [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Download cache and application directory must be separate.'
}
$mutex = [Threading.Mutex]::new($false, 'Local\AIBookVoiceStudioInstaller')
$acquired = $false
$appMutex = $null
$appAcquired = $false
$http = $null
$stage = $null
$backup = $null
$movedData = @()
$published = $false
try {
    try { $acquired = $mutex.WaitOne(0) } catch [Threading.AbandonedMutexException] { $acquired = $true }
    if (-not $acquired) { throw 'Another AIBook installation is already running.' }
    $hash = [Security.Cryptography.SHA256]::Create()
    try { $identity = [BitConverter]::ToString($hash.ComputeHash([Text.Encoding]::UTF8.GetBytes($target.ToLowerInvariant()))).Replace('-', '') } finally { $hash.Dispose() }
    $appMutex = [Threading.Mutex]::new($false, "Local\AIBookVoiceStudio_$identity")
    try { $appAcquired = $appMutex.WaitOne(0) } catch [Threading.AbandonedMutexException] { $appAcquired = $true }
    if (-not $appAcquired) { throw 'Close AIBook VoiceStudio before installing. Its existing process was preserved.' }
    Assert-NoRunningApplication $target
    Add-Type -AssemblyName System.Net.Http
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    $http = [Net.Http.HttpClient]::new()
    $http.Timeout = [TimeSpan]::FromHours(3)
    $http.DefaultRequestHeaders.UserAgent.ParseAdd('AIBookVoiceStudio-Installer/1.2.0')
    [void][IO.Directory]::CreateDirectory($cache)
    if ($ManifestPath) {
        $manifestFile = [IO.Path]::GetFullPath($ManifestPath)
    } else {
        $manifestFile = Join-Path $cache 'install-manifest.json'
        Get-RemoteFile "$releaseBase/install-manifest.json" $manifestFile
    }
    $manifest = Read-InstallManifest $manifestFile
    $manifestHash = (Get-FileHash -LiteralPath $manifestFile -Algorithm SHA256).Hash
    $markerPath = Join-Path $target '.aibook-install.json'
    $existing = $null
    if (Test-Path -LiteralPath $target) {
        if (Test-Path -LiteralPath $markerPath -PathType Leaf) {
            $existing = Get-Content -LiteralPath $markerPath -Raw -Encoding UTF8 | ConvertFrom-Json
            if ($existing.application -ne $application) { throw 'The target folder belongs to another application.' }
        } elseif (@(Get-ChildItem -LiteralPath $target -Force).Count) {
            throw 'The target folder is not an installer-managed AIBook directory. Existing files were preserved.'
        }
    }
    if ($existing -and $existing.manifest_sha256 -eq $manifestHash -and -not $ForceReinstall) {
        Install-VCRuntime
        Invoke-InstallationCheck $target $manifestFile
        Set-ApplicationShortcut $target
        Write-Host "AIBook VoiceStudio $version is already installed and verified: $target"
        if (-not $NoLaunch) {
            $appMutex.ReleaseMutex()
            $appAcquired = $false
            Start-Process -FilePath (Join-Path $target 'AIBook VoiceStudio.exe') -WindowStyle Hidden
        }
        return
    }
    $installDrive = [IO.DriveInfo]::new([IO.Path]::GetPathRoot($target))
    $cacheDrive = [IO.DriveInfo]::new([IO.Path]::GetPathRoot($cache))
    $sameDrive = $installDrive.Name -eq $cacheDrive.Name
    $pendingDownloadBytes = Get-PendingDownloadBytes $manifest $cache
    if ($defaultCache -and $sameDrive -and $installDrive.AvailableFreeSpace -lt ($manifest.installed_bytes + $pendingDownloadBytes + $gib)) {
        $other = @([IO.DriveInfo]::GetDrives() | Where-Object {
            $_.DriveType -eq 'Fixed' -and $_.IsReady -and $_.Name -ne $installDrive.Name -and $_.AvailableFreeSpace -gt ($manifest.download_bytes + $gib)
        } | Sort-Object AvailableFreeSpace -Descending | Select-Object -First 1)
        if ($other.Count) {
            $cache = Join-Path $other[0].RootDirectory.FullName "AIBookVoiceStudio-Downloads\$env:USERNAME\v$version"
            [void][IO.Directory]::CreateDirectory($cache)
            Write-Host "Using a download cache on another drive: $cache"
            $cacheDrive = $other[0]
            $sameDrive = $false
            $pendingDownloadBytes = Get-PendingDownloadBytes $manifest $cache
        }
    }
    $installRequired = [int64]$manifest.installed_bytes + $gib
    $cacheRequired = $pendingDownloadBytes + $gib
    if ($sameDrive) { $installRequired += $pendingDownloadBytes }
    if ($installDrive.AvailableFreeSpace -lt $installRequired -or (-not $sameDrive -and $cacheDrive.AvailableFreeSpace -lt $cacheRequired)) {
        throw 'Not enough disk space. Choose -InstallDir and -CacheDir on a drive with at least 20 GiB free.'
    }
    [void][IO.Directory]::CreateDirectory($parent)
    $stage = Join-Path $parent ('.aibook-stage-' + [Guid]::NewGuid().ToString('N').Substring(0, 10))
    [void][IO.Directory]::CreateDirectory($stage)
    Write-Host ("Installing AIBook VoiceStudio {0}: download {1:N2} GiB, installed {2:N2} GiB." -f $version, ($manifest.download_bytes / $gib), ($manifest.installed_bytes / $gib))
    foreach ($asset in $manifest.assets) {
        $assetPath = Get-SafeChildPath $cache $asset.name
        Get-RemoteFile "$releaseBase/$($asset.name)" $assetPath $asset.size $asset.sha256
        if ($asset.kind -eq 'zip') { Expand-VerifiedArchive $assetPath $stage }
    }
    Join-VerifiedParts $manifest $stage
    Install-VCRuntime
    Write-Host 'Verifying all installed files, Python packages, native libraries, Tk and models...'
    Invoke-InstallationCheck $stage $manifestFile
    $marker = @{ application = $application; version = $version; repository = $repository; source_commit = $manifest.source_commit; manifest_sha256 = $manifestHash }
    [IO.File]::WriteAllText((Join-Path $stage '.aibook-install.json'), ($marker | ConvertTo-Json), [Text.UTF8Encoding]::new($false))
    Assert-NoRunningApplication $target
    if (Test-Path -LiteralPath $target) {
        foreach ($relative in @('data/voicestudio', 'outputs')) {
            $source = Get-SafeChildPath $target $relative
            if (Test-Path -LiteralPath $source) {
                $destination = Get-SafeChildPath $stage $relative
                [void][IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($destination))
                Move-InstallDirectory $source $destination
                $movedData += $relative
            }
        }
        $backup = Join-Path $parent ([IO.Path]::GetFileName($target) + '.previous-' + [Guid]::NewGuid().ToString('N').Substring(0, 8))
        Move-InstallDirectory $target $backup
    }
    Move-InstallDirectory $stage $target
    $published = $true
    $stage = $null
    Set-ApplicationShortcut $target
    Write-Host "Installed and verified: $(Join-Path $target 'AIBook VoiceStudio.exe')"
    Write-Host 'Python, Git, uv, FFmpeg and CUDA Toolkit do not need separate installation.'
    if ($backup) { Write-Host "Previous application files preserved: $backup" }
    if (-not $KeepDownloads) {
        foreach ($asset in $manifest.assets) { Remove-Item -LiteralPath (Get-SafeChildPath $cache $asset.name) -Force }
    }
    if (-not $NoLaunch) {
        $appMutex.ReleaseMutex()
        $appAcquired = $false
        Start-Process -FilePath (Join-Path $target 'AIBook VoiceStudio.exe') -WindowStyle Hidden
    }
} finally {
    try {
        if (-not $published -and $stage) {
            if ($backup -and (Test-Path -LiteralPath $backup) -and -not (Test-Path -LiteralPath $target)) {
                Move-InstallDirectory $backup $target
            }
            foreach ($relative in $movedData) {
                $source = Get-SafeChildPath $stage $relative
                if (Test-Path -LiteralPath $source) {
                    $destination = Get-SafeChildPath $target $relative
                    [void][IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($destination))
                    Move-InstallDirectory $source $destination
                }
            }
            $resolvedStage = [IO.Path]::GetFullPath($stage)
            if ([IO.Path]::GetDirectoryName($resolvedStage) -eq $parent -and [IO.Path]::GetFileName($resolvedStage) -match '^\.aibook-stage-[0-9a-f]{10}$' -and (Test-Path -LiteralPath $resolvedStage)) {
                Remove-Item -LiteralPath $resolvedStage -Recurse -Force
            }
        }
    } finally {
        foreach ($cleanup in @(
            { if ($http) { $http.Dispose() } },
            { if ($appAcquired) { $appMutex.ReleaseMutex() } },
            { if ($appMutex) { $appMutex.Dispose() } },
            { if ($acquired) { $mutex.ReleaseMutex() } },
            { $mutex.Dispose() }
        )) {
            try { & $cleanup } catch { Write-Warning ("Installer resource cleanup failed: " + $_.Exception.GetType().Name) }
        }
    }
}
