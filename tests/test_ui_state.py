from __future__ import annotations

import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch

from aibook.app import AIBookApp
from aibook.settings import SettingsStore


class UiStateTests(unittest.TestCase):
    def test_live_state_is_restored_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root_path = Path(directory)
            patches = (
                patch("aibook.settings.app_data_dir", return_value=root_path / "data"),
                patch("aibook.settings.output_dir", return_value=root_path / "outputs"),
                patch("aibook.history.app_data_dir", return_value=root_path / "data"),
                patch("aibook.history.output_dir", return_value=root_path / "outputs"),
            )
            with patches[0], patches[1], patches[2], patches[3]:
                root = tk.Tk()
                root.withdraw()
                app = AIBookApp(root)
                app.source_path.set("C:/Books/book.fb2")
                app.voice_mode.set("clone")
                app.reference_path.set("C:/Voices/sample.mp3")
                app.device.set("cpu")
                app.speed.set(1.8)
                app.playback_speed.set(2.4)
                app.pitch_semitones.set(-1.5)
                app.volume_db.set(3.0)
                app.text.insert("1.0", "Черновик книги")
                app.text.edit_modified(True)
                app._on_text_modified()
                root.after(750, app._on_close)
                root.mainloop()

                store = SettingsStore()
                restored = store.load()
                draft = store.load_draft()

            self.assertEqual(Path(restored["source_path"]), Path("C:/Books/book.fb2"))
            self.assertEqual(
                Path(restored["reference_path"]), Path("C:/Voices/sample.mp3")
            )
            self.assertEqual(restored["device"], "cpu")
            self.assertEqual(restored["model_speed"], 1.8)
            self.assertEqual(restored["playback_speed"], 2.4)
            self.assertEqual(restored["pitch_semitones"], -1.5)
            self.assertEqual(restored["volume_db"], 3.0)
            self.assertEqual(draft, "Черновик книги")


if __name__ == "__main__":
    unittest.main()
