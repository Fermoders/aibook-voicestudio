from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import threading
import time
import tkinter as tk
import unittest
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import soundfile as sf

from aibook.app import AIBookApp
from aibook.engine import VoiceSpec
from aibook.job import JobCancelled, SynthesisJob, SynthesisOptions
from aibook.voice_studio import VoiceStudioEngine, _read_messages
from aibook.voice_studio_worker import _redact
from voice_studio_main import studio_profile

FAKE_WORKER = """
import json, sys, time, wave
print(json.dumps({'op': 'ready'}), flush=True)
for line in sys.stdin:
    request = json.loads(line)
    if request['text'] == 'slow':
        time.sleep(60)
    with wave.open(request['output'], 'wb') as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(24000)
        output.writeframes(b'\\x01\\x00' * 2400)
    print(json.dumps({'op': 'audio', 'device': 'cpu'}), flush=True)
"""


class StudioEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.environment = patch.dict(
            os.environ, {"AIBOOK_DATA_DIR": str(self.root / "data")}
        )
        self.environment.start()
        self.engine = VoiceStudioEngine(
            command=[sys.executable, "-u", "-c", FAKE_WORKER], timeout=3
        )

    def tearDown(self) -> None:
        self.engine.unload()
        self.environment.stop()
        self.temporary.cleanup()

    def render(
        self, text: str, name: str, cancel: threading.Event | None = None
    ) -> str:
        return self.engine.synthesize_chunk(
            text=text,
            output_path=self.root / name,
            voice=VoiceSpec(speaker="Рассказчик"),
            device_preference="cpu",
            temperature=0.65,
            speed=1.0,
            seed=42,
            status=lambda _: None,
            cancel_event=cancel,
        )

    def test_multiple_threads_share_one_worker_without_interleaved_requests(
        self,
    ) -> None:
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(
                pool.map(lambda i: self.render("test", f"{i}.wav"), range(8))
            )
        self.assertEqual(results, ["cpu"] * 8)
        self.assertEqual(len(list(self.root.glob("*.wav"))), 8)
        self.assertIsNotNone(self.engine._process)

    def test_cancellation_reaps_worker_and_allows_restart(self) -> None:
        cancel = threading.Event()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.render, "slow", "cancel.wav", cancel)
            deadline = time.monotonic() + 3
            while self.engine._process is None and time.monotonic() < deadline:
                time.sleep(0.01)
            process = self.engine._process
            cancel.set()
            with self.assertRaises(JobCancelled):
                future.result(timeout=3)
        self.assertIsNotNone(process)
        self.assertIsNotNone(process.poll())
        self.assertIsNone(self.engine._process)
        self.assertEqual(self.render("test", "after.wav"), "cpu")

    def test_timeout_reaps_worker(self) -> None:
        self.engine._timeout = 0.2
        with self.assertRaises(TimeoutError):
            self.render("slow", "timeout.wav")
        self.assertIsNone(self.engine._process)

    def test_missing_reference_transcript_fails_before_worker_spawn(self) -> None:
        with self.assertRaisesRegex(ValueError, "точный текст"):
            self.engine.synthesize_chunk(
                text="test",
                output_path=self.root / "test.wav",
                voice=VoiceSpec(reference_audio=self.root / "absent.wav"),
                device_preference="cpu",
                temperature=0.65,
                speed=1,
                seed=1,
                status=lambda _: None,
            )
        self.assertIsNone(self.engine._process)

    def test_reference_cache_tracks_audio_content_and_transcript(self) -> None:
        reference = self.root / "reference.wav"
        reference.write_bytes(b"first")
        first = self.engine.voice_signature(
            VoiceSpec(reference_audio=reference, reference_text="one")
        )
        other_text = self.engine.voice_signature(
            VoiceSpec(reference_audio=reference, reference_text="two")
        )
        reference.write_bytes(b"other")
        other_audio = self.engine.voice_signature(
            VoiceSpec(reference_audio=reference, reference_text="one")
        )
        self.assertNotEqual(first, other_text)
        self.assertNotEqual(first, other_audio)

    def test_malformed_protocol_is_reported(self) -> None:
        _read_messages(io.BytesIO(b"not-json\n"), self.engine._messages)
        self.assertEqual(self.engine._messages.get_nowait()["op"], "error")
        self.assertEqual(self.engine._messages.get_nowait()["op"], "eof")

    def test_job_resume_and_quality_cache_are_separate(self) -> None:
        job = SynthesisJob(self.engine)
        options = SynthesisOptions(
            output_path=self.root / "book.wav",
            voice=VoiceSpec(speaker="Рассказчик"),
            device="cpu",
            num_steps=32,
        )
        with patch("aibook.job.jobs_dir", return_value=self.root / "jobs"):
            result = job.run(
                "Первое предложение. Второе предложение.",
                options,
                threading.Event(),
                lambda *_: None,
            )
            manifest = next((self.root / "jobs").glob("*/manifest.json"))
            self.assertIn(
                "VoiceStudio", json.loads(manifest.read_text(encoding="utf-8"))["model"]
            )
            worker = self.engine._process
            self.engine.unload()
            job.run(
                "Первое предложение. Второе предложение.",
                options,
                threading.Event(),
                lambda *_: None,
            )
            self.assertIsNone(self.engine._process)
            self.assertIsNotNone(worker.poll())
            self.assertGreater(sf.info(result).frames, 0)
            other = SynthesisOptions(
                output_path=self.root / "book.wav", voice=options.voice, num_steps=16
            )
            from aibook.text_processing import split_into_chunks

            chunks = split_into_chunks("Первое предложение.")
            self.assertNotEqual(
                job._job_id(chunks, options), job._job_id(chunks, other)
            )

    def test_worker_errors_redact_credential_shapes(self) -> None:
        self.assertEqual(_redact("hf_" + "a" * 32), "[REDACTED]")
        self.assertEqual(_redact("sk-" + "b" * 32), "[REDACTED]")


