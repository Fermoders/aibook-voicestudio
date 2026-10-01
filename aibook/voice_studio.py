from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from .config import app_data_dir
from .engine import StatusCallback, VoiceSpec
from .voice_library import cached_prompt_path
from .voice_studio_config import (
    MODEL_REVISION,
    PRESETS,
    SOURCE_COMMIT,
    model_ready,
    studio_source_dir,
    workspace_dir,
)


class VoiceStudioEngine:
    interruptible = True

    def __init__(
        self, *, timeout: float = 900.0, command: list[str] | None = None
    ) -> None:
        self._lock = threading.RLock()
        self._process: subprocess.Popen[bytes] | None = None
        self._messages: queue.Queue[dict[str, Any]] = queue.Queue()
        self._reader: threading.Thread | None = None
        self._log: Any = None
        self._device: str | None = None
        self._preference: str | None = None
        self._timeout = timeout
        self._command = command

    @property
    def device(self) -> str | None:
        with self._lock:
            return self._device

    @property
    def cache_signature(self) -> str:
        packages = "|".join(
            importlib.metadata.version(name)
            for name in ("torch", "torchaudio", "transformers")
        )
        return f"VoiceStudio|{SOURCE_COMMIT}|OmniVoice|{MODEL_REVISION}|preset-v1|{packages}"

    def voice_signature(self, voice: VoiceSpec) -> str:
        if voice.saved_prompt is not None:
            with voice.saved_prompt.open("rb") as source:
                return "saved_" + hashlib.file_digest(source, "sha256").hexdigest()
        if voice.reference_audio is None:
            return voice.speaker or ""
        digest = hashlib.sha256()
        with voice.reference_audio.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
        digest.update(voice.reference_text.strip().encode("utf-8"))
        return "clone_" + digest.hexdigest()

    def _voice_request(self, voice: VoiceSpec) -> dict[str, Any]:
        voice.validate()
        if voice.reference_audio is not None and not voice.reference_text.strip():
            raise ValueError("Введите точный текст, произнесённый в образце голоса")
        if voice.speaker and voice.speaker not in PRESETS:
            raise ValueError(f"Неизвестный голос VoiceStudio: {voice.speaker}")
        return {
            "speaker": voice.speaker,
            "reference": str(voice.reference_audio.resolve())
            if voice.reference_audio
            else None,
            "reference_text": voice.reference_text.strip(),
            "voice_id": self.voice_signature(voice),
            "saved_prompt": str(voice.saved_prompt.resolve())
            if voice.saved_prompt
            else None,
        }

    def create_voice(
        self,
        voice: VoiceSpec,
        device: str,
        status: StatusCallback,
        cancel_event: threading.Event,
    ) -> Path:
        if voice.reference_audio is None:
            raise ValueError("Выберите образец для клонирования")
        request = {"op": "clone", "device": device, **self._voice_request(voice)}
        self._exchange(request, "clone", status, cancel_event)
        path = cached_prompt_path(request["voice_id"])
        if not path.is_file():
            raise RuntimeError("Движок не сохранил голосовой профиль")
        return path

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
        num_steps: int = 32,
        cancel_event: threading.Event | None = None,
    ) -> str:
        if device_preference not in {"auto", "cuda", "cpu"}:
            raise ValueError("Устройство должно быть auto, cuda или cpu")
        if not 4 <= num_steps <= 64:
            raise ValueError("Число шагов должно быть от 4 до 64")
        cancel_event = cancel_event or threading.Event()
        request = {
            "op": "synthesize",
            "text": text,
            "output": str(output_path.resolve()),
            **self._voice_request(voice),
            "device": device_preference,
            "speed": speed,
            "seed": seed,
            "num_steps": num_steps,
        }
        result = self._exchange(request, "audio", status, cancel_event)
        return str(result["device"])

    def _exchange(
        self,
        request: dict[str, Any],
        expected: str,
        status: StatusCallback,
        cancel_event: threading.Event,
    ) -> dict[str, Any]:
        device_preference = request["device"]
        if device_preference not in {"auto", "cuda", "cpu"}:
            raise ValueError("Устройство должно быть auto, cuda или cpu")
        with self._lock:
            if cancel_event.is_set():
                from .job import JobCancelled

                raise JobCancelled("Озвучка остановлена")
            if self._preference != device_preference:
                self._stop_locked()
            try:
                self._start_locked(status, cancel_event)
                assert self._process is not None and self._process.stdin is not None
                self._preference = device_preference
                self._process.stdin.write(
                    (json.dumps(request, ensure_ascii=True) + "\n").encode()
                )
                self._process.stdin.flush()
                result = self._wait_locked(
                    expected, status, cancel_event, self._timeout
                )
                self._device = str(result["device"])
                return result
            except BaseException:
                self._stop_locked()
                raise

    def unload(self) -> None:
        with self._lock:
            self._stop_locked()

    def _start_locked(self, status: StatusCallback, cancel: threading.Event) -> None:
        if self._process is not None and self._process.poll() is None:
            return
        self._stop_locked()
        if self._command is None:
            if not model_ready():
                raise RuntimeError(
                    "Модель OmniVoice не установлена. Выполните install_voicestudio.ps1."
                )
            if not (
                studio_source_dir() / "omnivoice" / "models" / "omnivoice.py"
            ).is_file():
                raise RuntimeError("Не найдены исходники движка VoiceStudio")
        executable = Path(sys.executable)
        if executable.name.lower() == "pythonw.exe":
            executable = executable.with_name("python.exe")
        command = self._command or [
            str(executable),
            "-u",
            "-m",
            "aibook.voice_studio_worker",
        ]
        directory = app_data_dir()
        directory.mkdir(parents=True, exist_ok=True)
        self._log = (directory / "voicestudio-worker.log").open("ab", buffering=0)
        environment = os.environ.copy()
        environment.update(
            {
                "HF_HUB_OFFLINE": "1",
                "TRANSFORMERS_OFFLINE": "1",
                "HF_HUB_DISABLE_TELEMETRY": "1",
                "HF_HUB_DISABLE_IMPLICIT_TOKEN": "1",
                "PYTHONUTF8": "1",
                "PYTHONNOUSERSITE": "1",
                "OMP_NUM_THREADS": "8",
            }
        )
        self._messages = queue.Queue()
        self._process = subprocess.Popen(
            command,
            cwd=workspace_dir(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._log,
            env=environment,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        assert self._process.stdout is not None
        self._reader = threading.Thread(
            target=_read_messages,
            args=(self._process.stdout, self._messages),
            daemon=True,
            name="voicestudio-output",
        )
        self._reader.start()
        self._wait_locked("ready", status, cancel, 30.0)

    def _wait_locked(
        self,
        expected: str,
        status: StatusCallback,
        cancel: threading.Event,
        timeout: float,
    ) -> dict[str, Any]:
        from .job import JobCancelled

        deadline = time.monotonic() + timeout
        while True:
            if cancel.is_set():
                raise JobCancelled("Озвучка остановлена. Готовые фрагменты сохранены.")
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    "VoiceStudio превысил время ожидания. Попробуйте меньший фрагмент."
                )
            try:
                message = self._messages.get(timeout=0.1)
            except queue.Empty:
                continue
            operation = message.get("op")
            if operation == expected:
                return message
            if operation == "status":
                status(str(message.get("message", "")))
            elif operation == "error":
                raise RuntimeError(str(message.get("message", "Ошибка VoiceStudio")))
            elif operation == "eof":
                raise RuntimeError(
                    "Процесс VoiceStudio завершился. Подробности: voicestudio-worker.log"
                )

    def _stop_locked(self) -> None:
        process = self._process
        if process is not None:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
            if self._reader is not None:
                self._reader.join(timeout=2)
            for stream in (process.stdin, process.stdout):
                if stream is not None:
                    stream.close()
        self._process = None
        self._reader = None
        self._device = None
        self._preference = None
        if self._log is not None:
            self._log.close()
            self._log = None


def _read_messages(stream: Any, messages: queue.Queue[dict[str, Any]]) -> None:
    try:
        for line in stream:
            if len(line) > 65536:
                raise ValueError("Слишком большое сообщение движка")
            value = json.loads(line)
            if not isinstance(value, dict):
                raise TypeError("Некорректное сообщение движка")
            messages.put(value)
    except (OSError, ValueError, TypeError) as error:
        messages.put(
            {"op": "error", "message": f"Ошибка протокола VoiceStudio: {error}"}
        )
    finally:
        messages.put({"op": "eof"})
