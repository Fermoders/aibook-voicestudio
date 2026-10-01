from __future__ import annotations

import gc
import hashlib
import importlib.metadata
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .config import MODEL_NAME, model_dir, voices_dir

StatusCallback = Callable[[str], None]

BUILTIN_SPEAKERS = [
    "Ana Florence",
    "Asya Anara",
    "Lidiya Szekeres",
    "Sofia Hellen",
    "Viktor Eka",
    "Viktor Menelaos",
    "Andrew Chipper",
    "Royston Min",
    "Craig Gutsy",
    "Nova Hogarth",
]


@dataclass(frozen=True, slots=True)
class VoiceSpec:
    speaker: str | None = None
    reference_audio: Path | None = None
    reference_text: str = ""
    saved_prompt: Path | None = None

    def validate(self) -> None:
        if (
            sum(
                bool(value)
                for value in (self.speaker, self.reference_audio, self.saved_prompt)
            )
            != 1
        ):
            raise ValueError(
                "Выберите готовый голос, сохранённый клон или один образец"
            )

    @property
    def cache_id(self) -> str:
        if self.reference_audio is None:
            return ""
        source = self.reference_audio.resolve()
        stat = source.stat()
        signature = (
            f"{source}|{stat.st_size}|{stat.st_mtime_ns}|{_model_signature()}"
        ).encode()
        return f"voice_{hashlib.sha256(signature).hexdigest()[:20]}"


class XTTSEngine:
    def __init__(self) -> None:
        self._api = None
        self._device: str | None = None
        self._lock = threading.RLock()

    @property
    def device(self) -> str | None:
        return self._device

    def ensure_loaded(self, preference: str, status: StatusCallback) -> str:
        with self._lock:
            requested = self._resolve_device(preference)
            if self._api is not None and self._device == requested:
                return requested
            self._unload_locked()
            try:
                self._load_locked(requested, status)
            except RuntimeError as exc:
                if requested != "cuda" or not _is_cuda_memory_error(exc):
                    raise
                status("Недостаточно видеопамяти. Переключение на CPU...")
                self._unload_locked()
                self._load_locked("cpu", status)
            return self._device or "cpu"

    def synthesize_chunk(
        self,
        *,
        text: str,
        output_path: Path,
        voice: VoiceSpec,
        device_preference: str,
        temperature: float,
        speed: float,
        seed: int,
        status: StatusCallback,
    ) -> str:
        voice.validate()
        if voice.saved_prompt is not None:
            raise ValueError("Сохранённый профиль OmniVoice нельзя использовать в XTTS")
        with self._lock:
            self.ensure_loaded(device_preference, status)
            try:
                self._synthesize_locked(
                    text, output_path, voice, temperature, speed, seed
                )
            except RuntimeError as exc:
                if self._device != "cuda" or not _is_cuda_memory_error(exc):
                    raise
                status("Видеопамять закончилась во время синтеза. Повтор на CPU...")
                self._unload_locked()
                self._load_locked("cpu", status)
                self._synthesize_locked(
                    text, output_path, voice, temperature, speed, seed
                )
            return self._device or "cpu"

    def unload(self) -> None:
        with self._lock:
            self._unload_locked()

    def _load_locked(self, device: str, status: StatusCallback) -> None:
        status(f"Загрузка XTTS v2 на {device.upper()}...")
        import torch
        from TTS.api import TTS

        _install_xtts_audio_loader()
        torch.set_num_threads(max(1, min(8, os.cpu_count() or 1)))
        api = TTS(MODEL_NAME, progress_bar=False)
        api.to(device)
        self._api = api
        self._device = device
        status(f"XTTS v2 готов, устройство: {device.upper()}")

    def _synthesize_locked(
        self,
        text: str,
        output_path: Path,
        voice: VoiceSpec,
        temperature: float,
        speed: float,
        seed: int,
    ) -> None:
        if self._api is None:
            raise RuntimeError("Модель XTTS не загружена")

        import torch

        torch.manual_seed(seed)
        if torch.cuda.is_available():
            torch.cuda.manual_seed_all(seed)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        kwargs = {
            "text": text,
            "language": "ru",
            "file_path": str(output_path),
            "split_sentences": False,
            "temperature": temperature,
            "speed": speed,
        }
        if voice.reference_audio is not None:
            voice_file = voices_dir() / f"{voice.cache_id}.pth"
            kwargs.update({"speaker": voice.cache_id, "voice_dir": str(voices_dir())})
            if not voice_file.is_file():
                from .audio import prepare_reference_wav

                prepared = prepare_reference_wav(
                    voice.reference_audio,
                    voices_dir() / f"{voice.cache_id}.reference.wav",
                )
                kwargs["speaker_wav"] = str(prepared)
        else:
            kwargs["speaker"] = voice.speaker

        self._api.tts_to_file(**kwargs)

    def _unload_locked(self) -> None:
        self._api = None
        self._device = None
        gc.collect()
        try:
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except ImportError:
            pass

    @staticmethod
    def _resolve_device(preference: str) -> str:
        normalized = preference.lower()
        if normalized not in {"auto", "cuda", "cpu"}:
            raise ValueError(f"Неизвестное устройство: {preference}")
        if normalized == "cpu":
            return "cpu"

        import torch

        if torch.cuda.is_available():
            return "cuda"
        if normalized == "cuda":
            raise RuntimeError("CUDA недоступна. Выберите Auto или CPU.")
        return "cpu"


def _is_cuda_memory_error(error: BaseException) -> bool:
    message = str(error).lower()
    return "cuda" in message and (
        "out of memory" in message or "memory allocation" in message
    )


def _model_signature() -> str:
    package_version = importlib.metadata.version("coqui-tts")
    hash_file = model_dir() / "hash.md5"
    model_hash = (
        hash_file.read_text(encoding="utf-8").strip()
        if hash_file.is_file()
        else "not-downloaded"
    )
    return f"{MODEL_NAME}|{package_version}|{model_hash}"


def _install_xtts_audio_loader() -> None:
    import soundfile as sf
    import torch
    import torchaudio
    from TTS.tts.models import xtts as xtts_module

    if getattr(xtts_module.load_audio, "_aibook_adapter", False):
        return

    def load_audio_with_soundfile(
        audiopath: str | os.PathLike[str], sampling_rate: int
    ):
        samples, source_rate = sf.read(str(audiopath), dtype="float32", always_2d=True)
        audio = torch.from_numpy(samples.T.copy())
        if audio.size(0) != 1:
            audio = torch.mean(audio, dim=0, keepdim=True)
        if source_rate != sampling_rate:
            audio = torchaudio.functional.resample(audio, source_rate, sampling_rate)
        audio.clip_(-1, 1)
        return audio

    load_audio_with_soundfile._aibook_adapter = True  # type: ignore[attr-defined]
    xtts_module.load_audio = load_audio_with_soundfile
