from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aibook.settings import SettingsStore


class SettingsStoreTests(unittest.TestCase):
    def test_empty_paths_stay_empty_after_restart(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("aibook.settings.app_data_dir", return_value=Path(directory)),
        ):
            store = SettingsStore()
            values = store.load()
            store.save(values, "draft")
            loaded = store.load()
            self.assertEqual(loaded["reference_path"], "")
            self.assertEqual(loaded["player_path"], "")
            self.assertEqual(loaded["source_path"], "")

    def test_atomic_snapshot_is_authoritative_over_stale_legacy_draft(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("aibook.settings.app_data_dir", return_value=Path(directory)),
        ):
            store = SettingsStore()
            values = store.load()
            values.update(player_removed=[[0, 4]], player_position=1.3)
            store.save(values, "remaining text")
            store.draft_path.write_text("stale draft", encoding="utf-8")
            self.assertEqual(store.load_draft(), "remaining text")
            self.assertEqual(store.load()["player_removed"], [[0, 4]])

    def test_round_trip_preserves_parameters_and_draft(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch("aibook.settings.app_data_dir", return_value=root),
                patch("aibook.settings.output_dir", return_value=root / "outputs"),
            ):
                store = SettingsStore()
                values = store.load()
                values.update(
                    {
                        "voice_mode": "clone",
                        "reference_path": "voice.mp3",
                        "model_speed": 1.7,
                        "playback_speed": 2.2,
                        "pitch_semitones": -2.5,
                        "volume_db": 4.0,
                    }
                )
                store.save(values, "Сохраненный текст книги")

                restored = store.load()
                draft = store.load_draft()

            self.assertEqual(restored["voice_mode"], "clone")
            self.assertEqual(restored["playback_speed"], 2.2)
            self.assertEqual(restored["pitch_semitones"], -2.5)
            self.assertEqual(draft, "Сохраненный текст книги")


if __name__ == "__main__":
    unittest.main()
