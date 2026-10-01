from __future__ import annotations

import gc
import json
import os
import re
import sys
import traceback
from pathlib import Path
from typing import Any

from .config import voices_dir
from .voice_library import cached_prompt_path
from .voice_studio_config import (
    PRESET_TEXT,
    PRESETS,
    configure_environment,
    studio_model_dir,
    studio_source_dir,
)


class StudioWorker:
    def __init__(self, send: Any) -> None:
        self.send = send
        self.model: Any = None
        self.device: str | None = None
        self.prompts: dict[str, Any] = {}
        self._fallback_cpu = False

    def load(self, preference: str) -> None:
        import torch
        from omnivoice import OmniVoice

        if preference == "cuda" and not torch.cuda.is_available():
            raise RuntimeError("CUDA недоступна. Выберите auto или cpu.")
        device = (
            "cuda"
            if preference != "cpu"
            and not self._fallback_cpu
            and torch.cuda.is_available()
            else "cpu"
        )
        if self.model is not None and self.device == device:
            return
        self.model = None
        self.prompts.clear()
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        self.send(
            {
                "op": "status",
                "message": f"Загрузка VoiceStudio / OmniVoice на {device.upper()}...",
            }
        )
        torch.set_num_threads(min(8, os.cpu_count() or 1))
        self.model = OmniVoice.from_pretrained(
            str(studio_model_dir()),
            device_map=device,
            dtype=torch.float16 if device == "cuda" else torch.float32,
            load_asr=False,
            local_files_only=True,
            trust_remote_code=False,
        )
        self.device = device
        self.send({"op": "status", "message": f"OmniVoice готов: {device.upper()}"})

    def prompt(self, request: dict[str, Any]) -> Any:
        import torch
        from filelock import FileLock
        from omnivoice.models.omnivoice import VoiceClonePrompt

        voice_id = request["voice_id"]
        if voice_id in self.prompts:
            return self.prompts[voice_id]
        if request.get("saved_prompt"):
            path = Path(request["saved_prompt"])
            with FileLock(str(path.with_suffix(".lock")), timeout=30):
                prompt = VoiceClonePrompt.load(str(path))
            self.prompts[voice_id] = prompt
            return prompt
        directory = voices_dir()
        prompt_path = cached_prompt_path(voice_id, directory)
        prepared: Path | None = None
        with FileLock(str(prompt_path.with_suffix(".lock")), timeout=600):
            if prompt_path.is_file():
                try:
                    prompt = VoiceClonePrompt.load(str(prompt_path))
                    self.prompts[voice_id] = prompt
                    return prompt
                except (ValueError, RuntimeError, EOFError):
                    prompt_path.unlink(missing_ok=True)
            reference = request.get("reference")
            reference_text = request.get("reference_text")
            if reference:
                import soundfile as sf

                from .audio import prepare_reference_wav

                path = prepare_reference_wav(
                    Path(reference), prompt_path.with_suffix(".reference.wav")
                )
                prepared = path
                samples, rate = sf.read(path, dtype="float32", always_2d=True)
                reference_audio = (torch.from_numpy(samples.T.copy()), rate)
            else:
                speaker = request["speaker"]
                self.send(
                    {
                        "op": "status",
                        "message": f"Подготовка постоянного голоса: {speaker}...",
                    }
                )
                torch.manual_seed(12345)
                sample = self.model.generate(
                    text=PRESET_TEXT,
                    language="Russian",
                    instruct=PRESETS[speaker],
                    num_step=32,
                )[0]
                reference_audio = (sample, self.model.sampling_rate)
                reference_text = PRESET_TEXT
            prompt = self.model.create_voice_clone_prompt(
                ref_audio=reference_audio,
                ref_text=reference_text,
            )
            temporary = prompt_path.with_suffix(f".{os.getpid()}.tmp")
            try:
                prompt.save(str(temporary))
                temporary.replace(prompt_path)
            finally:
                temporary.unlink(missing_ok=True)
                if prepared is not None:
                    prepared.unlink(missing_ok=True)
            self.prompts[voice_id] = prompt
            return prompt

    def synthesize(self, request: dict[str, Any]) -> None:
        import torch

        self.load(request["device"])
        try:
            self._synthesize(request)
        except torch.OutOfMemoryError:
            if self.device != "cuda" or request["device"] != "auto":
                raise
            self.send(
                {
                    "op": "status",
                    "message": "Недостаточно видеопамяти. Повтор на CPU...",
                }
            )
            self._fallback_cpu = True
            self.load("cpu")
            self._synthesize(request)

    def _synthesize(self, request: dict[str, Any]) -> None:
        import numpy as np
        import soundfile as sf
        import torch

        prompt = self.prompt(request)
        torch.manual_seed(int(request["seed"]))
        self.send(
            {
                "op": "status",
                "message": f"Синтез VoiceStudio ({self.device.upper()}, {request['num_steps']} шагов)...",
            }
        )
        result = self.model.generate(
            text=request["text"],
            language="Russian",
            voice_clone_prompt=prompt,
            speed=float(request["speed"]),
            num_step=int(request["num_steps"]),
        )[0]
        samples = result.detach().float().cpu().numpy().reshape(-1)
        if (
            not len(samples)
            or not np.isfinite(samples).all()
            or float(np.max(np.abs(samples))) < 1e-6
        ):
            raise RuntimeError("OmniVoice создал пустой, тихий или повреждённый звук")
        output = Path(request["output"])
        output.parent.mkdir(parents=True, exist_ok=True)
        sf.write(
            str(output),
            samples,
            int(self.model.sampling_rate),
            subtype="PCM_16",
            format="WAV",
        )
        self.send({"op": "audio", "device": self.device, "output": str(output)})


def _redact(value: str) -> str:
    return re.sub(r"(?:hf_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9_-]{20,})", "[REDACTED]", value)


def main() -> int:
    configure_environment()
    sys.path.insert(0, str(studio_source_dir()))
    # Keep model-library prints and native fd-1 writes off the JSON channel.
    protocol = os.fdopen(os.dup(1), "wb", buffering=0)
    os.dup2(2, 1)

    def send(message: dict[str, Any]) -> None:
        protocol.write((json.dumps(message, ensure_ascii=True) + "\n").encode())

    worker = StudioWorker(send)
    send({"op": "ready"})
    for line in sys.stdin.buffer:
        try:
            request = json.loads(line)
            if request.get("op") == "shutdown":
                return 0
            if request.get("op") == "clone":
                worker.load(request["device"])
                worker.prompt(request)
                send({"op": "clone", "device": worker.device})
                continue
            if request.get("op") != "synthesize":
                raise ValueError("Неизвестная команда")
            worker.synthesize(request)
        except Exception as error:  # noqa: BLE001 - Keep model failures inside the protocol boundary.
            sys.stderr.write(_redact(traceback.format_exc()))
            sys.stderr.flush()
            send({"op": "error", "message": _redact(str(error))})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
