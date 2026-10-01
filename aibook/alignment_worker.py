from __future__ import annotations

import sys
from pathlib import Path

from .timeline import AudioTimeline, map_alignment
from .voice_studio_config import configure_environment, studio_model_dir


def main() -> None:
    configure_environment()
    import soundfile as sf
    import stable_whisper
    import torch

    audio = Path(sys.argv[1])
    timeline = AudioTimeline.load(audio)
    if timeline is None:
        raise ValueError("Не найден исходный текст аудиокниги")
    checkpoint = studio_model_dir().parent / "Whisper" / "tiny.pt"
    if not checkpoint.is_file():
        raise FileNotFoundError(
            "Не установлена локальная модель синхронизации Whisper tiny"
        )
    torch.set_num_threads(min(4, torch.get_num_threads()))
    model = stable_whisper.load_model(str(checkpoint), device="cpu")
    result = model.align(
        str(audio),
        " ".join(timeline.source.split()),
        language="ru",
        verbose=None,
        regroup=False,
        stream=True,
        vad=False,
        failure_threshold=0.3,
    )
    if result is None:
        raise RuntimeError("Не удалось определить время произнесения слов")
    aligned = [
        word for segment in result.to_dict()["segments"] for word in segment["words"]
    ]
    words = map_alignment(timeline.source, aligned, sf.info(str(audio)).duration)
    AudioTimeline(timeline.source, timeline.stamp, words).save(audio)


if __name__ == "__main__":
    main()
