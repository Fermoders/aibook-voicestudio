from __future__ import annotations

import json
import math
import shutil
import subprocess
from collections.abc import Iterable
from pathlib import Path
from uuid import uuid4

import numpy as np
import soundfile as sf


def audio_duration(path: str | Path) -> float:
    source = Path(path)
    try:
        return float(sf.info(str(source)).duration)
    except RuntimeError:
        ffprobe = shutil.which("ffprobe")
        if not ffprobe:
            raise ValueError(
                "Не удалось прочитать аудиофайл и не найден ffprobe"
            ) from None
        result = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "json",
                str(source),
            ],
            check=True,
            capture_output=True,
            text=True,
            creationflags=_no_window_flag(),
        )
        return float(json.loads(result.stdout)["format"]["duration"])


def validate_reference_audio(path: str | Path) -> float:
    source = Path(path)
    if not source.is_file():
        raise FileNotFoundError(f"Образец голоса не найден: {source}")
    duration = audio_duration(source)
    if duration < 3.0:
        raise ValueError("Образец голоса должен быть длиннее 3 секунд")
    return duration


def prepare_reference_wav(
    source_path: str | Path, destination_path: str | Path
) -> Path:
    source = Path(source_path)
    destination = Path(destination_path)
    validate_reference_audio(source)
    if destination.is_file():
        try:
            info = sf.info(str(destination))
            if info.frames > 0 and info.samplerate == 22050 and info.channels == 1:
                return destination
        except RuntimeError:
            pass

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("Для подготовки образца голоса требуется ffmpeg")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".tmp.wav")
    temporary.unlink(missing_ok=True)
    try:
        subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(source),
                "-ac",
                "1",
                "-ar",
                "22050",
                "-codec:a",
                "pcm_s16le",
                str(temporary),
            ],
            check=True,
            creationflags=_no_window_flag(),
        )
        info = sf.info(str(temporary))
        if info.frames <= 0 or info.samplerate != 22050 or info.channels != 1:
            raise RuntimeError("FFmpeg создал некорректный образец голоса")
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def combine_chunks(
    chunk_paths: Iterable[Path],
    pauses_ms: Iterable[int],
    output_path: str | Path,
    *,
    volume_db: float = 0.0,
    pitch_semitones: float = 0.0,
    playback_speed: float = 1.0,
) -> Path:
    chunks = list(chunk_paths)
    pauses = list(pauses_ms)
    if not chunks:
        raise ValueError("Нет аудиофрагментов для сборки")
    if len(chunks) != len(pauses):
        raise ValueError("Количество пауз не совпадает с количеством фрагментов")

    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.suffix.lower() not in {".wav", ".mp3"}:
        raise ValueError("Поддерживаются итоговые форматы WAV и MP3")
    if not -20.0 <= volume_db <= 12.0:
        raise ValueError("Громкость должна быть от -20 до +12 дБ")
    if not -6.0 <= pitch_semitones <= 6.0:
        raise ValueError("Тембр должен быть от -6 до +6 полутонов")
    if not 0.5 <= playback_speed <= 3.0:
        raise ValueError("Скорость файла должна быть от 0.5 до 3.0")

    effects_enabled = (
        abs(volume_db) > 0.001
        or abs(pitch_semitones) > 0.001
        or abs(playback_speed - 1.0) > 0.001
    )
    intermediate = destination.with_name(
        f"{destination.stem}.{uuid4().hex}.aibook.raw.wav"
    )
    try:
        _combine_wav(chunks, pauses, intermediate)
        if destination.suffix.lower() == ".mp3" or effects_enabled:
            _render_output(
                intermediate,
                destination,
                volume_db=volume_db,
                pitch_semitones=pitch_semitones,
                playback_speed=playback_speed,
            )
        else:
            intermediate.replace(destination)
    finally:
        intermediate.unlink(missing_ok=True)
    return destination


def _combine_wav(chunks: list[Path], pauses: list[int], output: Path) -> None:
    first = sf.info(str(chunks[0]))
    sample_rate = first.samplerate
    with sf.SoundFile(
        str(output),
        mode="w",
        samplerate=sample_rate,
        channels=1,
        subtype="PCM_16",
        format="WAV",
    ) as target:
        for source_path, pause_ms in zip(chunks, pauses, strict=True):
            info = sf.info(str(source_path))
            if info.samplerate != sample_rate:
                raise ValueError(f"Частота дискретизации отличается: {source_path}")
            with sf.SoundFile(str(source_path), mode="r") as source:
                while True:
                    block = source.read(65536, dtype="float32", always_2d=True)
                    if not len(block):
                        break
                    target.write(block.mean(axis=1))
            silence_frames = round(sample_rate * max(0, pause_ms) / 1000)
            if silence_frames:
                target.write(np.zeros(silence_frames, dtype=np.float32))


def _render_output(
    source: Path,
    destination: Path,
    *,
    volume_db: float,
    pitch_semitones: float,
    playback_speed: float,
) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError(
            "Для MP3 и обработки звука требуется ffmpeg. Установите ffmpeg."
        )

    filters: list[str] = []
    if abs(pitch_semitones) > 0.001 or abs(playback_speed - 1.0) > 0.001:
        pitch_ratio = math.pow(2.0, pitch_semitones / 12.0)
        filters.append(
            f"rubberband=tempo={playback_speed:.6f}:pitch={pitch_ratio:.8f}:pitchq=quality"
        )
    if abs(volume_db) > 0.001:
        filters.append(f"volume={volume_db:+.2f}dB")
    if filters:
        filters.append("alimiter=limit=0.97:level=false:latency=true")

    temporary = destination.with_name(
        f"{destination.stem}.{uuid4().hex}.aibook.output.tmp{destination.suffix.lower()}"
    )
    temporary.unlink(missing_ok=True)
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source),
    ]
    if filters:
        command.extend(["-af", ",".join(filters)])
    if destination.suffix.lower() == ".mp3":
        command.extend(["-codec:a", "libmp3lame", "-b:a", "128k"])
    else:
        command.extend(["-codec:a", "pcm_s16le"])
    command.append(str(temporary))

    try:
        subprocess.run(
            command,
            check=True,
            creationflags=_no_window_flag(),
        )
        info = sf.info(str(temporary))
        if info.frames <= 0:
            raise RuntimeError("FFmpeg создал пустой итоговый файл")
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def _no_window_flag() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)
