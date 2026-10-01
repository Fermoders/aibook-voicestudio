from __future__ import annotations

import tkinter as tk
from contextlib import contextmanager


@contextmanager
def edit_group(text: tk.Text):
    automatic = text.cget("autoseparators")
    text.edit_separator()
    text.configure(autoseparators=False)
    try:
        yield
    finally:
        text.edit_separator()
        text.configure(autoseparators=automatic)


class TextEditing:
    def __init__(self, text: tk.Text) -> None:
        self.text = text
        text.configure(exportselection=False, autoseparators=True, maxundo=-1)
        self.menu = tk.Menu(text, tearoff=False)
        for label, command in (
            ("Отменить", self.undo),
            ("Повторить", self.redo),
            ("Вырезать", self.cut),
            ("Копировать", self.copy),
            ("Вставить", self.paste),
            ("Удалить выделенное", self.delete),
            ("Выделить всё", self.select_all),
        ):
            self.menu.add_command(label=label, command=command)
        text.bind("<Button-3>", self.popup)
        text.bind("<Shift-F10>", self.popup)
        text.bind("<Control-KeyPress>", self.control_key)
        for event, command in (
            ("<<Copy>>", self.copy),
            ("<<Cut>>", self.cut),
            ("<<Paste>>", self.paste),
            ("<<SelectAll>>", self.select_all),
            ("<<Undo>>", self.undo),
            ("<<Redo>>", self.redo),
        ):
            text.bind(event, lambda _event, action=command: action())
        text.bind("<Delete>", self.delete_key)
        text.bind("<BackSpace>", self.delete_key)
        text.bind("<Control-Insert>", lambda _event: self.copy())
        text.bind("<Shift-Insert>", lambda _event: self.paste())
        text.bind("<Shift-Delete>", lambda _event: self.cut())

    def selection(self) -> tuple[str, str] | None:
        ranges = self.text.tag_ranges("sel")
        return (str(ranges[0]), str(ranges[1])) if len(ranges) == 2 else None

    def copy(self) -> str:
        if selection := self.selection():
            self.text.clipboard_clear()
            self.text.clipboard_append(self.text.get(*selection))
        return "break"

    def delete(self) -> str:
        if selection := self.selection():
            self.text.edit_separator()
            self.text.mark_set("insert", selection[0])
            self.text.delete(*selection)
            self.text.edit_separator()
        return "break"

    def cut(self) -> str:
        self.copy()
        return self.delete()

    def paste(self) -> str:
        try:
            value = self.text.clipboard_get()
        except tk.TclError:
            return "break"
        with edit_group(self.text):
            if selection := self.selection():
                self.text.mark_set("insert", selection[0])
                self.text.delete(*selection)
            self.text.insert("insert", value.replace("\r\n", "\n"))
        self.text.see("insert")
        return "break"

    def select_all(self) -> str:
        self.text.tag_add("sel", "1.0", "end-1c")
        self.text.mark_set("insert", "end-1c")
        return "break"

    def undo(self) -> str:
        try:
            self.text.edit_undo()
        except tk.TclError:
            pass
        return "break"

    def redo(self) -> str:
        try:
            self.text.edit_redo()
        except tk.TclError:
            pass
        return "break"

    def delete_key(self, _event: tk.Event) -> str | None:
        return self.delete() if self.selection() else None

    def control_key(self, event: tk.Event) -> str | None:
        # Windows virtual keycodes are independent of the English/Russian layout.
        actions = {
            65: self.select_all,
            67: self.copy,
            86: self.paste,
            88: self.cut,
            89: self.redo,
            90: self.redo if event.state & 1 else self.undo,
        }
        action = actions.get(event.keycode)
        return action() if action else None

    def popup(self, event: tk.Event) -> str:
        self.text.focus_set()
        selected = self.selection() is not None
        for label in ("Вырезать", "Копировать", "Удалить выделенное"):
            self.menu.entryconfigure(label, state="normal" if selected else "disabled")
        try:
            self.text.clipboard_get()
            paste_state = "normal"
        except tk.TclError:
            paste_state = "disabled"
        self.menu.entryconfigure("Вставить", state=paste_state)
        x, y = event.x_root, event.y_root
        if event.keysym == "F10":
            box = self.text.bbox("insert")
            if box:
                x, y = (
                    self.text.winfo_rootx() + box[0],
                    self.text.winfo_rooty() + box[1] + box[3],
                )
        try:
            self.menu.tk_popup(x, y)
        finally:
            self.menu.grab_release()
        return "break"
