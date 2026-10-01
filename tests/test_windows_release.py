from __future__ import annotations

import importlib.util
import json
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


release = load_script("package_windows_release")
check = load_script("check_windows_installation")


class WindowsReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.root = self.directory / "portable"
        self.root.mkdir()
        self.output = self.directory / "release"

    def tearDown(self):
        for attempt in range(6):
            try:
                self.temporary.cleanup()
                return
            except OSError as error:
                if getattr(error, "winerror", None) not in {32, 145} or attempt == 5:
                    raise
                time.sleep(0.1 * (attempt + 1))

    def add(self, name, data=b"test"):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def package(self, **kwargs):
        return release.build_release(
            self.root, self.output, version="1.1.0", repository="owner/repo",
            source_commit="a" * 40, packages={}, **kwargs,
        )

    def test_user_data_audio_caches_and_bytecode_are_never_published(self):
        self.add("app/main.py")
        self.add("data/models/Whisper/tiny.pt")
        self.add("data/voicestudio/voices/saved_private.pt")
        self.add("data/voicestudio/draft.txt")
        self.add("outputs/private.wav")
        self.add("runtime/python/Lib/__pycache__/private.pyc")
        self.add("source/scripts/__pycache__/private.pyc")
        self.assertEqual(
            [path.relative_to(self.root).as_posix() for path in release.release_files(self.root)],
            ["app/main.py", "data/models/Whisper/tiny.pt"],
        )

    def test_large_model_is_split_and_reconstructed_with_verified_hashes(self):
        data = bytes(range(256)) * 10
        self.add("data/models/OmniVoice/model.safetensors", data)
        self.add("app/main.py", b"print('app')")
        manifest = json.loads(self.package(limit=1024).read_text())
        self.assertEqual(len(manifest["joins"][0]["parts"]), 3)
        installed = self.directory / "installed"
        installed.mkdir()
        for asset in manifest["assets"]:
            path = self.output / asset["name"]
            self.assertEqual(asset["sha256"], release.file_digest(path))
            if asset["kind"] == "zip":
                with zipfile.ZipFile(path) as archive:
                    archive.extractall(installed)
        join = manifest["joins"][0]
        target = check.installation_path(installed, join["path"])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"".join((self.output / name).read_bytes() for name in join["parts"]))
        self.assertEqual(target.read_bytes(), data)
        check.verify_files(installed, manifest)

    def test_modified_installed_file_is_rejected(self):
        path = self.add("app/main.py", b"original")
        manifest = {"files": [release.record_file(self.root, path)]}
        path.write_bytes(b"tampered")
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            check.verify_files(self.root, manifest)

    def test_path_traversal_and_windows_absolute_paths_are_rejected(self):
        for name in ("../escape", "/absolute", "C:/escape", "app\\escape", "app/../escape", "app//x"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                check.installation_path(self.root, name)

    def test_release_cannot_write_into_its_input_directory(self):
        self.output = self.root / "nested"
        with self.assertRaisesRegex(ValueError, "outside"):
            self.package()


if __name__ == "__main__":
    unittest.main()
