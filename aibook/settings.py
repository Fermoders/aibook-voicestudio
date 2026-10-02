from __future__ import annotations

import json
import threading
from typing import Any
from uuid import uuid4

from filelock import FileLock

from .config import app_data_dir, output_dir
from .paths import decode_portable_path, encode_portable_path

PATH_KEYS = {"source_path", "output_folder", "reference_path", "player_path"}


def default_settings() -> dict[str, Any]:
    return {
        "source_path": "",
        "output_folder": str(output_dir()),
        "output_format": "wav",
        "voice_mode": "builtin",
        "speaker": "Ana Florence",
        "reference_path": "",
        "reference_text": "",
        "num_steps": 32,
        "device": "auto",
        "model_speed": 1.0,
        "playback_speed": 1.0,
        "temperature": 0.65,
        "max_chars": 220,
        "seed": 42,
        "pitch_semitones": 0.0,
        "volume_db": 0.0,
        "saved_voice_id": "",
        "player_path": "",
        "player_mode": "text",
        "reader_state": {},
        "player_position": 0.0,
        "player_run_start": 0.0,
        "player_stamp": "",
        "player_removed": [],
        "delete_spoken": False,
        "player_volume": 0.8,
    }


class SettingsStore:
    def __init__(self) -> None:
        directory = app_data_dir()
        self.settings_path = directory / "settings.json"
        self.draft_path = directory / "draft.txt"
        self._lock = threading.RLock()

    def load(self) -> dict[str, Any]:
        with self._lock:
            settings = default_settings()
            if not self.settings_path.is_file():
                return settings
            try:
                payload = json.loads(self.settings_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                return settings
            if isinstance(payload, dict):
                restored = {
                    key: value for key, value in payload.items() if key in settings
                }
                for key in PATH_KEYS:
                    if isinstance(restored.get(key), str) and restored[key]:
                        restored[key] = str(decode_portable_path(restored[key]))
                settings.update(restored)
                if "player_mode" not in payload and settings["player_path"]:
                    settings["player_mode"] = "file"
            return settings

    def load_draft(self) -> str:
        with self._lock:
            try:
                payload = json.loads(self.settings_path.read_text(encoding="utf-8"))
                if isinstance(payload, dict) and isinstance(payload.get("_draft"), str):
                    return payload["_draft"]
            except (OSError, ValueError):
                pass
            try:
                return self.draft_path.read_text(encoding="utf-8")
            except OSError:
                return ""

    def save(self, settings: dict[str, Any], draft: str) -> None:
        with self._lock:
            self.settings_path.parent.mkdir(parents=True, exist_ok=True)
            serialized = dict(settings)
            for key in PATH_KEYS:
                if isinstance(serialized.get(key), str):
                    serialized[key] = encode_portable_path(serialized[key])
            # Keep the draft and destructive read-along state in one atomic snapshot.
            serialized["_draft"] = draft
            key = uuid4().hex
            settings_temporary = self.settings_path.with_name(f"settings.{key}.tmp")
            draft_temporary = self.draft_path.with_name(f"draft.{key}.tmp")
            with FileLock(str(self.settings_path.with_suffix(".lock")), timeout=30):
                try:
                    settings_temporary.write_text(
                        json.dumps(serialized, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                    draft_temporary.write_text(draft, encoding="utf-8")
                    draft_temporary.replace(self.draft_path)
                    settings_temporary.replace(self.settings_path)
                finally:
                    settings_temporary.unlink(missing_ok=True)
                    draft_temporary.unlink(missing_ok=True)
