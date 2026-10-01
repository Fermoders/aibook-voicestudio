from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from aibook.app import _result_stem, _unique_output_path


class AppHelperTests(unittest.TestCase):
    def test_result_name_comes_from_book_and_does_not_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(_result_stem("C:/Books/Роман.fb2", "Текст"), "Роман")
            first = _unique_output_path(root, "Роман", "wav")
            first.write_bytes(b"audio")
            second = _unique_output_path(root, "Роман", "wav")
            self.assertEqual(second.name, "Роман_2.wav")

    def test_invalid_windows_name_falls_back(self) -> None:
        self.assertEqual(_result_stem("C:/Books/CON.txt", "Текст"), "audiobook")


if __name__ == "__main__":
    unittest.main()
