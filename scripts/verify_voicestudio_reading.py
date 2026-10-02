from __future__ import annotations

import argparse
import ctypes
import json
import os
import shutil
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Verify native audiobook reading and saved clones"
    )
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--device", choices=("auto", "cuda", "cpu"), default="auto")
    parser.add_argument(
        "--null-audio",
        action="store_true",
        help="Explicit test-only output without hardware playback",
    )
    args = parser.parse_args()
    root_path = args.root.resolve()
    sys.path.insert(
        0, str(root_path / "app" if (root_path / "app").is_dir() else root_path)
    )
    artifacts = root_path / "outputs" / ("reading_acceptance_" + uuid4().hex[:8])
    artifacts.mkdir(parents=True)
    os.environ.update(
        {
            "AIBOOK_ROOT": str(root_path),
            "AIBOOK_DATA_DIR": str(artifacts / "data"),
            "AIBOOK_OUTPUT_DIR": str(artifacts),
            "AIBOOK_VOICESTUDIO_MODEL_DIR": str(
                root_path / "data" / "models" / "OmniVoice"
            ),
        }
    )
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"

    import tkinter as tk

    import miniaudio
    import soundfile as sf
    from PIL import ImageGrab

    from aibook.app import AIBookApp
    from aibook.editor import edit_group
    from aibook.engine import VoiceSpec
    from aibook.settings import SettingsStore
    from aibook.voice_library import cached_prompt_path
    from aibook.voice_studio_config import PRESETS, configure_environment
    from voice_studio_main import studio_profile

    configure_environment()
    voices = artifacts / "data" / "voices"
    voices.mkdir(parents=True)
    for speaker in PRESETS:
        original = cached_prompt_path(
            speaker, root_path / "data" / "voicestudio" / "voices"
        )
        if original.is_file():
            shutil.copyfile(original, voices / original.name)
    errors: list[str] = []
    root = tk.Tk()
    app = AIBookApp(root, studio_profile())
    report: dict = {
        "audio_backend": "null" if args.null_audio else "hardware",
        "playback_devices": len(miniaudio.Devices().get_playbacks()),
        "checks": [],
    }

    def wait(predicate, timeout=600):
        deadline = time.monotonic() + timeout
        while not predicate():
            root.update()
            if errors:
                raise RuntimeError(errors[0])
            if time.monotonic() > deadline:
                raise TimeoutError("Native reading acceptance timed out")
            time.sleep(0.02)
        root.update()

    def check(condition, name):
        if not condition:
            raise AssertionError(name)
        report["checks"].append(name)

    def screenshot(name):
        root.update_idletasks()
        handle = ctypes.windll.user32.GetAncestor(root.winfo_id(), 2)
        path = artifacts / name
        ImageGrab.grab(window=handle).save(path)
        return str(path)

    def close_app():
        if not app._closing:
            with patch("aibook.app.messagebox.askyesno", return_value=True):
                app._on_close()
        app.engine.unload()

    try:
        with patch(
            "aibook.app.messagebox.showerror",
            side_effect=lambda _title, message, **_: errors.append(message),
        ):
            document = root_path / "examples" / "sample_ru.fb2"
            document_before = document.read_bytes()
            with patch(
                "aibook.app.filedialog.askopenfilename", return_value=str(document)
            ):
                app._choose_document()
            wait(lambda: not app._document_pending)
            original_text = app.text.get("1.0", "end-1c")
            check(bool(original_text), "FB2 loaded through native UI")

            with edit_group(app.text):
                app.text.delete("1.0", "end")
                app.text.insert("1.0", "Раз два три.")
            root.update()
            root.focus_force()
            app.text.focus_set()
            root.update()
            app.text.tag_add("sel", "1.4", "1.7")
            app.text.event_generate("<Control-KeyPress-c>")
            check(app.text.clipboard_get() == "два", "Physical Ctrl+C")
            app.text.clipboard_clear()
            app.text.clipboard_append("новый")
            app.text.event_generate("<Control-KeyPress-v>")
            check(
                app.text.get("1.0", "end-1c") == "Раз новый три.",
                "Physical Ctrl+V replaces selection",
            )
            app.text.event_generate("<Control-KeyPress-z>")
            check(
                app.text.get("1.0", "end-1c") == "Раз два три.",
                "Paste is one undo operation",
            )
            app.text.tag_add("sel", "1.4", "1.7")
            app.text.event_generate("<BackSpace>")
            check(
                app.text.get("1.0", "end-1c") == "Раз  три.",
                "Physical Backspace deletes selection",
            )
            with edit_group(app.text):
                app.text.delete("1.0", "end")
                app.text.insert("1.0", original_text)
            app._on_text_modified()
            app.device.set(args.device)
            app.num_steps.set(16)
            reference = artifacts / "reference.wav"
            transcript = "Это проверка сохранённого голоса. После клонирования образец больше не нужен."
            future = app.synthesis_pool.submit(
                app.engine.synthesize_chunk,
                text=transcript,
                output_path=reference,
                voice=VoiceSpec(speaker="Рассказчик"),
                device_preference=args.device,
                temperature=0.65,
                speed=1,
                seed=42,
                num_steps=16,
                status=lambda message: app.events.put(("voice_progress", message)),
            )
            wait(future.done)
            future.result()
            app.voice_mode.set("clone")
            app.reference_path.set(str(reference))
            app.reference_text.set(transcript)
            app._update_voice_controls()
            with patch(
                "aibook.app.simpledialog.askstring", return_value="Проверочный голос"
            ):
                app.save_voice_button.invoke()
            wait(
                lambda: (
                    app.clone_future is not None
                    and app.clone_future.done()
                    and app.voice_mode.get() == "builtin"
                )
            )
            voice = app._selected_saved_voice()
            check(
                voice is not None and app._voice_spec().saved_prompt.is_file(),
                "Encoded clone selectable in voice list",
            )
            check(
                not app.reference_path.get() and not app.reference_text.get(),
                "Reference fields cleared after cloning",
            )
            reference.unlink()
            with patch(
                "aibook.app.simpledialog.askstring", return_value="Сохранённый диктор"
            ):
                app.rename_voice_button.invoke()
            check(
                app._selected_saved_voice().id == voice.id
                and app._selected_saved_voice().name == "Сохранённый диктор",
                "Clone rename preserves identity",
            )

            app.output_format.set("mp3")
            app.player_panel.mode.set("file")
            app.playback_speed.set(1.2)
            app.pitch_semitones.set(-0.5)
            app.volume_db.set(1)
            app.start_button.invoke()
            wait(lambda: app.last_output is not None)
            wait(
                lambda: (
                    not app.player_panel._loading
                    and not app.player_panel._alignment_pending
                )
            )
            panel = app.player_panel
            check(
                panel._timeline is not None and len(panel._timeline.words) > 10,
                "Final MP3 word alignment with tempo and pitch effects",
            )
            check(panel._reading is not None, "Exact editor-to-audio binding")
            report.update(
                audio=str(app.last_output),
                duration=sf.info(app.last_output).duration,
                words=len(panel._timeline.words),
                device=app.engine.device,
            )
            if args.null_audio:
                panel.player._device_factory = lambda **kwargs: (
                    miniaudio.PlaybackDevice(
                        backends=[miniaudio.Backend.NULL], **kwargs
                    )
                )
            panel.volume.set(0)
            panel.player.volume(0)
            selected = panel._timeline.words[8]
            app.text.tag_add(
                "sel", f"1.0+{selected.char_start}c", f"1.0+{selected.char_end}c"
            )
            panel.delete_check.invoke()
            panel.play_button.invoke()
            expected_start = panel._timeline.time_for_char(selected.char_start)
            check(
                abs(panel.player.snapshot().position - expected_start) < 0.15,
                "Playback starts at selected word",
            )
            wait(
                lambda: panel.player.snapshot().position >= expected_start + 2.5,
                timeout=15,
            )
            panel.play_button.invoke()
            check(
                panel._reading.removed
                and app.text.get("1.0", "end-1c") == panel._reading.remaining_text(),
                "Only spoken words removed from editor",
            )
            check(
                app.text.get("1.0", "end-1c").startswith(
                    original_text[: selected.char_start]
                ),
                "Unplayed prefix remains",
            )
            check(
                document.read_bytes() == document_before,
                "Original book remains unchanged",
            )
            before_close = panel.player.snapshot().position
            remaining = app.text.get("1.0", "end-1c")
            removed = list(panel._reading.removed)
            report["desktop_screenshot"] = screenshot("reading_desktop.png")
            check(app.text.winfo_height() >= 140, "Desktop editor remains usable")
            root.geometry("1050x800")
            root.update()
            check(app.text.winfo_height() >= 140, "Compact editor remains usable")
            report["compact_screenshot"] = screenshot("reading_compact.png")
            app.voice_mode.set("clone")
            app._update_voice_controls()
            root.update()
            check(
                app.text.winfo_height() >= 140,
                "Compact clone-mode editor remains usable",
            )
            report["clone_screenshot"] = screenshot("clone_compact.png")
            app.voice_mode.set("builtin")
            app._update_voice_controls()
            close_app()

            saved = SettingsStore().load()
            check(
                abs(saved["player_position"] - before_close) < 0.15,
                "Playback position saved on close",
            )
            root = tk.Tk()
            app = AIBookApp(root, studio_profile())
            wait(lambda: not app.player_panel._loading)
            panel = app.player_panel
            check(
                not panel.player.snapshot().playing
                and abs(panel.player.snapshot().position - before_close) < 0.15,
                "Paused position restored after restart",
            )
            check(
                app.text.get("1.0", "end-1c") == remaining
                and panel._reading.removed == removed,
                "Remaining text and offsets restored together",
            )
            check(
                app._selected_saved_voice().id == voice.id
                and not app.reference_path.get()
                and not app.reference_text.get(),
                "Clone usable after sample deletion and restart",
            )
            with patch("aibook.app.messagebox.askyesno", return_value=True):
                app.delete_voice_button.invoke()
            check(
                not app.voice_library.list() and not app.voice_library.import_cached(),
                "Deleted clone does not reappear after cache import",
            )
            close_app()
    finally:
        close_app()
    check(
        not any(thread.name.startswith("aibook-") for thread in threading.enumerate()),
        "All application worker threads stopped",
    )
    path = artifacts / "report.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(path), **report}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
