from __future__ import annotations

import argparse
import ctypes
import json
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Exercise the real native audiobook UI"
    )
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--device", choices=("cpu", "cuda", "auto"), default="auto")
    args = parser.parse_args()
    root_path = args.root.resolve()
    sys.path.insert(
        0, str(root_path / "app" if (root_path / "app").is_dir() else root_path)
    )

    import tkinter as tk

    import soundfile as sf
    from PIL import ImageGrab

    from aibook.app import AIBookApp
    from aibook.voice_studio_config import configure_environment
    from voice_studio_main import studio_profile

    configure_environment()
    root = tk.Tk()
    app = AIBookApp(root, studio_profile())
    app.device.set(args.device)
    app.output_format.set("mp3")
    app.voice_mode.set("builtin")
    app.speaker.set("Рассказчик")
    app.num_steps.set(16)
    app.max_chars.set(220)
    app.output_folder.set(str(root_path / "outputs"))
    app._update_voice_controls()
    document = root_path / "examples" / "sample_ru.fb2"
    with patch("aibook.app.filedialog.askopenfilename", return_value=str(document)):
        app._choose_document()

    started = time.monotonic()
    began = False
    result: dict[str, object] = {}
    errors: list[str] = []

    def poll() -> None:
        nonlocal began
        if errors or time.monotonic() - started > 600:
            app.cancel_event.set()
            result["error"] = errors[0] if errors else "UI acceptance timed out"
            root.destroy()
            return
        if (
            not began
            and not app._document_pending
            and app.text.get("1.0", "end-1c").strip()
        ):
            began = True
            app._start_synthesis()
            if app.job_future is None:
                errors.append("UI did not start synthesis")
        if app.last_output is not None:
            info = sf.info(app.last_output)
            result.update(
                file=str(app.last_output),
                duration=info.duration,
                samplerate=info.samplerate,
                device=app.engine.device or "cached",
                document_loaded=str(document),
                screenshot=str(root_path / "outputs" / "VoiceStudio_UI.png"),
            )
            root.update_idletasks()
            handle = ctypes.windll.user32.GetAncestor(root.winfo_id(), 2)
            ImageGrab.grab(window=handle).save(str(result["screenshot"]))
            app._on_close()
            return
        root.after(100, poll)

    root.after(100, poll)
    try:
        with patch(
            "aibook.app.messagebox.showerror",
            side_effect=lambda _title, message, **_: errors.append(message),
        ):
            root.mainloop()
    finally:
        app.cancel_event.set()
        app.engine.unload()
        app.io_pool.shutdown(wait=True, cancel_futures=True)
        app.settings_pool.shutdown(wait=True, cancel_futures=True)
        app.synthesis_pool.shutdown(wait=True, cancel_futures=True)
    if not result.get("file"):
        raise RuntimeError(
            str(result.get("error", "UI verification was not completed"))
        )
    report = root_path / "outputs" / "VoiceStudio_UI_acceptance.json"
    report.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if any(thread.name.startswith("aibook-") for thread in threading.enumerate()):
        raise RuntimeError("Application worker threads did not stop")


if __name__ == "__main__":
    main()
