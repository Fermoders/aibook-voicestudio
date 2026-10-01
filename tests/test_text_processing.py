from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from aibook.text_processing import load_document, normalize_text, split_into_chunks


class TextProcessingTests(unittest.TestCase):
    def test_normalize_preserves_paragraphs_and_joins_wrapped_lines(self) -> None:
        source = "Первая стро-\nка продолжается.\nВторая строка.\n\nНовый абзац."
        self.assertEqual(
            normalize_text(source),
            "Первая строка продолжается. Вторая строка.\n\nНовый абзац.",
        )

    def test_chunks_do_not_exceed_limit(self) -> None:
        text = (
            "Это первое предложение русской книги. Это второе предложение, которое немного длиннее первого. "
            "Затем начинается третье предложение с дополнительными подробностями.\n\nНовый абзац завершает проверку."
        )
        chunks = split_into_chunks(text, max_chars=100)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(0 < len(chunk.text) <= 100 for chunk in chunks))
        self.assertEqual(chunks[-1].pause_ms, 620)

    def test_load_utf8_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "book.txt"
            path.write_text("Глава первая.\n\nНачало книги.", encoding="utf-8")
            self.assertEqual(load_document(path), "Глава первая.\n\nНачало книги.")


if __name__ == "__main__":
    unittest.main()