class StudioUITests(unittest.TestCase):
    def test_invalid_numeric_input_is_reported_without_starting_worker(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(
                os.environ,
                {"AIBOOK_DATA_DIR": directory, "AIBOOK_OUTPUT_DIR": directory},
            ),
        ):
            root = tk.Tk()
            root.withdraw()
            app = AIBookApp(root, studio_profile())
            try:
                app.text.insert("1.0", "Проверка.")
                app.speed.set("not-a-number")
                with patch("aibook.app.messagebox.showerror") as error:
                    app._start_synthesis()
                error.assert_called_once()
                self.assertIsNone(app.job_future)
                self.assertIsNone(app.engine._process)
            finally:
                app._on_close()

    def test_only_latest_document_request_can_update_the_editor(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(
                os.environ,
                {"AIBOOK_DATA_DIR": directory, "AIBOOK_OUTPUT_DIR": directory},
            ),
        ):
            root = tk.Tk()
            root.withdraw()
            app = AIBookApp(root, studio_profile())
            first, second = Future(), Future()
            try:
                with (
                    patch(
                        "aibook.app.filedialog.askopenfilename",
                        side_effect=["first.txt", "second.txt"],
                    ),
                    patch.object(app.io_pool, "submit", side_effect=[first, second]),
                ):
                    app._choose_document()
                    app._choose_document()
                self.assertTrue(app._document_pending)
                self.assertEqual(str(app.start_button.cget("state")), "disabled")
                second.set_result("Вторая книга")
                first.set_result("Первая книга")
                app._drain_events()
                self.assertEqual(app.text.get("1.0", "end-1c"), "Вторая книга")
                self.assertEqual(app.source_path.get(), "second.txt")
                self.assertFalse(app._document_pending)
                self.assertEqual(str(app.start_button.cget("state")), "normal")
            finally:
                app._on_close()

    def test_voice_studio_ui_has_own_engine_and_reference_text(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(
                os.environ,
                {"AIBOOK_DATA_DIR": directory, "AIBOOK_OUTPUT_DIR": directory},
            ),
        ):
            root = tk.Tk()
            root.withdraw()
            app = AIBookApp(root, studio_profile())
            self.assertEqual(root.title(), "AIBook VoiceStudio")
            self.assertIsInstance(app.engine, VoiceStudioEngine)
            self.assertFalse(app.profile.needs_cpml)
            self.assertEqual(app.speaker.get(), "Рассказчик")
            app.voice_mode.set("clone")
            app._update_voice_controls()
            self.assertEqual(str(app.transcript_entry.cget("state")), "normal")
            app.reference_text.set("Точный текст образца")
            app.num_steps.set(64)
            app._on_close()
            settings = json.loads(
                (Path(directory) / "settings.json").read_text(encoding="utf-8")
            )
            self.assertEqual(settings["reference_text"], "Точный текст образца")
            self.assertEqual(settings["num_steps"], 64)


if __name__ == "__main__":
    unittest.main()
