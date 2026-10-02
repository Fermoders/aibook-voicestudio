from __future__ import annotations

import re
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, replace
from typing import Any

from razdel import sentenize

from .audio import BufferedSpeech
from .engine import VoiceSpec
from .job import JobCancelled
from .paths import decode_portable_path, encode_portable_path


@dataclass(frozen=True, slots=True)
class ReadingOptions:
    voice: VoiceSpec
    device: str = "auto"
    max_chars: int = 220
    speed: float = 1.0
    playback_speed: float = 1.0
    pitch_semitones: float = 0.0
    volume_db: float = 0.0
    seed: int = 42
    num_steps: int = 32

    def validate(self) -> None:
        self.voice.validate()
        if self.device not in {"auto", "cpu", "cuda"}:
            raise ValueError("Invalid synthesis device")
        if not 80 <= self.max_chars <= 300 or self.num_steps not in {16, 32, 64}:
            raise ValueError("Invalid fragment size or synthesis steps")
        if not 0.7 <= self.speed <= 3 or not 0.5 <= self.playback_speed <= 3:
            raise ValueError("Invalid reading speed")
        if not -6 <= self.pitch_semitones <= 6 or not -20 <= self.volume_db <= 12:
            raise ValueError("Invalid audio effects")

    def serialize(self) -> dict:
        return {
            "voice": {
                "speaker": self.voice.speaker,
                "reference_audio": encode_portable_path(self.voice.reference_audio)
                if self.voice.reference_audio
                else "",
                "reference_text": self.voice.reference_text,
                "saved_prompt": encode_portable_path(self.voice.saved_prompt)
                if self.voice.saved_prompt
                else "",
            },
            "device": self.device,
            "max_chars": self.max_chars,
            "speed": self.speed,
            "playback_speed": self.playback_speed,
            "pitch_semitones": self.pitch_semitones,
            "volume_db": self.volume_db,
            "seed": self.seed,
            "num_steps": self.num_steps,
        }

    @classmethod
    def restore(cls, data: dict) -> ReadingOptions:
        voice = data["voice"]
        result = cls(
            voice=VoiceSpec(
                speaker=voice.get("speaker"),
                reference_audio=decode_portable_path(voice["reference_audio"])
                if voice.get("reference_audio")
                else None,
                reference_text=voice.get("reference_text", ""),
                saved_prompt=decode_portable_path(voice["saved_prompt"])
                if voice.get("saved_prompt")
                else None,
            ),
            **{
                name: data[name]
                for name in (
                    "device",
                    "max_chars",
                    "speed",
                    "playback_speed",
                    "pitch_semitones",
                    "volume_db",
                    "seed",
                    "num_steps",
                )
            },
        )
        result.validate()
        return result


@dataclass(frozen=True, slots=True)
class ReadingFragment:
    index: int
    start: int
    end: int
    pause_ms: int


@dataclass(frozen=True, slots=True)
class PreparedFragment:
    fragment: ReadingFragment
    speech: BufferedSpeech


def reading_fragments(source: str, max_chars: int) -> tuple[ReadingFragment, ...]:
    if not 80 <= max_chars <= 300:
        raise ValueError("Invalid reading fragment size")
    fragments: list[ReadingFragment] = []
    paragraphs: list[tuple[int, int]] = []
    start = 0
    for separator in re.finditer(r"\n\s*\n+", source):
        paragraphs.append((start, separator.start()))
        start = separator.end()
    paragraphs.append((start, len(source)))
    for begin, end in paragraphs:
        units: list[tuple[int, int]] = []
        for sentence in sentenize(source[begin:end]):
            words = list(re.finditer(r"\S+", sentence.text))
            current_start = current_end = None
            length = 0
            for word in words:
                word_start = begin + sentence.start + word.start()
                word_end = begin + sentence.start + word.end()
                if (
                    current_start is not None
                    and length + 1 + len(word.group()) > max_chars
                ):
                    units.append((current_start, current_end))
                    current_start = current_end = None
                    length = 0
                while word_end - word_start > max_chars:
                    units.append((word_start, word_start + max_chars))
                    word_start += max_chars
                if current_start is None:
                    current_start = word_start
                else:
                    length += 1
                current_end = word_end
                length += word_end - word_start
            if current_start is not None:
                units.append((current_start, current_end))
        current_start = current_end = None
        length = 0
        packed = []
        for first, last in units:
            unit_length = len(" ".join(source[first:last].split()))
            if current_start is not None and length + 1 + unit_length > max_chars:
                packed.append((current_start, current_end))
                current_start = current_end = None
                length = 0
            if current_start is None:
                current_start = first
            else:
                length += 1
            current_end = last
            length += unit_length
        if current_start is not None:
            packed.append((current_start, current_end))
        for offset, (first, last) in enumerate(packed):
            fragments.append(
                ReadingFragment(
                    len(fragments),
                    first,
                    last,
                    620 if offset == len(packed) - 1 else 220,
                )
            )
    return tuple(fragments)


