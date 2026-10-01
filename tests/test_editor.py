from __future__ import annotations

import tkinter as tk
import unittest
from types import SimpleNamespace

from aibook.editor import TextEditing


class EditorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tk.Tk()
        self.root.withdraw()
        self.text = tk.Text(self.root, undo=True)
        self.editor = TextEditing(self.text)
        self.text.insert("1.0", "Первый второй третий.")
        self.text.edit_reset()
        self.select_second()

    def tearDown(self) -> None:
        self.root.update_idletasks()
        self.root.destroy()

    def select_second(self) -> None:
        self.text.tag_remove("sel", "1.0", "end")
        self.text.tag_add("sel", "1.7", "1.13")
        self.text.mark_set("insert", "1.13")

    def test_copy_virtual_event_preserves_unicode_selection(self) -> None:
        self.text.event_generate("<<Copy>>")
        self.assertEqual(self.text.clipboard_get(), "второй")
        self.assertEqual(self.text.get("1.0", "end-1c"), "Первый второй третий.")

    def test_paste_replaces_selection_and_is_one_undo_step(self) -> None:
        self.text.mark_set("insert", "end-1c")
        self.text.clipboard_clear()
        self.text.clipboard_append("новый")
        self.text.event_generate("<<Paste>>")
        self.assertEqual(self.text.get("1.0", "end-1c"), "Первый новый третий.")
        self.editor.undo()
        self.assertEqual(self.text.get("1.0", "end-1c"), "Первый второй третий.")
        self.editor.redo()
        self.assertEqual(self.text.get("1.0", "end-1c"), "Первый новый третий.")

    def test_russian_layout_uses_physical_copy_cut_and_paste_keys(self) -> None:
        event = SimpleNamespace(keycode=67, keysym="Cyrillic_es", state=4)
        self.assertEqual(self.editor.control_key(event), "break")
        self.assertEqual(self.text.clipboard_get(), "второй")
        self.editor.control_key(SimpleNamespace(keycode=88, state=4))
        self.assertEqual(self.text.get("1.0", "end-1c"), "Первый  третий.")
        self.editor.control_key(SimpleNamespace(keycode=86, state=4))
        self.assertEqual(self.text.get("1.0", "end-1c"), "Первый второй третий.")

    def test_delete_and_backspace_remove_the_whole_selection(self) -> None:
        self.assertEqual(self.editor.delete_key(None), "break")
        self.assertEqual(self.text.get("1.0", "end-1c"), "Первый  третий.")
        self.assertIsNone(self.editor.delete_key(None))
        self.editor.undo()
        self.select_second()
        self.assertTrue(self.text.bind("<BackSpace>"))
        self.editor.delete_key(None)
        self.assertEqual(self.text.get("1.0", "end-1c"), "Первый  третий.")

    def test_select_all_and_cut_are_undoable(self) -> None:
        self.editor.control_key(SimpleNamespace(keycode=65, state=4))
        self.editor.cut()
        self.assertEqual(self.text.get("1.0", "end-1c"), "")
        self.editor.undo()
        self.assertEqual(self.text.get("1.0", "end-1c"), "Первый второй третий.")

    def test_empty_clipboard_does_not_delete_selection(self) -> None:
        self.text.clipboard_clear()
        self.editor.paste()
        self.assertEqual(self.text.get("1.0", "end-1c"), "Первый второй третий.")


if __name__ == "__main__":
    unittest.main()
