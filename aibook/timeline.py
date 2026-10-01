from __future__ import annotations

import bisect
import json
import math
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from uuid import uuid4


def sidecar_path(audio: Path) -> Path:
    return audio.with_name(audio.name + ".aibook.json")


def audio_stamp(audio: Path) -> str:
    stat = audio.stat()
    return f"{stat.st_size}:{stat.st_mtime_ns}"


@dataclass(frozen=True, slots=True)
class WordTime:
    char_start: int
    char_end: int
    start: float
    end: float


@dataclass(frozen=True, slots=True)
class AudioTimeline:
    source: str
    stamp: str
    words: tuple[WordTime, ...] = ()

    @classmethod
    def load(cls, audio: Path) -> AudioTimeline | None:
        path = sidecar_path(audio)
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload["version"] != 1 or payload["stamp"] != audio_stamp(audio):
                return None
            source = payload["source"]
            if not isinstance(source, str):
                raise TypeError("Invalid source")
            words = tuple(WordTime(**word) for word in payload.get("words", []))
            previous_char = 0
            previous_start = previous_end = 0.0
            for word in words:
                if not (
                    previous_char <= word.char_start < word.char_end <= len(source)
                    and math.isfinite(word.start)
                    and math.isfinite(word.end)
                    and previous_start <= word.start <= word.end
                    and previous_end <= word.end
                ):
                    raise ValueError("Invalid word timestamp")
                previous_char, previous_start, previous_end = (
                    word.char_end,
                    word.start,
                    word.end,
                )
            return cls(source, payload["stamp"], words)
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise ValueError("Повреждена синхронизация аудио и текста") from error

    def save(self, audio: Path) -> None:
        if self.stamp != audio_stamp(audio):
            raise ValueError("Аудиофайл изменён во время синхронизации")
        path = sidecar_path(audio)
        temporary = path.with_name(path.name + f".{uuid4().hex}.tmp")
        try:
            temporary.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "source": self.source,
                        "stamp": self.stamp,
                        "words": [asdict(word) for word in self.words],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    def time_for_char(self, offset: int) -> float:
        if not self.words:
            raise ValueError("Синхронизация текста ещё не готова")
        for word in self.words:
            if offset < word.char_end:
                return max(0.0, word.start - 0.03)
        return self.words[-1].end


def map_alignment(
    source: str, aligned_words: list[dict], duration: float
) -> tuple[WordTime, ...]:
    compact: list[str] = []
    offsets: list[int] = []
    for match in re.finditer(r"\S+", source):
        if compact:
            compact.append(" ")
            offsets.append(match.start() - 1)
        compact.extend(match.group())
        offsets.extend(range(match.start(), match.end()))
    normalized = "".join(compact)
    cursor = 0
    words: list[WordTime] = []
    for aligned in aligned_words:
        value = " ".join(aligned["word"].split())
        if not value:
            continue
        found = normalized.find(value, cursor)
        if found < 0 or normalized[cursor:found].strip():
            raise ValueError("Слова синхронизации не совпадают с исходным текстом")
        end_offset = found + len(value)
        start, end = float(aligned["start"]), float(aligned["end"])
        if not (
            math.isfinite(start)
            and math.isfinite(end)
            and 0 <= start <= end <= duration + 0.5
        ):
            raise ValueError("Некорректное время произнесения слова")
        start = max(words[-1].start if words else 0.0, min(start, duration))
        end = max(words[-1].end if words else 0.0, start, min(end, duration))
        word = WordTime(offsets[found], offsets[end_offset - 1] + 1, start, end)
        if words and words[-1].char_end == word.char_start:
            previous = words.pop()
            word = WordTime(
                previous.char_start, word.char_end, previous.start, word.end
            )
        words.append(word)
        cursor = end_offset
    if not words or normalized[cursor:].strip():
        raise ValueError("Синхронизация не покрывает весь текст")
    return tuple(words)


class ReadingSession:
    """Original character offsets remain stable as spoken ranges are removed."""

    def __init__(self, timeline: AudioTimeline, removed: list | None = None) -> None:
        self.timeline = timeline
        self.removed: list[tuple[int, int]] = []
        for start, end in removed or []:
            if (
                not isinstance(start, int)
                or not isinstance(end, int)
                or not 0 <= start < end <= len(timeline.source)
            ):
                raise ValueError("Повреждено состояние чтения")
            self._add_range(start, end)
        self._ends = [word.end for word in timeline.words]
        self._astral = [
            match.start()
            for match in re.finditer(r"[\U00010000-\U0010ffff]", timeline.source)
        ]

    def tk_offset(self, visible: int) -> int:
        original = self.original_offset(visible)
        extra = bisect.bisect_left(self._astral, original)
        for start, end in self.removed:
            if start >= original:
                break
            extra -= bisect.bisect_left(
                self._astral, min(original, end)
            ) - bisect.bisect_left(self._astral, start)
        return visible + extra

    def remaining_text(self) -> str:
        cursor = 0
        parts: list[str] = []
        for start, end in self.removed:
            parts.append(self.timeline.source[cursor:start])
            cursor = end
        parts.append(self.timeline.source[cursor:])
        return "".join(parts)

    def original_offset(self, visible: int) -> int:
        original = max(0, visible)
        for start, end in self.removed:
            if start > original:
                break
            original += end - start
        return min(len(self.timeline.source), original)

    def visible_offset(self, original: int) -> int:
        removed = sum(
            max(0, min(original, end) - start)
            for start, end in self.removed
            if start < original
        )
        return original - removed

    def _add_range(self, start: int, end: int) -> None:
        ranges: list[tuple[int, int]] = []
        for old_start, old_end in self.removed:
            if old_end < start:
                ranges.append((old_start, old_end))
            elif end < old_start:
                ranges.append((start, end))
                start, end = old_start, old_end
            else:
                start, end = min(start, old_start), max(end, old_end)
        ranges.append((start, end))
        self.removed = ranges

    def consume(
        self,
        previous: float,
        position: float,
        run_start: float,
        *,
        tk_units: bool = False,
    ) -> list[tuple[int, int]]:
        # A small safety margin prevents deletion at a rounded word-end timestamp.
        first = bisect.bisect_right(self._ends, previous - 0.12)
        last = bisect.bisect_right(self._ends, position - 0.12)
        deletions: list[tuple[int, int]] = []
        words = self.timeline.words
        for index in range(first, last):
            word = words[index]
            if word.start < run_start - 0.04 or word.end <= word.start:
                continue
            end = word.char_end
            while (
                end < len(self.timeline.source) and self.timeline.source[end].isspace()
            ):
                end += 1
            start_visible, end_visible = (
                self.visible_offset(word.char_start),
                self.visible_offset(end),
            )
            if start_visible < end_visible:
                deletions.append(
                    (self.tk_offset(start_visible), self.tk_offset(end_visible))
                    if tk_units
                    else (start_visible, end_visible)
                )
                self._add_range(word.char_start, end)
        return deletions