class BufferedReading:
    """Only the current and next fragment may be queued or retained."""

    def __init__(
        self,
        source: str,
        options: ReadingOptions,
        engine: Any,
        notify: Callable[[str, int, Any], None],
        prepare_voice: Callable[[VoiceSpec, str, threading.Event], VoiceSpec]
        | None = None,
    ) -> None:
        options.validate()
        self.source = source
        self.options = options
        self.fragments = reading_fragments(source, options.max_chars)
        if not self.fragments:
            raise ValueError("Добавьте текст книги")
        self.engine, self.notify, self.prepare_voice = engine, notify, prepare_voice
        self._lock = threading.RLock()
        self._pool = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="aibook-reading"
        )
        self._futures: dict[int, tuple[ReadingFragment, Future]] = {}
        self._pending: set[Future] = set()
        self._cancel = threading.Event()
        self._epoch = 0
        self._closed = False

    @property
    def epoch(self) -> int:
        with self._lock:
            return self._epoch

    @property
    def finished(self) -> bool:
        with self._lock:
            return self._closed and not self._pending

    def index_for_char(self, offset: int) -> int:
        return next(
            (part.index for part in self.fragments if offset < part.end),
            len(self.fragments) - 1,
        )

    def request(self, index: int, start: int | None = None) -> int:
        with self._lock:
            if self._closed:
                raise RuntimeError("Reading session is closed")
            part = self.fragments[index]
            if start is not None:
                part = replace(part, start=min(part.end - 1, max(part.start, start)))
            existing = self._futures.get(index)
            if existing is None or existing[0] != part:
                self._cancel.set()
                for _, future in self._futures.values():
                    future.cancel()
                self._futures.clear()
                self._cancel = threading.Event()
                self._epoch += 1
            for old in list(self._futures):
                if old not in {index, index + 1}:
                    self._futures.pop(old)[1].cancel()
            for selected in (part, *self.fragments[index + 1 : index + 2]):
                if selected.index in self._futures:
                    continue
                epoch, cancel = self._epoch, self._cancel
                future = self._pool.submit(self._render, selected, epoch, cancel)
                self._pending.add(future)
                self._futures[selected.index] = selected, future
                future.add_done_callback(
                    lambda item, token=epoch, chunk=selected: self._complete(
                        item, token, chunk
                    )
                )
            return self._epoch

    def _complete(self, future: Future, epoch: int, part: ReadingFragment) -> None:
        with self._lock:
            self._pending.discard(future)
        self.notify("read_ready", epoch, (part, future))

    def future(self, index: int) -> Future | None:
        with self._lock:
            item = self._futures.get(index)
            return item[1] if item else None

    def discard(self, index: int) -> None:
        with self._lock:
            self._futures.pop(index, None)

    def queued_indices(self) -> tuple[int, ...]:
        with self._lock:
            return tuple(self._futures)

    def saved_options(self) -> dict:
        with self._lock:
            return self.options.serialize()

    def _render(self, part: ReadingFragment, epoch: int, cancel: threading.Event):
        if cancel.is_set():
            raise JobCancelled("Reading stopped")
        with self._lock:
            options = self.options
        if options.voice.reference_audio is not None and self.prepare_voice is not None:
            voice = self.prepare_voice(options.voice, options.device, cancel)
            with self._lock:
                if cancel.is_set():
                    raise JobCancelled("Reading stopped")
                self.options = options = replace(options, voice=voice)
        speech = self.engine.synthesize_buffer(
            text=self.source[part.start : part.end],
            voice=options.voice,
            device_preference=options.device,
            speed=options.speed,
            seed=options.seed + part.index,
            num_steps=options.num_steps,
            status=lambda message: self.notify("read_status", epoch, message),
            cancel_event=cancel,
            pitch_semitones=options.pitch_semitones,
            volume_db=options.volume_db,
            playback_speed=options.playback_speed,
            pause_ms=part.pause_ms,
        )
        if cancel.is_set():
            raise JobCancelled("Reading stopped")
        return PreparedFragment(part, speech)

    def close(self, *, wait: bool = True) -> None:
        with self._lock:
            if not self._closed:
                self._closed = True
                self._cancel.set()
                for _, future in self._futures.values():
                    future.cancel()
                self._futures.clear()
        self._pool.shutdown(wait=wait, cancel_futures=True)
