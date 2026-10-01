from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .config import app_data_dir, output_dir
from .paths import decode_portable_path, encode_portable_path


@dataclass(frozen=True, slots=True)
class HistoryEntry:
    path: Path
    created_at: datetime

    @property
    def display_time(self) -> str:
        return self.created_at.astimezone().strftime("%d.%m %H:%M")


class ResultHistory:
    def __init__(self, limit: int = 10) -> None:
        self.limit = limit
        self.path = app_data_dir() / "history.json"
        self._lock = threading.RLock()

    def load(self) -> list[HistoryEntry]:
        with self._lock:
            entries = self._read_locked()
            if not entries:
                entries = self._discover_existing()
            entries = [entry for entry in entries if entry.path.is_file()]
            entries.sort(key=lambda entry: entry.created_at, reverse=True)
            entries = entries[: self.limit]
            self._write_locked(entries)
            return entries

    def add(self, result_path: str | Path) -> list[HistoryEntry]:
        result = Path(result_path).resolve()
        with self._lock:
            entries = [entry for entry in self._read_locked() if entry.path != result]
            entries.insert(0, HistoryEntry(result, datetime.now().astimezone()))
            entries = [entry for entry in entries if entry.path.is_file()][: self.limit]
            self._write_locked(entries)
            return entries

    def _read_locked(self) -> list[HistoryEntry]:
        if not self.path.is_file():
            return []
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return []

        entries: list[HistoryEntry] = []
        for item in payload if isinstance(payload, list) else []:
            try:
                entries.append(
                    HistoryEntry(
                        path=decode_portable_path(item["path"]),
                        created_at=datetime.fromisoformat(item["created_at"]),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        return entries

    def _write_locked(self, entries: list[HistoryEntry]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = [
            {
                "path": encode_portable_path(entry.path),
                "created_at": entry.created_at.isoformat(),
            }
            for entry in entries[: self.limit]
        ]
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(self.path)

    def _discover_existing(self) -> list[HistoryEntry]:
        candidates = [
            path
            for suffix in ("*.wav", "*.mp3")
            for path in output_dir().glob(suffix)
            if path.is_file()
        ]
        candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)
        return [
            HistoryEntry(
                path=path.resolve(),
                created_at=datetime.fromtimestamp(path.stat().st_mtime).astimezone(),
            )
            for path in candidates[: self.limit]
        ]
