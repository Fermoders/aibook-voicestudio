from __future__ import annotations

import hashlib
import importlib.metadata
import json
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import soundfile as sf
from filelock import FileLock, Timeout

from .audio import combine_chunks
from .config import MODEL_NAME, jobs_dir, model_dir
from .engine import VoiceSpec
from .text_processing import TextChunk, split_into_chunks

ProgressCallback = Callable[[int, int, str], None]


class JobCancelled(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SynthesisOptions:
    output_path: Path
    voice: VoiceSpec
    device: str = "auto"
    max_chars: int = 220
    temperature: float = 0.65
    speed: float = 1.0
    seed: int = 42
    pitch_semitones: float = 0.0
    volume_db: float = 0.0
    playback_speed: float = 1.0
    num_steps: int | None = None
    include_text: bool = False


class SynthesisJob:
    def __init__(self, engine: Any) -> None:
        self.engine = engine

    def run(
        self,
        text: str,
        options: SynthesisOptions,
        cancel_event: threading.Event,
        progress: ProgressCallback,
    ) -> Path:
        if not 0.7 <= options.speed <= 3.0:
            raise ValueError("Скорость должна быть от 0.7 до 3.0")
        if not -6.0 <= options.pitch_semitones <= 6.0:
            raise ValueError("Тембр должен быть от -6 до +6 полутонов")
        if not -20.0 <= options.volume_db <= 12.0:
            raise ValueError("Громкость должна быть от -20 до +12 дБ")
        if not 0.5 <= options.playback_speed <= 3.0:
            raise ValueError("Скорость файла должна быть от 0.5 до 3.0")
        if options.num_steps is not None and not 4 <= options.num_steps <= 64:
            raise ValueError("Число шагов должно быть от 4 до 64")
        chunks = split_into_chunks(text, max_chars=options.max_chars)
        if not chunks:
            raise ValueError("Нет текста для озвучки")

        work_dir = jobs_dir() / self._job_id(chunks, options)
        work_dir.mkdir(parents=True, exist_ok=True)
        lock = FileLock(str(work_dir / "job.lock"))
        announced = False
        while True:
            if cancel_event.is_set():
                raise JobCancelled("Озвучка остановлена")
            try:
                lock.acquire(timeout=0.2)
                break
            except Timeout:
                if not announced:
                    progress(
                        0,
                        len(chunks),
                        "Ожидание другого задания с теми же параметрами...",
                    )
                    announced = True
        try:
            result = self._run_locked(chunks, work_dir, options, cancel_event, progress)
            if options.include_text:
                from .timeline import AudioTimeline, audio_stamp

                AudioTimeline(text, audio_stamp(result)).save(result)
            return result
        finally:
            lock.release()

    def _run_locked(
        self,
        chunks: list[TextChunk],
        work_dir: Path,
        options: SynthesisOptions,
        cancel_event: threading.Event,
        progress: ProgressCallback,
    ) -> Path:
        self._write_manifest(work_dir, chunks, options)
        chunk_paths: list[Path] = []
        total = len(chunks)

        for index, chunk in enumerate(chunks):
            if cancel_event.is_set():
                raise JobCancelled(
                    "Озвучка остановлена. Готовые фрагменты сохранены для продолжения."
                )

            chunk_path = work_dir / f"chunk_{index:06d}.wav"
            chunk_paths.append(chunk_path)
            if _valid_wav(chunk_path):
                progress(
                    index + 1,
                    total,
                    f"Фрагмент {index + 1}/{total}: используется готовый",
                )
                continue

            progress(index, total, f"Фрагмент {index + 1}/{total}: синтез")
            temporary = chunk_path.with_name(f"{chunk_path.stem}.{uuid4().hex}.tmp.wav")
            kwargs: dict[str, Any] = {
                "text": chunk.text,
                "output_path": temporary,
                "voice": options.voice,
                "device_preference": options.device,
                "temperature": options.temperature,
                "speed": options.speed,
                "seed": options.seed + index,
                "status": lambda message, i=index: progress(i, total, message),
            }
            if getattr(self.engine, "interruptible", False):
                kwargs.update(
                    num_steps=options.num_steps or 32, cancel_event=cancel_event
                )
            try:
                device = self.engine.synthesize_chunk(**kwargs)
                if not _valid_wav(temporary):
                    raise RuntimeError(
                        f"Движок создал повреждённый фрагмент: {chunk_path.name}"
                    )
                temporary.replace(chunk_path)
            finally:
                temporary.unlink(missing_ok=True)
            progress(
                index + 1,
                total,
                f"Фрагмент {index + 1}/{total} готов ({device.upper()})",
            )

        if cancel_event.is_set():
            raise JobCancelled("Озвучка остановлена перед сборкой файла")

        progress(total, total, "Сборка итогового аудиофайла...")
        result = combine_chunks(
            chunk_paths,
            [chunk.pause_ms for chunk in chunks],
            options.output_path,
            volume_db=options.volume_db,
            pitch_semitones=options.pitch_semitones,
            playback_speed=options.playback_speed,
        )
        progress(total, total, f"Готово: {result}")
        return result

    def _signature(self) -> str:
        return getattr(self.engine, "cache_signature", None) or _model_signature()

    def _voice_signature(self, voice: VoiceSpec) -> str:
        if hasattr(self.engine, "voice_signature"):
            return self.engine.voice_signature(voice)
        return voice.speaker or voice.cache_id

    def _job_id(self, chunks: list[TextChunk], options: SynthesisOptions) -> str:
        voice_value = self._voice_signature(options.voice)
        payload = {
            "model": self._signature(),
            "chunks": [asdict(chunk) for chunk in chunks],
            "voice": voice_value,
            "device_independent": True,
            "temperature": options.temperature,
            "speed": options.speed,
            "seed": options.seed,
        }
        if options.num_steps is not None:
            payload["num_steps"] = options.num_steps
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode(
            "utf-8"
        )
        return hashlib.sha256(encoded).hexdigest()[:24]

    def _write_manifest(
        self, work_dir: Path, chunks: list[TextChunk], options: SynthesisOptions
    ) -> None:
        manifest = {
            "version": 1,
            "model": self._signature(),
            "output": str(options.output_path),
            "voice": self._voice_signature(options.voice),
            "settings": {
                "max_chars": options.max_chars,
                "temperature": options.temperature,
                "speed": options.speed,
                "seed": options.seed,
                "pitch_semitones": options.pitch_semitones,
                "volume_db": options.volume_db,
                "playback_speed": options.playback_speed,
                "num_steps": options.num_steps,
            },
            "chunks": [asdict(chunk) for chunk in chunks],
        }
        temporary = work_dir / "manifest.json.tmp"
        temporary.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(work_dir / "manifest.json")


def _valid_wav(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size < 128:
        return False
    try:
        info = sf.info(str(path))
    except RuntimeError:
        return False
    return info.frames > 0 and info.samplerate > 0


def _model_signature() -> str:
    hash_file = model_dir() / "hash.md5"
    model_hash = (
        hash_file.read_text(encoding="utf-8").strip()
        if hash_file.is_file()
        else "not-downloaded"
    )
    return f"{MODEL_NAME}|{importlib.metadata.version('coqui-tts')}|{model_hash}"
