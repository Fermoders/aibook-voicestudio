from __future__ import annotations

import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import numpy as np
import soundfile as sf

from aibook.engine import VoiceSpec
from aibook.job import SynthesisJob, SynthesisOptions


class FakeEngine:
    cache_signature = "test-engine-v1"

    def __init__(self) -> None:
        self.calls = 0

    def synthesize_chunk(self, *, output_path: Path, **_kwargs) -> str:
        self.calls += 1
        sf.write(output_path, np.full(2400, 0.05, dtype=np.float32), 24000)
        return "cpu"


class JobTests(unittest.TestCase):
    def test_playback_sidecar_keeps_exact_editor_text(self) -> None:
        from aibook.timeline import AudioTimeline

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            options = SynthesisOptions(
                output_path=root / "book.wav",
                voice=VoiceSpec(speaker="Ana Florence"),
                include_text=True,
            )
            text = "  Первая строка.\n\nВторая строка.  "
            with patch("aibook.job.jobs_dir", return_value=root / "jobs"):
                output = SynthesisJob(FakeEngine()).run(
                    text, options, threading.Event(), lambda *_: None
                )
            self.assertEqual(AudioTimeline.load(output).source, text)

    def test_concurrent_identical_jobs_synthesize_each_chunk_only_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entered = threading.Event()
            release = threading.Event()

            class SlowEngine(FakeEngine):
                def synthesize_chunk(self, *, output_path: Path, **kwargs) -> str:
                    entered.set()
                    if not release.wait(timeout=3):
                        raise TimeoutError("Test did not release synthesis")
                    return super().synthesize_chunk(output_path=output_path, **kwargs)

            engine = SlowEngine()
            first = SynthesisOptions(
                output_path=root / "one.wav", voice=VoiceSpec(speaker="Ana Florence")
            )
            second = SynthesisOptions(output_path=root / "two.wav", voice=first.voice)
            job = SynthesisJob(engine)
            waiting = threading.Event()

            def progress(*_args) -> None:
                waiting.set()

            with (
                patch("aibook.job.jobs_dir", return_value=root / "jobs"),
                ThreadPoolExecutor(max_workers=2) as pool,
            ):
                first_future = pool.submit(
                    job.run, "Проверка.", first, threading.Event(), lambda *_: None
                )
                self.assertTrue(entered.wait(timeout=2))
                second_future = pool.submit(
                    job.run, "Проверка.", second, threading.Event(), progress
                )
                try:
                    self.assertTrue(waiting.wait(timeout=2))
                finally:
                    release.set()
                self.assertTrue(first_future.result(timeout=3).is_file())
                self.assertTrue(second_future.result(timeout=3).is_file())
            self.assertEqual(engine.calls, 1)

    def test_failed_chunk_is_not_published_or_reused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            class FailOnceEngine(FakeEngine):
                def synthesize_chunk(self, *, output_path: Path, **kwargs) -> str:
                    device = super().synthesize_chunk(output_path=output_path, **kwargs)
                    if self.calls == 1:
                        raise RuntimeError("Interrupted after writing")
                    return device

            engine = FailOnceEngine()
            job = SynthesisJob(engine)
            options = SynthesisOptions(
                output_path=root / "book.wav", voice=VoiceSpec(speaker="Ana Florence")
            )
            with patch("aibook.job.jobs_dir", return_value=root / "jobs"):
                with self.assertRaisesRegex(RuntimeError, "Interrupted"):
                    job.run("Проверка.", options, threading.Event(), lambda *_: None)
                self.assertFalse(list((root / "jobs").glob("*/*.wav")))
                result = job.run(
                    "Проверка.", options, threading.Event(), lambda *_: None
                )
            self.assertTrue(result.is_file())
            self.assertEqual(engine.calls, 2)

    def test_rejects_playback_speed_above_three(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            options = SynthesisOptions(
                output_path=Path(directory) / "book.wav",
                voice=VoiceSpec(speaker="Ana Florence"),
                playback_speed=3.1,
            )
            with self.assertRaisesRegex(ValueError, "Скорость файла"):
                SynthesisJob(FakeEngine()).run(  # type: ignore[arg-type]
                    "Проверка.", options, threading.Event(), lambda *_args: None
                )

    def test_rejects_speed_above_three(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            options = SynthesisOptions(
                output_path=Path(directory) / "book.wav",
                voice=VoiceSpec(speaker="Ana Florence"),
                speed=3.1,
            )
            with self.assertRaisesRegex(ValueError, "от 0.7 до 3.0"):
                SynthesisJob(FakeEngine()).run(  # type: ignore[arg-type]
                    "Проверка.", options, threading.Event(), lambda *_args: None
                )

    def test_second_run_reuses_valid_chunks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            engine = FakeEngine()
            job = SynthesisJob(engine)  # type: ignore[arg-type]
            options = SynthesisOptions(
                output_path=root / "book.wav",
                voice=VoiceSpec(speaker="Ana Florence"),
                max_chars=100,
            )
            text = "Первое предложение для проверки. Второе предложение для проверки. Третье предложение для проверки."
            with patch("aibook.job.jobs_dir", return_value=root / "jobs"):
                result = job.run(text, options, threading.Event(), lambda *_args: None)
                first_call_count = engine.calls
                second = job.run(text, options, threading.Event(), lambda *_args: None)

            self.assertTrue(result.is_file())
            self.assertEqual(result, second)
            self.assertGreater(first_call_count, 0)
            self.assertEqual(engine.calls, first_call_count)


if __name__ == "__main__":
    unittest.main()
