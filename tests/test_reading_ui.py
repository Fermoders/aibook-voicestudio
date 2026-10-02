from __future__ import annotations

import io
import os
import tempfile
import time
import tkinter as tk
import unittest
from concurrent.futures import Future
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import soundfile as sf

from aibook.app import AIBookApp
from aibook.player import PlaybackState
from aibook.settings import SettingsStore
from test_buffered_reading import SOURCE, BufferEngine
from test_playback_ui import FakePlayer
from voice_studio_main import studio_profile


class MemoryPlayer(FakePlayer):
    def __init__(self):
        super().__init__()
        self.loads = 0
        self.audio = None

    def load_buffer(self, audio, position=0):
        self.loads += 1
        self.audio = audio
        self.state = PlaybackState(
            None, sf.info(io.BytesIO(audio)).duration, position, False, True
        )
        return self.state

    def close(self):
        self.audio = None
        super().close()


class ReadingUITests(unittest.TestCase):
    def setUp(self):
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
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = AIBookApp(self.root, studio_profile())
        self.app.text.insert("1.0", SOURCE)
        self.app._on_text_modified()
        self.app.max_chars.set(100)
        self.engine = BufferEngine(hold_after=1)
        self.app.engine = self.engine
        self.app.player_panel.player.close()
        self.app.player_panel.player = MemoryPlayer()

    def tearDown(self):
        if not self.app._closing:
            self.app._on_close()
        self.environment.stop()
        self.temporary.cleanup()

    def wait(self, predicate):
        deadline = time.monotonic() + 5
        while not predicate() and time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.01)
        self.assertTrue(predicate())

    def start(self):
        panel = self.app.player_panel
        panel.toggle()
        self.wait(lambda: panel.player.snapshot().playing)
        return panel

    def test_play_starts_first_buffer_while_next_is_preparing(self):
        panel = self.start()
        self.wait(lambda: len(self.engine.calls) == 2)
        self.assertEqual(panel.mode.get(), "text")
        self.assertTrue(panel.player.snapshot().buffered)
        self.assertFalse(panel.reader.pipeline.future(1).done())
        self.assertIsNone(self.app.job_future)
        self.assertIsNone(self.app.last_output)
        self.assertFalse((self.directory / "data" / "jobs").exists())
        self.assertEqual(list(self.directory.rglob("*.wav")), [])

    def test_ready_prefetch_becomes_next_buffer_and_previous_is_released(self):
        panel = self.start()
        pipeline = panel.reader.pipeline
        self.engine.gate.set()
        self.wait(lambda: pipeline.future(1).done())
        panel.player.state = replace(panel.player.state, position=2.0, playing=False)
        panel.reader.poll()
        self.assertEqual(panel.reader.index, 1)
        self.assertEqual(panel.player.loads, 2)
        self.assertTrue(panel.player.snapshot().playing)
        self.assertIsNone(pipeline.future(0))

    def test_selection_is_generated_without_the_unplayed_prefix(self):
        panel = self.app.player_panel
        selected = SOURCE.index("Здесь")
        self.app.text.tag_add("sel", f"1.0+{selected}c", f"1.0+{selected + 5}c")
        panel.toggle()
        self.wait(lambda: panel.player.snapshot().playing)
        self.assertTrue(self.engine.calls[0].startswith("Здесь"))
        self.assertEqual(panel.reader.part.start, selected)
        self.assertFalse(self.app.text.tag_ranges("sel"))

    def test_manual_edit_stops_reading_without_deleting_edited_text(self):
        panel = self.start()
        panel.delete_spoken.set(True)
        panel.player.state = replace(panel.player.state, position=1.2)
        self.app.text.insert("1.0", "Правка. ")
        expected = self.app.text.get("1.0", "end-1c")
        self.app._on_text_modified()
        self.assertEqual(self.app.text.get("1.0", "end-1c"), expected)
        self.assertFalse(panel.reader.active)
        self.assertFalse(panel.delete_spoken.get())
        self.assertIsNone(panel.player.audio)

    def test_pause_does_not_generate_the_rest_of_the_book(self):
        panel = self.start()
        panel.toggle()
        self.engine.gate.set()
        self.wait(lambda: panel.reader.pipeline.future(1).done())
        self.root.update()
        self.assertFalse(panel.player.snapshot().playing)
        self.assertEqual(len(self.engine.calls), 2)
        panel.stop()
        self.assertIsNone(panel.player.audio)
        self.assertFalse(panel.reader.active)

    def test_a_cancelled_session_cannot_deliver_audio_to_a_new_session(self):
        panel = self.start()
        old_token = (panel.reader._generation, panel.reader._epoch)
        old_part = panel.reader.part
        panel.stop()
        self.app.engine = BufferEngine(hold_after=0)
        panel.toggle()
        active = panel.reader.pipeline
        self.assertTrue(panel.reader._waiting)
        stale = Future()
        stale.set_exception(RuntimeError("Old session failure"))
        panel.reader.handle("read_ready", old_token, (old_part, stale))
        self.assertIs(panel.reader.pipeline, active)
        self.assertTrue(panel.reader._waiting)

    def test_seek_after_stop_keeps_a_valid_restart_checkpoint(self):
        panel = self.start()
        panel.stop()
        target = SOURCE.index("Первый", 100)
        panel.seek(target)
        saved = panel.reader.save_checkpoint()
        self.assertTrue(saved["options"])
        self.assertEqual(saved["cursor"], target)
        self.engine.gate.set()
        panel.toggle()
        self.wait(lambda: panel.player.snapshot().playing)
        self.assertEqual(panel.reader.part.start, target)

    def test_restart_restores_text_and_position_without_audio_files(self):
        panel = self.start()
        panel.delete_spoken.set(True)
        panel.player.state = replace(panel.player.state, position=0.8)
        panel._consume(0.8)
        remaining = self.app.text.get("1.0", "end-1c")
        panel.position.set(20)
        self.app._on_close()
        saved = SettingsStore().load()
        self.assertEqual(saved["reader_state"]["position"], 0.8)
        self.assertEqual(saved["player_path"], "")
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = AIBookApp(self.root, studio_profile())
        self.assertEqual(self.app.text.get("1.0", "end-1c"), remaining)
        self.assertFalse(self.app.player_panel.reader.active)
        self.assertEqual(self.app.player_panel.reader.checkpoint["position"], 0.8)
        self.assertFalse(self.app.player_panel.player.snapshot().playing)
        self.app.engine = BufferEngine()
        self.app.player_panel.player.close()
        self.app.player_panel.player = MemoryPlayer()
        self.app.player_panel.toggle()
        self.wait(lambda: self.app.player_panel.player.snapshot().playing)
        self.assertAlmostEqual(self.app.player_panel.player.snapshot().position, 0.8)


if __name__ == "__main__":
    unittest.main()
