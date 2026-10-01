from __future__ import annotations

import os
import tempfile
import time
import tkinter as tk
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import numpy as np
import soundfile as sf

from aibook.app import AIBookApp
from aibook.player import PlaybackState
from aibook.settings import SettingsStore
from aibook.timeline import AudioTimeline, WordTime, audio_stamp
from voice_studio_main import studio_profile

SOURCE = "Раз два три."
WORDS = (WordTime(0, 3, 0, 1), WordTime(4, 7, 2, 3), WordTime(8, 12, 4, 5))


class FakePlayer:
    def __init__(self) -> None:
        self.state = PlaybackState()

    def load(self, path, position=0):
        self.state = PlaybackState(path.resolve(), 6, position)
        return self.state

    def snapshot(self):
        return self.state

    def play(self):
        self.state = replace(self.state, playing=True)

    def pause(self):
        self.state = replace(self.state, playing=False)

    def seek(self, position):
        self.state = replace(self.state, position=position)

    def volume(self, _value):
        pass

    def close(self):
        self.state = PlaybackState()


class PlaybackUITests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.environment = patch.dict(
            os.environ,
            {
                "AIBOOK_DATA_DIR": str(self.directory / "data"),
                "AIBOOK_OUTPUT_DIR": str(self.directory / "outputs"),
            },
        )
        self.environment.start()
        self.path = self.directory / "book.wav"
        sf.write(self.path, np.zeros(144000, dtype=np.float32), 24000)
        AudioTimeline(SOURCE, audio_stamp(self.path), WORDS).save(self.path)
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = AIBookApp(self.root, studio_profile())
        self.app.text.insert("1.0", SOURCE)
        self.app._on_text_modified()
        self.app.player_panel.player.close()
        self.app.player_panel.player = FakePlayer()
        self.app.player_panel.load(self.path)
        self.wait_loaded()

    def tearDown(self) -> None:
        if not self.app._closing:
            self.app._on_close()
        self.environment.stop()
        self.temporary.cleanup()

    def wait_loaded(self) -> None:
        deadline = time.monotonic() + 3
        while self.app.player_panel._loading and time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.01)
        self.assertFalse(self.app.player_panel._loading)

    def test_selection_starts_at_word_time_and_deletes_only_heard_text(self) -> None:
        panel = self.app.player_panel
        self.app.text.tag_add("sel", "1.4", "1.7")
        panel.delete_spoken.set(True)
        panel.toggle()
        self.assertAlmostEqual(panel.player.state.position, 1.97)
        self.assertTrue(panel.player.state.playing)
        self.assertFalse(self.app.text.tag_ranges("sel"))
        panel._consume(3.2)
        self.root.update()
        self.assertEqual(self.app.text.get("1.0", "end-1c"), "Раз три.")
        self.assertEqual(panel._reading.remaining_text(), "Раз три.")
        self.assertIsNotNone(panel._reading)

    def test_manual_edit_pauses_and_disarms_destructive_playback(self) -> None:
        panel = self.app.player_panel
        panel.delete_spoken.set(True)
        panel.toggle()
        self.app.text.insert("end-1c", " Дополнение")
        self.app._on_text_modified()
        self.assertFalse(panel.player.snapshot().playing)
        self.assertFalse(panel.delete_spoken.get())
        self.assertIsNone(panel._reading)
        self.assertEqual(str(panel.delete_check.cget("state")), "disabled")

    def test_undo_recovers_removed_text_without_continuing_deletion(self) -> None:
        panel = self.app.player_panel
        panel.delete_spoken.set(True)
        panel._consume(1.3)
        self.assertEqual(self.app.text.get("1.0", "end-1c"), "два три.")
        self.app.text_editing.undo()
        self.app._on_text_modified()
        self.assertEqual(self.app.text.get("1.0", "end-1c"), SOURCE)
        self.assertIsNone(panel._reading)

    def test_restart_preserves_position_remaining_text_and_selected_clone(self) -> None:
        prompt = self.directory / "reference.pt"
        prompt.write_bytes(b"encoded prompt")
        entry = self.app.voice_library.add(prompt, "Клон")
        self.app.reference_path.set("old.wav")
        self.app.reference_text.set("Old transcript")
        self.app._use_saved_voice(entry, ("old.wav", "Old transcript"))
        prompt.unlink()
        panel = self.app.player_panel
        panel.delete_spoken.set(True)
        panel._consume(1.3)
        panel.player.state = replace(panel.player.state, position=1.3)
        self.app._on_close()

        self.root = tk.Tk()
        self.root.withdraw()
        self.app = AIBookApp(self.root, studio_profile())
        self.wait_loaded()
        self.assertEqual(self.app.text.get("1.0", "end-1c"), "два три.")
        self.assertEqual(self.app.player_panel._reading.removed, [(0, 4)])
        self.assertAlmostEqual(self.app.player_panel.player.snapshot().position, 1.3)
        self.assertFalse(self.app.player_panel.player.snapshot().playing)
        self.assertEqual(self.app._selected_saved_voice().id, entry.id)
        self.assertEqual(self.app.reference_path.get(), "")
        self.assertEqual(self.app.reference_text.get(), "")
        self.assertTrue(self.app._voice_spec().saved_prompt.is_file())

    def test_restore_original_text_is_undoable(self) -> None:
        panel = self.app.player_panel
        panel.delete_spoken.set(True)
        panel._consume(1.3)
        with patch("aibook.playback_ui.messagebox.askyesno", return_value=True):
            panel.restore_text()
        self.assertEqual(self.app.text.get("1.0", "end-1c"), SOURCE)
        self.assertEqual(panel._reading.removed, [])
        self.app.text_editing.undo()
        self.assertEqual(self.app.text.get("1.0", "end-1c"), "два три.")

    def test_plain_audio_restores_position_without_sidecar(self) -> None:
        plain = self.directory / "plain.wav"
        sf.write(plain, np.zeros(144000, dtype=np.float32), 24000)
        panel = self.app.player_panel
        panel.load(
            plain, restored={"player_stamp": audio_stamp(plain), "player_position": 3}
        )
        self.wait_loaded()
        self.assertAlmostEqual(panel.player.snapshot().position, 3)
        self.assertIsNone(panel._reading)
        self.assertFalse(panel.delete_spoken.get())

    def test_seek_cannot_remove_a_skipped_word(self) -> None:
        panel = self.app.player_panel
        panel.delete_spoken.set(True)
        panel._consume(1.3)
        panel.player.state = replace(panel.player.state, position=1.3)
        panel.seek(4)
        panel._consume(5.3)
        self.assertEqual(self.app.text.get("1.0", "end-1c"), "два ")

    def test_decoder_failure_on_close_still_saves_position_and_remaining_text(self) -> None:
        panel = self.app.player_panel
        panel.delete_spoken.set(True)
        panel.player.state = replace(panel.player.state, position=1.3, playing=True)
        stopped = replace(panel.player.state, playing=False)
        with (
            patch.object(
                panel.player,
                "snapshot",
                side_effect=[RuntimeError("decoder test failure"), stopped],
            ),
            self.assertLogs("aibook.playback_ui", level="ERROR"),
        ):
            self.app._on_close()
        panel.close()
        store = SettingsStore()
        saved = store.load()
        self.assertAlmostEqual(saved["player_position"], 1.3)
        self.assertEqual(saved["player_removed"], [[0, 4]])
        self.assertEqual(store.load_draft(), "два три.")
        self.assertIsNone(panel.player.state.path)


if __name__ == "__main__":
    unittest.main()
