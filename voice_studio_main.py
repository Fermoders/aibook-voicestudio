from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
import threading
from pathlib import Path

from aibook.voice_studio_config import (
    PRESETS,
    SOURCE_COMMIT,
    SOURCE_VERSION,
    application_root,
    configure_environment,
    model_ready,
    studio_model_dir,
)

logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="AIBook VoiceStudio: local Russian audiobooks"
    )
    parser.add_argument("--text")
    parser.add_argument("--text-file", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--speaker", choices=tuple(PRESETS), default=next(iter(PRESETS))
    )
    parser.add_argument("--reference", type=Path)
    parser.add_argument("--reference-text", default="")
    parser.add_argument("--device", choices=("auto", "cuda", "cpu"), default="auto")
    parser.add_argument("--num-steps", type=int, choices=(16, 32, 64), default=32)
    parser.add_argument("--max-chars", type=int, default=220)
    parser.add_argument("--speed", type=float, default=1.0)
    parser.add_argument("--playback-speed", type=float, default=1.0)
    parser.add_argument("--pitch-semitones", type=float, default=0.0)
    parser.add_argument("--volume-db", type=float, default=0.0)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--ui-smoke", action="store_true")
    parser.add_argument("--doctor", action="store_true")
    return parser


def studio_profile():
    from aibook.app import AppProfile
    from aibook.voice_studio import VoiceStudioEngine

    return AppProfile(
        title="AIBook VoiceStudio",
        speakers=tuple(PRESETS),
        engine_factory=VoiceStudioEngine,
        license_label="OmniVoice: CC-BY-NC",
        license_url="https://huggingface.co/k2-fsa/OmniVoice",
        needs_cpml=False,
        voice_studio=True,
        icon_path=application_root() / "AIBook.ico",
    )


def main() -> int:
    configure_environment()
    from main import _configure_logging

    _configure_logging()
    args = build_parser().parse_args()
    if args.doctor:
        return doctor()
    if args.output:
        return synthesize(args)
    from aibook.app import run_app

    run_app(profile=studio_profile(), smoke_test=args.ui_smoke)
    return 0


def doctor() -> int:
    import torch

    report = {
        "application": "AIBook VoiceStudio",
        "voicestudio_version": SOURCE_VERSION,
        "voicestudio_commit": SOURCE_COMMIT,
        "model_ready": model_ready(),
        "model_dir": str(studio_model_dir()),
        "torch": torch.__version__,
        "cuda": torch.cuda.is_available(),
        "gpu": torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,
        "ffmpeg": shutil.which("ffmpeg"),
        "alignment_model_ready": (
            studio_model_dir().parent / "Whisper" / "tiny.pt"
        ).is_file(),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return (
        0
        if report["model_ready"]
        and report["ffmpeg"]
        and report["alignment_model_ready"]
        else 1
    )


def synthesize(args: argparse.Namespace) -> int:
    from aibook.audio import validate_reference_audio
    from aibook.engine import VoiceSpec
    from aibook.history import ResultHistory
    from aibook.job import SynthesisJob, SynthesisOptions
    from aibook.text_processing import load_document
    from aibook.voice_studio import VoiceStudioEngine

    if args.text and args.text_file:
        raise ValueError("Укажите --text или --text-file, не оба сразу")
    text = load_document(args.text_file) if args.text_file else args.text
    if not text:
        raise ValueError("Для озвучки укажите --text или --text-file")
    if args.reference:
        if validate_reference_audio(args.reference) > 20:
            raise ValueError("Образец должен быть от 3 до 20 секунд")
        voice = VoiceSpec(
            reference_audio=args.reference, reference_text=args.reference_text
        )
    else:
        voice = VoiceSpec(speaker=args.speaker)
    engine = VoiceStudioEngine()
    cancel = threading.Event()
    options = SynthesisOptions(
        output_path=args.output,
        voice=voice,
        device=args.device,
        max_chars=args.max_chars,
        num_steps=args.num_steps,
        speed=args.speed,
        playback_speed=args.playback_speed,
        pitch_semitones=args.pitch_semitones,
        volume_db=args.volume_db,
        seed=args.seed,
        include_text=True,
    )
    try:
        result = SynthesisJob(engine).run(
            text,
            options,
            cancel,
            lambda done, total, message: print(
                f"[{done}/{total}] {message}", flush=True
            ),
        )
        ResultHistory().add(result)
        print(result, flush=True)
        return 0
    finally:
        engine.unload()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        logger.exception("VoiceStudio application failed")
        if sys.stderr is not None:
            print(f"Ошибка: {error}", file=sys.stderr)
        else:
            from tkinter import messagebox

            messagebox.showerror("AIBook VoiceStudio", str(error))
        raise SystemExit(1)
