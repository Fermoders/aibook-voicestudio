from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "AIBook XTTS"
MODEL_NAME = "tts_models/multilingual/multi-dataset/xtts_v2"
CPML_URL = "https://coqui.ai/cpml.txt"


def app_data_dir() -> Path:
    portable = os.environ.get("AIBOOK_DATA_DIR")
    if portable:
        return Path(portable).expanduser().resolve(strict=False)
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    return base / "AIBookXTTS"


def output_dir() -> Path:
    configured = os.environ.get("AIBOOK_OUTPUT_DIR")
    path = (
        Path(configured).expanduser().resolve(strict=False)
        if configured
        else Path.home() / "Documents" / "AIBook"
    )
    path.mkdir(parents=True, exist_ok=True)
    return path


def jobs_dir() -> Path:
    path = app_data_dir() / "jobs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def voices_dir() -> Path:
    path = app_data_dir() / "voices"
    path.mkdir(parents=True, exist_ok=True)
    return path


def model_dir() -> Path:
    from trainer.io import get_user_data_dir

    return (
        Path(get_user_data_dir("tts"))
        / "tts_models--multilingual--multi-dataset--xtts_v2"
    )


def license_marker() -> Path:
    return model_dir() / "tos_agreed.txt"


def is_license_accepted() -> bool:
    return license_marker().is_file()


def accept_noncommercial_license() -> Path:
    marker = license_marker()
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        "I have read, understood and agreed to the non-commercial CPML terms.",
        encoding="utf-8",
    )
    return marker
