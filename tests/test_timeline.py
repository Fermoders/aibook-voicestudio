from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from aibook.timeline import (
    AudioTimeline,
    ReadingSession,
    WordTime,
    audio_stamp,
    map_alignment,
)

SOURCE = "Раз два три."
WORDS = (WordTime(0, 3, 0, 1), WordTime(4, 7, 2, 3), WordTime(8, 12, 4, 5))


class TimelineTests(unittest.TestCase):
    def test_removes_only_completed_words_with_a_safety_margin(self) -> None:
        reading = ReadingSession(AudioTimeline(SOURCE, "stamp", WORDS))
        self.assertEqual(reading.consume(0, 1.05, 0), [])
        self.assertEqual(reading.consume(1.05, 1.2, 0), [(0, 4)])
        self.assertEqual(reading.remaining_text(), "два три.")

    def test_selection_start_does_not_delete_unplayed_prefix(self) -> None:
        reading = ReadingSession(AudioTimeline(SOURCE, "stamp", WORDS))
        self.assertEqual(reading.consume(1.97, 5.2, 1.97), [(4, 8), (4, 8)])
        self.assertEqual(reading.remaining_text(), "Раз ")
        self.assertEqual(reading.removed, [(4, 12)])

    def test_seek_does_not_consume_skipped_words(self) -> None:
        reading = ReadingSession(AudioTimeline(SOURCE, "stamp", WORDS))
        reading.consume(0, 1.2, 0)
        reading.consume(4, 5.2, 4)
        self.assertEqual(reading.remaining_text(), "два ")
        self.assertEqual(reading.original_offset(0), 4)

    def test_mapping_survives_restart_and_multiple_removed_ranges(self) -> None:
        reading = ReadingSession(
            AudioTimeline(SOURCE, "stamp", WORDS), [[0, 4], [8, 12]]
        )
        self.assertEqual(reading.remaining_text(), "два ")
        self.assertEqual(reading.original_offset(0), 4)
        self.assertEqual(reading.visible_offset(8), 4)
        self.assertAlmostEqual(
            reading.timeline.time_for_char(reading.original_offset(1)), 1.97
        )

    def test_raw_whitespace_is_preserved_in_character_offsets(self) -> None:
        source = "  Раз\n\nдва   три. "
        aligned = [
            {"word": word, "start": index * 2, "end": index * 2 + 1}
            for index, word in enumerate((" Раз", " два", " три."))
        ]
        words = map_alignment(source, aligned, 6)
        self.assertEqual(
            [source[word.char_start : word.char_end] for word in words],
            ["Раз", "два", "три."],
        )

    def test_subword_tokens_are_grouped_before_deletion(self) -> None:
        words = map_alignment(
            "Привет.",
            [
                {"word": "При", "start": 0, "end": 0.4},
                {"word": "вет.", "start": 0.4, "end": 1},
            ],
            2,
        )
        self.assertEqual(words, (WordTime(0, 7, 0, 1),))

    def test_incomplete_or_nonfinite_alignment_is_rejected(self) -> None:
        for aligned in (
            [{"word": "два", "start": 0, "end": 1}],
            [{"word": SOURCE, "start": float("nan"), "end": 1}],
        ):
            with self.assertRaises(ValueError):
                map_alignment(SOURCE, aligned, 5)

    def test_zero_duration_words_are_never_deleted(self) -> None:
        reading = ReadingSession(AudioTimeline("Раз", "stamp", (WordTime(0, 3, 0, 0),)))
        self.assertEqual(reading.consume(0, 10, 0), [])
        self.assertEqual(reading.remaining_text(), "Раз")

    def test_tcl_surrogate_offsets_account_for_removed_non_bmp_characters(self) -> None:
        reading = ReadingSession(AudioTimeline("\U0001f600 Раз два", "stamp"), [[2, 6]])
        self.assertEqual(reading.remaining_text(), "\U0001f600 два")
        self.assertEqual(reading.tk_offset(2), 3)
        removed = ReadingSession(AudioTimeline("\U0001f600 Раз", "stamp"), [[0, 2]])
        self.assertEqual(removed.tk_offset(0), 0)

    def test_changed_audio_invalidates_its_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            audio = Path(directory) / "test.wav"
            audio.write_bytes(b"audio")
            timeline = AudioTimeline(SOURCE, audio_stamp(audio), WORDS)
            timeline.save(audio)
            self.assertEqual(AudioTimeline.load(audio), timeline)
            audio.write_bytes(b"different audio")
            self.assertIsNone(AudioTimeline.load(audio))

    def test_invalid_removed_ranges_are_rejected(self) -> None:
        for removed in ([[0, 100]], [[-1, 4]], [["0", 4]]):
            with self.assertRaises(ValueError):
                ReadingSession(AudioTimeline(SOURCE, "stamp", WORDS), removed)


if __name__ == "__main__":
    unittest.main()
