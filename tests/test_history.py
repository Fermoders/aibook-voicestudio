from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aibook.history import ResultHistory


class ResultHistoryTests(unittest.TestCase):
    def test_keeps_ten_newest_unique_existing_results(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch("aibook.history.app_data_dir", return_value=root / "data"),
                patch("aibook.history.output_dir", return_value=root / "outputs"),
            ):
                store = ResultHistory(limit=10)
                for index in range(12):
                    result = root / f"result_{index}.wav"
                    result.write_bytes(b"audio")
                    store.add(result)
                store.add(root / "result_11.wav")

                entries = store.load()

            self.assertEqual(len(entries), 10)
            self.assertEqual(entries[0].path.name, "result_11.wav")
            self.assertEqual(len({entry.path for entry in entries}), 10)


if __name__ == "__main__":
    unittest.main()
