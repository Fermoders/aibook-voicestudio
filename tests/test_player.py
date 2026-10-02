from __future__ import annotations

import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import soundfile as sf
from unittest.mock import patch

from aibook.player import AudioPlayer, format_time

try:
    import miniaudio
except ImportError:
    miniaudio = None


@unittest.skipIf(
    miniaudio is None, "miniaudio is only required by the VoiceStudio player"
)
class PlayerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "audio.wav"
        sf.write(
            self.path, np.sin(np.arange(48000) * 0.01).astype(np.float32) * 0.05, 24000
        )
        self.player = AudioPlayer(
            device_factory=lambda **kwargs: miniaudio.PlaybackDevice(
                backends=[miniaudio.Backend.NULL], **kwargs
            )
        )

    def tearDown(self) -> None:
        self.player.close()
        self.temporary.cleanup()

    def test_stream_play_pause_seek_and_resume(self) -> None:
        self.player.load(self.path)
        self.player.play()
        time.sleep(0.3)
        state = self.player.snapshot()
        self.assertTrue(state.playing)
        self.assertGreater(state.position, 0.1)
        self.assertLess(state.position, 0.35)
        self.player.pause()
        paused = self.player.snapshot()
        time.sleep(0.1)
        self.assertEqual(self.player.snapshot(), paused)
        self.player.seek(1)
        self.player.play()
        time.sleep(0.2)
        self.assertGreater(self.player.snapshot().position, 1.05)

    def test_buffer_playback_does_not_require_an_audio_file(self) -> None:
        audio = self.path.read_bytes()
        self.path.unlink()
        state = self.player.load_buffer(audio)
        self.assertTrue(state.buffered)
        self.assertIsNone(state.path)
        with patch(
            "miniaudio.stream_file", side_effect=AssertionError("File playback used")
        ):
            self.player.play()
            time.sleep(0.25)
            self.assertGreater(self.player.snapshot().position, 0.1)
            self.player.pause()
            self.player.seek(1.0)
            self.player.play()
            time.sleep(0.2)
            self.assertGreater(self.player.snapshot().position, 1.05)
        self.player.close()
        self.assertIsNone(self.player._buffer)

    def test_concurrent_buffer_controls_do_not_deadlock(self) -> None:
        self.player.load_buffer(self.path.read_bytes())
        self.player.play()
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [
                pool.submit(self.player.seek, (index % 4) / 4) for index in range(12)
            ]
            for future in futures:
                future.result(timeout=5)
        self.assertTrue(self.player.snapshot().buffered)

    def test_concurrent_controls_do_not_deadlock_the_audio_callback(self) -> None:
        self.player.load(self.path)
        self.player.play()

        def control(index: int):
            self.player.volume(index / 20)
            self.player.seek((index % 5) / 10)
            return self.player.snapshot()

        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(control, index) for index in range(12)]
            for future in futures:
                self.assertIsNotNone(future.result(timeout=5).path)

    def test_eof_and_replay_are_consistent(self) -> None:
        self.player.load(self.path, 1.7)
        self.player.play()
        time.sleep(0.6)
        state = self.player.snapshot()
        self.assertFalse(state.playing)
        self.assertAlmostEqual(state.position, state.duration, delta=0.05)
        self.player.play()
        self.assertLess(self.player.snapshot().position, 0.1)

    def test_missing_output_device_reports_error_not_silent_success(self) -> None:
        def missing(**_kwargs):
            raise miniaudio.MiniaudioError("no device")

        self.player._device_factory = missing
        self.player.load(self.path)
        with self.assertRaisesRegex(RuntimeError, "звуковое устройство"):
            self.player.play()
        self.assertFalse(self.player.snapshot().playing)

    def test_decoder_failure_cannot_prevent_thread_safe_cleanup(self) -> None:
        self.player.load(self.path)
        self.player.play()
        with self.player._state_lock:
            self.player._error = RuntimeError("decoder test failure")
        with self.assertLogs("aibook.player", level="ERROR"):
            self.player.close()
        self.assertIsNone(self.player.snapshot().path)
        self.assertIsNone(self.player._device)


class TimeTests(unittest.TestCase):
    def test_hour_format(self) -> None:
        self.assertEqual(format_time(3661), "1:01:01")
        self.assertEqual(format_time(65.9), "1:05")


if __name__ == "__main__":
    unittest.main()
