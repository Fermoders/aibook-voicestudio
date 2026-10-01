from __future__ import annotations

import argparse
import logging
import os
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

from aibook.config import (
    accept_noncommercial_license,
    app_data_dir,
    is_license_accepted,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AIBook XTTS v2")
    parser.add_argument(
        "--accept-cpml",
        action="store_true",
        help="Принять CPML для личного использования",
    )
    parser.add_argument(
        "--ui-smoke",
        action="store_true",
        help="Проверить запуск интерфейса и закрыть его",
    )
    parser.add_argument("--text", help="Текст для CLI-синтеза")
    parser.add_argument("--text-file", type=Path, help="Документ TXT/FB2/EPUB/DOCX/PDF")
    parser.add_argument("--output", type=Path, help="Итоговый WAV или MP3")
    parser.add_argument("--speaker", default="Ana Florence", help="Встроенный голос")
    parser.add_argument(
        "--reference", type=Path, help="Образец для клонирования голоса"
    )
    parser.add_argument("--device", choices=("auto", "cuda", "cpu"), default="auto")
    parser.add_argument("--max-chars", type=int, default=220)
    parser.add_argument("--temperature", type=float, default=0.65)
    parser.add_argument("--speed", type=float, default=1.0)
    parser.add_argument("--playback-speed", type=float, default=1.0)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--pitch-semitones", type=float, default=0.0)
    parser.add_argument("--volume-db", type=float, default=0.0)
    return parser


def main() -> int:
    _configure_logging()
    args = build_parser().parse_args()
    if args.accept_cpml:
        marker = accept_noncommercial_license()
        os.environ["COQUI_TOS_AGREED"] = "1"
        print(f"CPML accepted: {marker}")
        if not (args.output or args.text or args.text_file or args.ui_smoke):
            return 0

    if args.ui_smoke:
        from aibook.app import run_app

        run_app(smoke_test=True)
        return 0

    if args.output:
        return _run_cli(args)

    from aibook.app import run_app

    run_app()
    return 0


def _run_cli(args: argparse.Namespace) -> int:
    from aibook.engine import VoiceSpec, XTTSEngine
    from aibook.history import ResultHistory
    from aibook.job import SynthesisJob, SynthesisOptions
    from aibook.text_processing import load_document

    if not is_license_accepted():
        raise SystemExit("Сначала выполните: python main.py --accept-cpml")
    os.environ["COQUI_TOS_AGREED"] = "1"

    if args.text_file:
        text = load_document(args.text_file)
    elif args.text:
        text = args.text
    else:
        raise SystemExit("Для CLI укажите --text или --text-file")

    voice = (
        VoiceSpec(reference_audio=args.reference)
        if args.reference
        else VoiceSpec(speaker=args.speaker)
    )
    options = SynthesisOptions(
        output_path=args.output,
        voice=voice,
        device=args.device,
        max_chars=args.max_chars,
        temperature=args.temperature,
        speed=args.speed,
        seed=args.seed,
        pitch_semitones=args.pitch_semitones,
        volume_db=args.volume_db,
        playback_speed=args.playback_speed,
    )
    result = SynthesisJob(XTTSEngine()).run(
        text,
        options,
        threading.Event(),
        lambda done, total, message: print(f"[{done}/{total}] {message}", flush=True),
    )
    ResultHistory().add(result)
    print(result)
    return 0


def _configure_logging() -> None:
    directory = app_data_dir()
    directory.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        directory / "aibook.log",
        maxBytes=2_000_000,
        backupCount=2,
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    logging.basicConfig(level=logging.INFO, handlers=[handler])


if __name__ == "__main__":
    raise SystemExit(main())
