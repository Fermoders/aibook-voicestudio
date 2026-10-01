from __future__ import annotations

import os
from pathlib import Path

PROJECT_URL = "https://github.com/debpalash/VoiceStudio"
SOURCE_COMMIT = "3915a62cb482bb43117aaebab57d22745bd1cc21"
SOURCE_VERSION = "0.5.6"
MODEL_REPO = "k2-fsa/OmniVoice"
MODEL_REVISION = "c5fdb5ccb189668d56333f77ba2629f4cd7535f4"
MODEL_FILES = {
    "model.safetensors": 2450344112,
    "audio_tokenizer/model.safetensors": 805665628,
    "config.json": 2238,
    "tokenizer.json": 11423986,
    "tokenizer_config.json": 533,
    "audio_tokenizer/config.json": 2531,
    "audio_tokenizer/preprocessor_config.json": 206,
}
PRESETS = {
    "Рассказчик": "male, middle-aged, moderate pitch",
    "Рассказчица": "female, middle-aged, moderate pitch",
    "Низкий мужской": "male, middle-aged, low pitch",
    "Молодой женский": "female, young adult, moderate pitch",
}
PRESET_TEXT = (
    "Добрый день. Сегодня мы отправимся в мир книг и удивительных историй. "
    "Каждая страница открывает что-то новое."
)


def workspace_dir() -> Path:
    return Path(__file__).resolve().parent.parent


def studio_source_dir() -> Path:
    return workspace_dir() / "vendor" / "VoiceStudio"


def studio_model_dir() -> Path:
    configured = os.environ.get("AIBOOK_VOICESTUDIO_MODEL_DIR")
    return (
        Path(configured).resolve()
        if configured
        else application_root() / "data" / "models" / "OmniVoice"
    )


def application_root() -> Path:
    root = workspace_dir()
    if (
        root.name == "app"
        and (root.parent / "runtime" / "python" / "python.exe").is_file()
    ):
        return root.parent
    return root


def model_ready() -> bool:
    directory = studio_model_dir()
    for name, size in MODEL_FILES.items():
        try:
            if (directory / name).stat().st_size != size:
                return False
        except OSError:
            return False
    try:
        return (directory / "AIBOOK_REVISION.txt").read_text(
            encoding="ascii"
        ).strip() == MODEL_REVISION
    except OSError:
        return False


def configure_environment() -> None:
    root = application_root()
    os.environ.setdefault("AIBOOK_ROOT", str(root))
    os.environ.setdefault("AIBOOK_DATA_DIR", str(root / "data" / "voicestudio"))
    os.environ.setdefault("AIBOOK_OUTPUT_DIR", str(root / "outputs"))
    os.environ.setdefault("HF_HOME", str(root / ".cache" / "huggingface"))
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    os.environ["HF_HUB_DISABLE_IMPLICIT_TOKEN"] = "1"
    ffmpeg = root / "runtime" / "ffmpeg"
    if ffmpeg.is_dir() and str(ffmpeg) not in os.environ.get("PATH", "").split(
        os.pathsep
    ):
        os.environ["PATH"] = str(ffmpeg) + os.pathsep + os.environ.get("PATH", "")
