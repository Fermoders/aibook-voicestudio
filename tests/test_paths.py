from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aibook.paths import decode_portable_path, encode_portable_path


class PortablePathTests(unittest.TestCase):
    def test_internal_path_follows_portable_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first_root = Path(directory) / "First Folder"
            second_root = Path(directory) / "Новая папка"
            source = first_root / "outputs" / "book.wav"
            with patch.dict(os.environ, {"AIBOOK_ROOT": str(first_root)}):
                encoded = encode_portable_path(source)
            with patch.dict(os.environ, {"AIBOOK_ROOT": str(second_root)}):
                decoded = decode_portable_path(encoded)

            self.assertEqual(encoded, "$AIBOOK_ROOT$/outputs/book.wav")
            self.assertEqual(decoded, second_root / "outputs" / "book.wav")

    def test_external_path_stays_absolute(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Portable"
            external = Path(directory) / "Books" / "book.fb2"
            with patch.dict(os.environ, {"AIBOOK_ROOT": str(root)}):
                self.assertEqual(encode_portable_path(external), str(external))


if __name__ == "__main__":
    unittest.main()
