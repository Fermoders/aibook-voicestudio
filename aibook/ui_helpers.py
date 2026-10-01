from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import ttk


class Tooltip:
    def __init__(self, widget: tk.Widget, label: str | tk.Variable) -> None:
        self.widget, self.label = widget, label
        self.window = None
        self.pending = None
        widget.bind("<Enter>", self._enter, add=True)
        widget.bind("<Leave>", self._leave, add=True)
        widget.bind("<ButtonPress>", self._leave, add=True)
        widget.bind("<Destroy>", self._leave, add=True)

    def _enter(self, _event) -> None:
        self.pending = self.widget.after(600, self._show)

    def _show(self) -> None:
        self.pending = None
        self.window = tk.Toplevel(self.widget)
        self.window.overrideredirect(True)
        self.window.geometry(
            f"+{self.widget.winfo_rootx()}+{self.widget.winfo_rooty() + self.widget.winfo_height() + 4}"
        )
        ttk.Label(
            self.window,
            text=self.label.get()
            if isinstance(self.label, tk.Variable)
            else self.label,
            padding=6,
        ).pack()

    def _leave(self, _event=None) -> None:
        if self.pending is not None:
            self.widget.after_cancel(self.pending)
            self.pending = None
        if self.window is not None:
            self.window.destroy()
            self.window = None


def icon_button(
    parent, icon: str, label: str, command, *, state: str = "normal"
) -> ttk.Button:
    path = Path(__file__).parent / "assets" / "icons" / f"{icon}.png"
    button = ttk.Button(
        parent, command=command, state=state, width=3, style="Tool.TButton"
    )
    if path.is_file():
        button.image = tk.PhotoImage(master=parent, file=str(path))
        button.configure(image=button.image)
    else:
        button.configure(text=label, width=12)
    button.tooltip = Tooltip(button, label)
    return button


def release_images(widget: tk.Widget) -> None:
    for child in widget.winfo_children():
        release_images(child)
    image = getattr(widget, "image", None)
    if isinstance(image, tk.PhotoImage) and image.name:
        image.__del__()
        image.name = None
