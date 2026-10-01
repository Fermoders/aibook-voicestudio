from __future__ import annotations

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from aibook.engine import VoiceSpec
from aibook.voice_library import VoiceLibrary, cached_prompt_path
from aibook.voice_studio import VoiceStudioEngine
from aibook.voice_studio_config import PRESETS


class VoiceLibraryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.library = VoiceLibrary(self.root / "voices")
        self.source = self.root / "encoded.pt"
        self.source.write_bytes(b"encoded prompt with transcript and tokens")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_saved_voice_has_no_reference_file_or_transcript_dependency(self) -> None:
        entry = self.library.add(self.source, "Мой голос")
        voice = VoiceSpec(saved_prompt=self.library.path(entry.id))
        signature = VoiceStudioEngine().voice_signature(voice)
        self.source.unlink()
        voice.validate()
        self.assertEqual(VoiceStudioEngine().voice_signature(voice), signature)
        self.assertEqual(
            VoiceStudioEngine()._voice_request(voice)["reference_text"], ""
        )
        self.assertIsNone(VoiceStudioEngine()._voice_request(voice)["reference"])

    def test_rename_preserves_id_and_audio_tokens(self) -> None:
        entry = self.library.add(self.source, "Старое имя")
        before = self.library.path(entry.id).read_bytes()
        renamed = self.library.rename(entry.id, "Новое имя")
        self.assertEqual(renamed.id, entry.id)
        self.assertEqual(self.library.path(entry.id).read_bytes(), before)
        self.assertEqual(self.library.list()[0].name, "Новое имя")

    def test_delete_removes_only_the_saved_profile(self) -> None:
        entry = self.library.add(self.source, "Удалить")
        path = self.library.path(entry.id)
        self.library.delete(entry.id)
        self.assertEqual(self.library.list(), [])
        self.assertFalse(path.exists())
        self.assertTrue(self.source.exists())

    def test_names_are_validated_and_duplicates_are_case_insensitive(self) -> None:
        self.library.add(self.source, "Имя")
        for name in ("", "имя", "bad\nname", "a" * 81):
            with self.assertRaises(ValueError):
                self.library.add(self.source, name)

    def test_concurrent_writers_do_not_lose_profiles(self) -> None:
        def add(index: int):
            return VoiceLibrary(self.library.directory).add(
                self.source, f"Голос {index}"
            )

        with ThreadPoolExecutor(max_workers=8) as pool:
            entries = list(pool.map(add, range(16)))
        self.assertEqual(len({entry.id for entry in entries}), 16)
        self.assertEqual(len(self.library.list()), 16)
        self.assertFalse(list(self.library.directory.glob("*.tmp")))

    def test_legacy_clones_import_once_and_presets_are_excluded(self) -> None:
        for speaker in PRESETS:
            cached_prompt_path(speaker, self.library.directory).write_bytes(b"preset")
        cached_prompt_path("clone_old", self.library.directory).write_bytes(b"clone")
        cached_prompt_path("clone_duplicate", self.library.directory).write_bytes(
            b"clone"
        )
        self.assertEqual(len(self.library.import_cached()), 1)
        self.assertEqual(len(self.library.import_cached()), 1)

    def test_corrupt_index_is_not_silently_overwritten(self) -> None:
        self.library.index.write_text("not json", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            self.library.add(self.source, "Новый")
        self.assertEqual(self.library.index.read_text(encoding="utf-8"), "not json")

    def test_deleted_clone_does_not_reappear_from_legacy_cache(self) -> None:
        cached_prompt_path("clone_old", self.library.directory).write_bytes(b"clone")
        entry = self.library.import_cached()[0]
        self.library.delete(entry.id)
        reopened = VoiceLibrary(self.library.directory)
        self.assertEqual(reopened.import_cached(), [])


if __name__ == "__main__":
    unittest.main()
