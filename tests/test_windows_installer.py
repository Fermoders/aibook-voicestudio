from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import threading
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

INSTALLER = Path(__file__).resolve().parent.parent / "install-windows.ps1"
POWERSHELL = Path(os.environ.get("WINDIR", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
DIRECTORY_LOCK_SOURCE = """
using System;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;
public static class InstallerDirectoryLock {
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    public static extern SafeFileHandle CreateFileW(string path, uint access,
        uint share, IntPtr security, uint creation, uint flags, IntPtr template);
    public static SafeFileHandle Open(string path) {
        SafeFileHandle handle = CreateFileW(path, 0x80000000, 3, IntPtr.Zero, 3, 0x02000000, IntPtr.Zero);
        if (handle.IsInvalid) {
            int error = Marshal.GetLastWin32Error();
            handle.Dispose();
            throw new System.ComponentModel.Win32Exception(error);
        }
        return handle;
    }
}
"""


def ps_literal(value):
    return "'" + str(value).replace("'", "''") + "'"


@unittest.skipUnless(os.name == "nt" and POWERSHELL.is_file(), "Windows PowerShell is required")
class WindowsInstallerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def run_functions(self, body):
        script = (
            "$ErrorActionPreference = 'Stop'; $tokens = $null; $errors = $null; "
            f"$ast = [Management.Automation.Language.Parser]::ParseFile({ps_literal(INSTALLER)}, [ref]$tokens, [ref]$errors); "
            "if ($errors.Count) { throw $errors[0].Message }; "
            "$functions = $ast.FindAll({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst]}, $true); "
            "foreach ($function in $functions) { . ([scriptblock]::Create($function.Extent.Text)) }; "
            "Initialize-InstallerModules; "
            + body
        )
        result = subprocess.run(
            [str(POWERSHELL), "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=45,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_installer_parses_on_windows_powershell_51(self):
        self.run_functions("Write-Output 'Parsed on Windows PowerShell 5.1.'")

    def test_safe_paths_reject_escape_and_absolute_windows_paths(self):
        self.run_functions(
            f"$root = {ps_literal(self.root)}; "
            "foreach ($path in @('../escape', 'C:/escape', '/escape', 'app/../escape', 'app\\escape')) { "
            "$rejected = $false; try { [void](Get-SafeChildPath $root $path) } catch { $rejected = $true }; "
            "if (-not $rejected) { throw 'Unsafe path accepted' } }; "
            "[void](Get-SafeChildPath $root 'app/main.py')"
        )

    def test_checksum_rejects_corrupt_cache(self):
        path = self.root / "asset.bin"
        path.write_bytes(b"valid")
        digest = hashlib.sha256(b"valid").hexdigest()
        self.run_functions(
            f"$path = {ps_literal(path)}; "
            f"if (-not (Test-VerifiedFile $path 5 '{digest}')) {{ throw 'Valid asset rejected' }}; "
            f"if (Test-VerifiedFile $path 5 ('0' * 64)) {{ throw 'Corrupt asset accepted' }}"
        )

    def test_locked_directory_retries_native_rename_without_partial_copy(self):
        source = self.root / "stage"
        source.mkdir()
        (source / "retained.txt").write_bytes(b"retained")
        target = self.root / "installed"
        self.run_functions(
            f"Add-Type -TypeDefinition {ps_literal(DIRECTORY_LOCK_SOURCE)}; "
            f"$script:handle = [InstallerDirectoryLock]::Open({ps_literal(source)}); "
            "$script:sleeps = 0; "
            "function Start-Sleep { param([int]$Milliseconds) "
            "$script:sleeps++; "
            f"if (Test-Path -LiteralPath {ps_literal(target)}) {{ throw 'Rename created a partial target' }}; "
            "$script:handle.Dispose() }; "
            f"try {{ Move-InstallDirectory {ps_literal(source)} {ps_literal(target)} -Attempts 3; "
            "if ($script:sleeps -ne 1) { throw 'The native Windows lock was not retried' } } "
            "finally { $script:handle.Dispose() }"
        )
        self.assertFalse(source.exists())
        self.assertEqual((target / "retained.txt").read_bytes(), b"retained")

    def test_native_lock_reproduces_the_reported_move_item_ioerror(self):
        source = self.root / "stage"
        source.mkdir()
        target = self.root / "installed"
        self.run_functions(
            f"Add-Type -TypeDefinition {ps_literal(DIRECTORY_LOCK_SOURCE)}; "
            f"$handle = [InstallerDirectoryLock]::Open({ps_literal(source)}); "
            "$rejected = $false; "
            f"try {{ Move-Item -LiteralPath {ps_literal(source)} -Destination {ps_literal(target)} }} "
            "catch { $rejected = $true; if ($_.FullyQualifiedErrorId -notmatch 'MoveDirectoryItemIOError') { throw } } "
            "finally { $handle.Dispose() }; "
            "if (-not $rejected) { throw 'The reported Move-Item failure was not reproduced' }"
        )

    def test_persistent_directory_lock_is_bounded_and_preserves_source(self):
        source = self.root / "stage"
        source.mkdir()
        (source / "retained.txt").write_bytes(b"retained")
        target = self.root / "installed"
        self.run_functions(
            f"Add-Type -TypeDefinition {ps_literal(DIRECTORY_LOCK_SOURCE)}; "
            f"$handle = [InstallerDirectoryLock]::Open({ps_literal(source)}); "
            "$script:sleeps = 0; function Start-Sleep { param([int]$Milliseconds) $script:sleeps++ }; "
            "$rejected = $false; "
            f"try {{ Move-InstallDirectory {ps_literal(source)} {ps_literal(target)} -Attempts 3 }} "
            "catch { $rejected = $true; if ($_.Exception.ToString() -notmatch 'Windows error (5|32|33)') { throw } } "
            "finally { $handle.Dispose() }; "
            "if (-not $rejected -or $script:sleeps -ne 2) { throw 'Persistent lock was not bounded' }"
        )
        self.assertEqual((source / "retained.txt").read_bytes(), b"retained")
        self.assertFalse(target.exists())

    def test_directory_move_does_not_nest_inside_an_existing_destination(self):
        source, target = self.root / "stage", self.root / "installed"
        source.mkdir()
        target.mkdir()
        (source / "source.txt").write_bytes(b"source")
        (target / "original.txt").write_bytes(b"original")
        self.run_functions(
            "$rejected = $false; "
            f"try {{ Move-InstallDirectory {ps_literal(source)} {ps_literal(target)} }} "
            "catch { $rejected = $true }; if (-not $rejected) { throw 'Existing destination accepted' }"
        )
        self.assertEqual((source / "source.txt").read_bytes(), b"source")
        self.assertEqual(list(target.iterdir()), [target / "original.txt"])

    def test_directory_move_rejects_unsafe_roots_and_nested_targets(self):
        source = self.root / "stage"
        source.mkdir()
        self.run_functions(
            f"$source = {ps_literal(source)}; $drive = [IO.Path]::GetPathRoot($source); "
            "$rejected = 0; foreach ($destination in @($drive, $source, (Join-Path $source 'child'))) { "
            "try { Move-InstallDirectory $source $destination } catch { $rejected++ } }; "
            "if ($rejected -ne 3) { throw 'Unsafe move accepted' }"
        )
        self.assertTrue(source.exists())

    def test_pending_download_space_discounts_only_verified_assets_and_partials(self):
        cached = self.root / "cached.zip"
        cached.write_bytes(b"valid")
        corrupt = self.root / "corrupt.zip"
        corrupt.write_bytes(b"wrong")
        (self.root / "partial.bin.part").write_bytes(b"ab")
        manifest = {"assets": [
            {"name": path.name, "size": 5, "sha256": hashlib.sha256(b"valid").hexdigest()}
            for path in (cached, corrupt, self.root / "missing.bin", self.root / "partial.bin")
        ]}
        self.run_functions(
            f"$manifest = {ps_literal(json.dumps(manifest))} | ConvertFrom-Json; "
            f"$pending = Get-PendingDownloadBytes $manifest {ps_literal(self.root)}; "
            "if ($pending -ne 13) { throw \"Incorrect pending download space: $pending\" }"
        )

    def test_archive_cannot_write_outside_the_staging_directory(self):
        path = self.root / "bad.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("../escape.txt", b"x")
        stage = self.root / "stage"
        stage.mkdir()
        self.run_functions(
            "Add-Type -AssemblyName System.IO.Compression.FileSystem; "
            "$ExpectedFiles = @{'../escape.txt' = [pscustomobject]@{size = 1}}; "
            "$rejected = $false; "
            f"try {{ Expand-VerifiedArchive {ps_literal(path)} {ps_literal(stage)} }} catch {{ $rejected = $true }}; "
            "if (-not $rejected) { throw 'Archive escape accepted' }"
        )
        self.assertFalse((self.root / "escape.txt").exists())

    def test_interrupted_download_resumes_and_verifies_full_sha256(self):
        data = bytes(range(256)) * 256
        ranges = []
        lock = threading.Lock()

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                offset = 0
                header = self.headers.get("Range")
                if header:
                    offset = int(header.removeprefix("bytes=").split("-")[0])
                with lock:
                    ranges.append(offset)
                self.send_response(206 if offset else 200)
                self.send_header("Content-Length", str(len(data) - offset))
                if offset:
                    self.send_header("Content-Range", f"bytes {offset}-{len(data) - 1}/{len(data)}")
                self.end_headers()
                self.wfile.write(data[offset:])

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, name="installer-test-http")
        thread.start()
        path = self.root / "asset.bin"
        path.with_suffix(".bin.part").write_bytes(data[:1024])
        try:
            self.run_functions(
                "Add-Type -AssemblyName System.Net.Http; "
                "$handler = [Net.Http.HttpClientHandler]::new(); $handler.UseProxy = $false; "
                "$http = [Net.Http.HttpClient]::new($handler); "
                f"try {{ Get-RemoteFile 'http://127.0.0.1:{server.server_port}/asset' {ps_literal(path)} {len(data)} '{hashlib.sha256(data).hexdigest()}' }} "
                "finally { $http.Dispose() }"
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        self.assertEqual(path.read_bytes(), data)
        self.assertEqual(ranges, [1024])

    def test_failed_stage_cleanup_cannot_leave_installer_mutexes_locked(self):
        cache = self.root / "cache"
        cache.mkdir()
        asset = cache / "windows-01.zip"
        with zipfile.ZipFile(asset, "w") as archive:
            archive.writestr("unexpected.txt", b"x")
        manifest = cache / "manifest.json"
        files = [
            {"path": name, "size": 1, "sha256": hashlib.sha256(b"x").hexdigest()}
            for name in (
                "runtime/python/python.exe", "AIBook VoiceStudio.exe",
                "source/scripts/check_windows_installation.py",
            )
        ]
        manifest.write_text(json.dumps({
            "schema": 1, "application": "AIBookVoiceStudio", "version": "1.2.0",
            "repository": "Fermoders/aibook-voicestudio", "tag": "v1.2.0",
            "platform": "windows-x64", "source_commit": "a" * 40,
            "installed_bytes": 3, "download_bytes": asset.stat().st_size,
            "packages": {"torch": "2.8.0"}, "joins": [], "files": files,
            "assets": [{"name": asset.name, "kind": "zip", "size": asset.stat().st_size,
                        "sha256": hashlib.sha256(asset.read_bytes()).hexdigest()}],
        }), encoding="utf-8")
        target = self.root / "installed"
        identity = hashlib.sha256(str(target).lower().encode()).hexdigest().upper()
        child = (
            "foreach ($name in @('Local\\AIBookVoiceStudioInstaller', "
            f"'Local\\AIBookVoiceStudio_{identity}')) {{ "
            "$mutex = [Threading.Mutex]::new($false, $name); "
            "if (-not $mutex.WaitOne(0)) { throw 'Installer leaked a mutex' }; "
            "$mutex.ReleaseMutex(); $mutex.Dispose() }"
        )
        self.run_functions(
            "function Remove-Item { param([string]$LiteralPath, [switch]$Recurse, [switch]$Force) "
            "if ($LiteralPath -match '\\.aibook-stage-') { throw 'Controlled stage-cleanup failure' }; "
            "Microsoft.PowerShell.Management\\Remove-Item @PSBoundParameters }; "
            "$failed = $false; "
            f"try {{ & {ps_literal(INSTALLER)} -InstallDir {ps_literal(target)} "
            f"-CacheDir {ps_literal(cache)} -ManifestPath {ps_literal(manifest)} -NoLaunch -NoShortcut -KeepDownloads }} "
            "catch { $failed = $true }; if (-not $failed) { throw 'Invalid archive accepted' }; "
            f"& {ps_literal(POWERSHELL)} -NoProfile -Command {ps_literal(child)}; "
            "if ($LASTEXITCODE) { throw 'Resource-release assertion failed' }"
        )


if __name__ == "__main__":
    unittest.main()
