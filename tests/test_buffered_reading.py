from __future__ import annotations

import io
import json
import queue
import re
import threading
import time
import unittest
from unittest.mock import patch

import numpy as np
import soundfile as sf

from aibook.audio import MAX_AUDIO_BUFFER, BufferedSpeech, render_buffer
from aibook.buffered_reading import BufferedReading, ReadingOptions, reading_fragments
from aibook.engine import VoiceSpec
from aibook.job import JobCancelled
from aibook.timeline import WordTime
from aibook.voice_studio import _read_messages

SOURCE = (
    "Первый отрывок книги. Здесь начинается история и появляется главный герой.\n\n" * 5
)


class BufferEngine:
    def __init__(self, *, hold_after: int = 100) -> None:
        self.calls = []
        self.lock = threading.Lock()
        self.hold_after = hold_after
        self.gate = threading.Event()
        self.started = threading.Event()

    def synthesize_buffer(self, **kwargs):
        with self.lock:
            self.calls.append(kwargs["text"])
            number = len(self.calls)
        self.started.set()
        if number > self.hold_after:
            while not self.gate.wait(0.01):
                if kwargs["cancel_event"].is_set():
                    raise JobCancelled("Cancelled")
        source = kwargs["text"]
        audio = render_buffer(
            np.sin(np.arange(48000) * 0.01).astype(np.float32) * 0.1, 24000
        )
        matches = list(re.finditer(r"\S+", source))
        step = 1.6 / len(matches)
        words = tuple(
            WordTime(word.start(), word.end(), i * step, (i + 1) * step)
            for i, word in enumerate(matches)
        )
        return BufferedSpeech(audio, 2.0, words, "cpu")

    def unload(self):
        pass


class BufferedReadingTests(unittest.TestCase):
    def setUp(self):
        self.events = queue.Queue()
        self.engine = BufferEngine(hold_after=1)
        self.options = ReadingOptions(VoiceSpec(speaker="Рассказчик"), max_chars=100)
        self.reader = BufferedReading(
            SOURCE, self.options, self.engine, lambda *event: self.events.put(event)
        )

    def tearDown(self):
        self.reader.close()

    def test_only_current_and_next_are_prepared(self):
        self.reader.request(0)
        first = self.reader.future(0).result(timeout=3)
        self.assertGreater(len(first.speech.audio), 100)
        deadline = time.monotonic() + 3
        while len(self.engine.calls) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(len(self.engine.calls), 2)
        self.assertEqual(self.reader.queued_indices(), (0, 1))
        self.assertFalse(self.reader.future(1).done())

    def test_advancing_reuses_prefetched_audio_and_releases_old_future(self):
        self.reader.request(0)
        self.reader.future(0).result(timeout=3)
        self.engine.gate.set()
        second = self.reader.future(1)
        prepared = second.result(timeout=3)
        self.reader.discard(0)
        epoch = self.reader.epoch
        self.reader.request(1)
        self.assertEqual(self.reader.epoch, epoch)
        self.assertIs(self.reader.future(1), second)
        self.assertEqual(prepared.fragment.index, 1)
        self.assertEqual(self.reader.queued_indices(), (1, 2))
        self.assertIsNone(self.reader.future(0))

    def test_selection_does_not_synthesize_a_skipped_prefix(self):
        start = SOURCE.index("Здесь")
        self.reader.request(0, start)
        prepared = self.reader.future(0).result(timeout=3)
        self.assertEqual(prepared.fragment.start, start)
        self.assertTrue(self.engine.calls[0].startswith("Здесь"))

    def test_close_cancels_prefetch_and_joins_owned_threads(self):
        self.reader.request(0)
        self.reader.future(0).result(timeout=3)
        self.reader.close()
        self.assertTrue(self.reader.finished)
        self.assertEqual(self.reader.queued_indices(), ())
        self.assertFalse(
            any(
                thread.name.startswith("aibook-reading")
                for thread in threading.enumerate()
            )
        )

    def test_checkpoint_options_round_trip(self):
        self.assertEqual(ReadingOptions.restore(self.options.serialize()), self.options)

    def test_raw_offsets_keep_whitespace_and_unicode(self):
        source = "  Раз\tдва.\n\n\U0001f600 Три четыре.\nПять  шесть.  "
        parts = reading_fragments(source, 100)
        spoken = " ".join(
            " ".join(source[part.start : part.end].split()) for part in parts
        )
        self.assertEqual(spoken, " ".join(source.split()))
        self.assertEqual(len(parts), 2)

    def test_fragments_preserve_punctuation_and_do_not_exceed_the_limit(self):
        source = "Слово, " * 90
        parts = reading_fragments(source, 100)
        self.assertTrue(
            all(
                len(" ".join(source[part.start : part.end].split())) <= 100
                for part in parts
            )
        )
        self.assertEqual(
            " ".join(source[part.start : part.end] for part in parts), source.strip()
        )


class BufferTransportTests(unittest.TestCase):
    def test_binary_frame_larger_than_control_messages_is_supported(self):
        audio = b"\x00\n\xff" * 30000
        header = json.dumps({"op": "audio_buffer", "size": len(audio)}).encode() + b"\n"
        messages = queue.Queue()
        _read_messages(io.BytesIO(header + audio + b'{"op":"ready"}\n'), messages)
        self.assertEqual(messages.get_nowait()["payload"], audio)
        self.assertEqual(messages.get_nowait()["op"], "ready")
        self.assertEqual(messages.get_nowait()["op"], "eof")

    def test_invalid_or_truncated_frames_are_rejected(self):
        for size, data in (
            (MAX_AUDIO_BUFFER + 1, b""),
            (-1, b""),
            (8, b"x"),
            (True, b"x"),
        ):
            with self.subTest(size=size):
                messages = queue.Queue()
                header = (
                    json.dumps({"op": "audio_buffer", "size": size}).encode() + b"\n"
                )
                _read_messages(io.BytesIO(header + data), messages)
                self.assertEqual(messages.get_nowait()["op"], "error")

    def test_audio_effects_render_in_memory_without_files(self):
        samples = np.sin(np.arange(24000) * 0.05).astype(np.float32) * 0.1
        with patch(
            "pathlib.Path.open", side_effect=AssertionError("Audio was written to disk")
        ):
            audio = render_buffer(
                samples, 24000, playback_speed=2.0, pitch_semitones=1.0, volume_db=1.0
            )
        self.assertAlmostEqual(sf.info(io.BytesIO(audio)).duration, 0.5, delta=0.06)


if __name__ == "__main__":
    unittest.main()
