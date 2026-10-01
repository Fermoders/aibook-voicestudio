from __future__ import annotations

import hashlib
import json
import shutil
import threading
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from filelock import FileLock

from .config import voices_dir
from .voice_studio_config import MODEL_REVISION, PRESETS


def cached_prompt_path(voice_id: str, directory: Path | None = None) -> Path:
    key = hashlib.sha256(
        (voice_id + "|" + MODEL_REVISION + "|preset-v1").encode()
    ).hexdigest()[:32]
    return (directory or voices_dir()) / f"omnivoice_{key}.pt"


@dataclass(frozen=True, slots=True)
class SavedVoice:
    id: str
    name: str
    filename: str
    sha256: str
    created: str

    @property
    def label(self) -> str:
        return f"{self.name} (клон)"


class VoiceLibrary:
    def __init__(self, directory: Path | None = None) -> None:
        self.directory = directory or voices_dir()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.index = self.directory / "library.json"
        self._lock = threading.RLock()

    def _file_lock(self) -> FileLock:
        return FileLock(str(self.index.with_suffix(".lock")), timeout=30)

    def _read(self) -> list[SavedVoice]:
        if not self.index.exists():
            return []
        try:
            payload = json.loads(self.index.read_text(encoding="utf-8"))
            if payload["version"] != 1:
                raise ValueError("Unknown voice library format")
            entries = [SavedVoice(**item) for item in payload["voices"]]
            for entry in entries:
                if (
                    entry.filename != f"saved_{entry.id}.pt"
                    or len(entry.id) != 32
                    or any(c not in "0123456789abcdef" for c in entry.id)
                ):
                    raise ValueError("Invalid voice filename")
            return entries
        except (OSError, ValueError, TypeError, KeyError) as error:
            raise RuntimeError(
                "Повреждён список голосов. Существующие профили сохранены."
            ) from error

    def _ignored_cache(self) -> set[str]:
        if not self.index.exists():
            return set()
        return set(
            json.loads(self.index.read_text(encoding="utf-8")).get("ignored_cache", [])
        )

    def _write(self, entries: list[SavedVoice], *, ignore: str = "") -> None:
        ignored = self._ignored_cache()
        if ignore:
            ignored.add(ignore)
        temporary = self.index.with_name(f"library.{uuid4().hex}.tmp")
        try:
            temporary.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "voices": [asdict(entry) for entry in entries],
                        "ignored_cache": sorted(ignored),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            temporary.replace(self.index)
        finally:
            temporary.unlink(missing_ok=True)

    def list(self) -> list[SavedVoice]:
        with self._lock, self._file_lock():
            return self._read()

    def path(self, voice_id: str) -> Path:
        with self._lock, self._file_lock():
            entry = next((item for item in self._read() if item.id == voice_id), None)
            if entry is None:
                raise ValueError("Сохранённый голос удалён")
            path = self.directory / entry.filename
            if not path.is_file():
                raise FileNotFoundError("Не найден сохранённый профиль голоса")
            return path

    @staticmethod
    def _name(name: str, entries: list[SavedVoice], *, exclude: str = "") -> str:
        name = name.strip()
        if not name or len(name) > 80 or any(ord(c) < 32 for c in name):
            raise ValueError("Название голоса должно содержать от 1 до 80 символов")
        if any(
            item.id != exclude and item.name.casefold() == name.casefold()
            for item in entries
        ):
            raise ValueError("Голос с таким названием уже существует")
        return name

    @staticmethod
    def _unique_name(name: str, entries: list[SavedVoice]) -> str:
        base = name.strip()[:70] or "Мой голос"
        candidate, index = base, 2
        names = {item.name.casefold() for item in entries}
        while candidate.casefold() in names:
            candidate = f"{base} {index}"
            index += 1
        return candidate

    def add(self, source: Path, name: str, *, unique_name: bool = False) -> SavedVoice:
        with self._lock, self._file_lock():
            entries = self._read()
            name = (
                self._unique_name(name, entries)
                if unique_name
                else self._name(name, entries)
            )
            entry = self._add_locked(source, name)
            try:
                self._write([*entries, entry])
            except BaseException:
                (self.directory / entry.filename).unlink(missing_ok=True)
                raise
            return entry

    def _add_locked(self, source: Path, name: str) -> SavedVoice:
        voice_id = uuid4().hex
        target = self.directory / f"saved_{voice_id}.pt"
        temporary = target.with_suffix(".tmp")
        try:
            with FileLock(str(source.with_suffix(".lock")), timeout=600):
                if source.stat().st_size == 0:
                    raise ValueError("Пустой голосовой профиль")
                shutil.copyfile(source, temporary)
            with temporary.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
        return SavedVoice(
            voice_id, name, target.name, digest, datetime.now(timezone.utc).isoformat()
        )

    def rename(self, voice_id: str, name: str) -> SavedVoice:
        with self._lock, self._file_lock():
            entries = self._read()
            name = self._name(name, entries, exclude=voice_id)
            entry = next((item for item in entries if item.id == voice_id), None)
            if entry is None:
                raise ValueError("Голос уже удалён")
            renamed = replace(entry, name=name)
            self._write([renamed if item.id == voice_id else item for item in entries])
            return renamed

    def delete(self, voice_id: str) -> None:
        with self._lock, self._file_lock():
            entries = self._read()
            entry = next((item for item in entries if item.id == voice_id), None)
            if entry is None:
                return
            path = self.directory / entry.filename
            with FileLock(str(path.with_suffix(".lock")), timeout=30):
                self._write(
                    [item for item in entries if item.id != voice_id],
                    ignore=entry.sha256,
                )
                path.unlink(missing_ok=True)

    def import_cached(self) -> list[SavedVoice]:
        # Older releases stored unnamed clones next to generated presets.
        presets = {cached_prompt_path(name, self.directory).name for name in PRESETS}
        with self._lock, self._file_lock():
            entries = self._read()
            known = {item.sha256 for item in entries} | self._ignored_cache()
            imported: list[SavedVoice] = []
            for path in sorted(self.directory.glob("omnivoice_*.pt")):
                if path.name in presets:
                    continue
                with (
                    FileLock(str(path.with_suffix(".lock")), timeout=600),
                    path.open("rb") as stream,
                ):
                    digest = hashlib.file_digest(stream, "sha256").hexdigest()
                if digest in known:
                    continue
                entry = self._add_locked(path, self._unique_name("Мой голос", entries))
                entries.append(entry)
                imported.append(entry)
                known.add(entry.sha256)
            if imported:
                self._write(entries)
            return entries
