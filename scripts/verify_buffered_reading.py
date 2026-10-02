from __future__ import annotations

import argparse
import ctypes
import json
import os
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Verify bounded in-memory audiobook reading"
    )
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--device", choices=("auto", "cuda", "cpu"), default="auto")
    parser.add_argument("--null-audio", action="store_true")
    args = parser.parse_args()
    base = args.root.resolve()
    sys.path.insert(0, str(base / "app" if (base / "app").is_dir() else base))
    artifacts = base / "outputs" / ("buffer_acceptance_" + uuid4().hex[:8])
    artifacts.mkdir(parents=True)
    os.environ.update(
        {
            "AIBOOK_ROOT": str(base),
            "AIBOOK_DATA_DIR": str(artifacts / "data"),
            "AIBOOK_OUTPUT_DIR": str(artifacts),
            "AIBOOK_VOICESTUDIO_MODEL_DIR": str(base / "data/models/OmniVoice"),
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
        }
    )

    import tkinter as tk

    import miniaudio
    from PIL import ImageGrab

    from aibook.app import AIBookApp
    from aibook.editor import edit_group
    from aibook.settings import SettingsStore
    from aibook.voice_studio_config import configure_environment
    from voice_studio_main import studio_profile

    configure_environment()
    root = tk.Tk()
    app = AIBookApp(root, studio_profile())
    errors = []
    report = {"checks": [], "audio_backend": "null" if args.null_audio else "hardware"}

    def wait(predicate, timeout=600):
        deadline = time.monotonic() + timeout
        while not predicate():
            root.update()
            if errors:
                raise RuntimeError(errors[0])
            if time.monotonic() >= deadline:
                raise TimeoutError("Buffered reading acceptance timed out")
            time.sleep(0.02)
        root.update()

    def check(condition, name):
        if not condition:
            raise AssertionError(name)
        report["checks"].append(name)

    def set_device():
        if args.null_audio:
            app.player_panel.player._device_factory = (
                lambda **kwargs: miniaudio.PlaybackDevice(
                    backends=[miniaudio.Backend.NULL], **kwargs
                )
            )
        app.player_panel.volume.set(0)
        app.player_panel.player.volume(0)

    def screenshot(name):
        root.update_idletasks()
        handle = ctypes.windll.user32.GetAncestor(root.winfo_id(), 2)
        path = artifacts / name
        ImageGrab.grab(window=handle).save(path)
        return str(path)

    try:
        with patch(
            "aibook.app.messagebox.showerror",
            side_effect=lambda _title, message, **_: errors.append(message),
        ):
            source = (
                "Первая часть. Это проверка чтения книги из буфера.\n\n"
                "Вторая часть. Следующий отрывок готовится во время воспроизведения.\n\n"
                "Третья часть. Произнесённые слова удаляются только из черновика.\n\n"
                "Четвёртая часть. Позиция чтения сохраняется после закрытия приложения."
            )
            with edit_group(app.text):
                app.text.delete("1.0", "end")
                app.text.insert("1.0", source)
            app._on_text_modified()
            app.device.set(args.device)
            app.num_steps.set(16)
            app.max_chars.set(100)
            app.playback_speed.set(1.2)
            app.pitch_semitones.set(-0.5)
            set_device()
            panel = app.player_panel
            selected = source.index("Это")
            app.text.tag_add("sel", f"1.0+{selected}c", f"1.0+{selected + 3}c")
            panel.delete_spoken.set(True)
            started = time.monotonic()
            panel.play_button.invoke()
            wait(lambda: panel.player.snapshot().playing)
            report["first_audio_seconds"] = round(time.monotonic() - started, 3)
            pipeline = panel.reader.pipeline
            check(
                panel.player.snapshot().buffered
                and panel.player.snapshot().path is None,
                "Playback uses an in-memory buffer",
            )
            check(
                app.job_future is None and app.last_output is None,
                "Play does not run whole-book export",
            )
            check(
                panel.reader.part.start == selected,
                "First fragment begins at selected text",
            )
            check(
                pipeline.queued_indices() in ((1,), (0, 1)),
                "Only current and next are queued",
            )
            wait(lambda: panel.reader.index >= 1, timeout=600)
            check(pipeline.future(0) is None, "Completed fragment audio is released")
            check(
                not list(artifacts.rglob("*.wav"))
                and not list(artifacts.rglob("*.mp3")),
                "Reading creates no audio files",
            )
            wait(
                lambda: panel.player.snapshot().playing
                and panel.player.snapshot().position >= 0.8
            )
            panel.play_button.invoke()
            check(
                not panel.player.snapshot().playing,
                "Pause preserves the current buffer",
            )
            check(
                panel._reading.removed
                and app.text.get("1.0", "end-1c") == panel._reading.remaining_text(),
                "Spoken-text deletion remains synchronized",
            )
            check(
                app.text.get("1.0", "end-1c").startswith(source[:selected]),
                "Unplayed prefix remains intact",
            )
            saved_position = panel.player.snapshot().position
            saved_index = panel.reader.index
            remaining = app.text.get("1.0", "end-1c")
            report["desktop_screenshot"] = screenshot("buffer_desktop.png")
            root.geometry("1050x800")
            root.update()
            check(
                app.text.winfo_height() >= 140, "Minimum-window editor remains usable"
            )
            report["compact_screenshot"] = screenshot("buffer_compact.png")
            app.voice_mode.set("clone")
            app._update_voice_controls()
            root.update()
            check(
                app.text.winfo_height() >= 140,
                "Minimum-window clone editor remains usable",
            )
            report["clone_screenshot"] = screenshot("buffer_clone.png")
            app.voice_mode.set("builtin")
            app._on_close()
            saved = SettingsStore().load()
            check(
                saved["reader_state"]["index"] == saved_index
                and abs(saved["reader_state"]["position"] - saved_position) < 0.15,
                "Reading position saved without an audio path",
            )
            root = tk.Tk()
            app = AIBookApp(root, studio_profile())
            set_device()
            check(
                app.text.get("1.0", "end-1c") == remaining
                and not app.player_panel.reader.active,
                "Restart restores remaining text paused",
            )
            app.player_panel.play_button.invoke()
            wait(lambda: app.player_panel.player.snapshot().playing)
            check(
                app.player_panel.reader.index == saved_index
                and abs(app.player_panel.player.snapshot().position - saved_position)
                < 0.2,
                "Restart regenerates only the current fragment and resumes",
            )
            app.player_panel.stop_button.invoke()
            check(
                app.player_panel.player._buffer is None
                and not app.player_panel.reader.active,
                "Stop frees buffers and cancels prefetch",
            )
            app._on_close()
    finally:
        if not app._closing:
            app._on_close()
        app.engine.unload()
    check(
        not any(thread.name.startswith("aibook-") for thread in threading.enumerate()),
        "Owned worker threads have stopped",
    )
    path = artifacts / "report.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(path), **report}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
