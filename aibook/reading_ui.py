from __future__ import annotations

import bisect
import logging
from concurrent.futures import CancelledError
from dataclasses import replace

from .buffered_reading import BufferedReading, ReadingOptions
from .job import JobCancelled
from .player import format_time
from .timeline import AudioTimeline, ReadingSession

logger = logging.getLogger(__name__)


class TextReadingController:
    def __init__(self, panel) -> None:
        self.panel = panel
        self.pipeline: BufferedReading | None = None
        self._retired: list[BufferedReading] = []
        self.checkpoint: dict = {}
        self.part = None
        self.index = 0
        self._epoch = 0
        self._generation = 0
        self._resume_position = 0.0
        self._resume_run_start = 0.0
        self._intent = self._waiting = False

    @property
    def active(self) -> bool:
        return self.pipeline is not None

    def restore(self, checkpoint: dict) -> None:
        if not isinstance(checkpoint, dict) or checkpoint.get("version") != 1:
            return
        try:
            source = checkpoint["source"]
            if not isinstance(source, str):
                raise TypeError("Invalid reading source")
            ReadingOptions.restore(checkpoint["options"])
            reading = ReadingSession(
                AudioTimeline(source, "memory"), checkpoint["removed"]
            )
            if reading.remaining_text() != self.panel.app.text.get("1.0", "end-1c"):
                return
            self.checkpoint = dict(checkpoint)
            self.panel._bind_text(reading.timeline, reading.removed)
            self.panel.position.set(checkpoint.get("cursor", 0))
            self.panel.seek_bar.configure(to=max(1, len(source)), state="normal")
            self.panel.name.set("Чтение текста — пауза")
            self.panel.restore_button.configure(state="normal")
        except (KeyError, TypeError, ValueError):
            logger.warning("Invalid buffered-reading checkpoint", exc_info=True)

    def toggle(self) -> None:
        panel = self.panel
        if panel.app._document_pending:
            return
        if self._intent:
            self._intent = False
            if not self._waiting:
                panel._consume(panel.player.snapshot().position)
                panel.player.pause()
            self.save_checkpoint()
            return
        selection = panel.app.text.tag_ranges("sel")
        offset = None
        if selection:
            visible = len(panel.app.text.get("1.0", str(selection[0])))
            offset = (
                panel._reading.original_offset(visible) if panel._reading else visible
            )
        if self.pipeline is None:
            if panel.app._model_jobs_busy():
                raise ValueError("Дождитесь завершения озвучки или клонирования")
            options = panel.app.reading_options()
            source = panel.app.text.get("1.0", "end-1c")
            saved = self.checkpoint
            if saved and panel._reading is not None:
                source = panel._reading.timeline.source
            self._generation += 1
            generation = self._generation
            self.pipeline = BufferedReading(
                source,
                options,
                panel.app.engine,
                lambda kind, token, payload: panel.events.put(
                    (kind, (generation, token), payload)
                ),
                panel.app.prepare_reading_voice,
            )
            removed = saved.get("removed", []) if saved else []
            panel._bind_text(AudioTimeline(source, "memory"), removed)
            resume_matching = False
            if offset is None and saved:
                offset = saved.get("cursor", 0)
                if saved.get("options") == options.serialize() and not saved.get(
                    "restart_at_cursor"
                ):
                    resume_matching = True
                    self.index = min(
                        len(self.pipeline.fragments) - 1, max(0, int(saved["index"]))
                    )
                    offset = saved.get("clip_start", offset)
                    self._resume_position = max(0.0, float(saved.get("position", 0)))
                    self._resume_run_start = max(0.0, float(saved.get("run_start", 0)))
            self._request(offset or 0, preserve_resume=resume_matching)
            panel.app.start_button.configure(state="disabled")
            panel.app.stop_button.configure(state="normal")
            panel.app._update_voice_controls()
        elif offset is not None:
            self._request(offset)
        elif self._waiting:
            pass
        elif panel.player.snapshot().buffered:
            state = panel.player.snapshot()
            if state.position >= state.duration - 0.1:
                self._request(0)
            else:
                panel.player.play()
        else:
            self._request(self.checkpoint.get("cursor", 0))
        self._intent = True
        panel.app.text.tag_remove("sel", "1.0", "end")

    def _request(self, offset: int, *, preserve_resume: bool = False) -> None:
        panel = self.panel
        if self.pipeline is None:
            return
        if panel.player.snapshot().buffered:
            panel._consume(panel.player.snapshot().position)
            panel.player.close()
        if not preserve_resume:
            self._resume_position = self._resume_run_start = 0.0
            self.index = self.pipeline.index_for_char(offset)
        self._waiting = True
        self.part = replace(
            self.pipeline.fragments[self.index],
            start=max(
                self.pipeline.fragments[self.index].start,
                min(offset, self.pipeline.fragments[self.index].end - 1),
            ),
        )
        self._epoch = self.pipeline.request(self.index, self.part.start)
        panel.name.set(
            f"Фрагмент {self.index + 1}/{len(self.pipeline.fragments)} — подготовка"
        )
        panel.stop_button.configure(state="normal")
        panel.seek_bar.configure(to=max(1, len(self.pipeline.source)), state="normal")
        panel.position.set(self.part.start)
        panel.app.status.set("Подготовка отрывка...")

    def handle(self, kind: str, token: int, payload) -> None:
        if self.pipeline is None or token != (self._generation, self._epoch):
            return
        if kind == "read_status":
            self.panel.app.status.set(str(payload))
        elif kind == "read_ready":
            part, future = payload
            if part.index == self.index and self._waiting:
                self._load(future)
            elif future.done() and not future.cancelled():
                error = future.exception()
                if error is not None and not isinstance(error, JobCancelled):
                    self.panel.app.status.set("Следующий отрывок не подготовлен")

    def _load(self, future) -> None:
        panel = self.panel
        try:
            prepared = future.result()
            self.part = prepared.fragment
            speech = prepared.speech
            removed = (
                list(panel._reading.removed)
                if panel._reading
                else self.checkpoint.get("removed", [])
            )
            words = tuple(
                replace(
                    word,
                    char_start=word.char_start + self.part.start,
                    char_end=word.char_end + self.part.start,
                )
                for word in speech.words
            )
            panel._state = panel.player.load_buffer(speech.audio, self._resume_position)
            panel._bind_text(
                AudioTimeline(self.pipeline.source, "memory", words), removed
            )
            panel._delete_cursor = panel._state.position
            panel._run_start = min(panel._state.position, self._resume_run_start)
            self._resume_position = self._resume_run_start = 0.0
            self._waiting = False
            self.pipeline.discard(self.index)
            panel.name.set(f"Фрагмент {self.index + 1}/{len(self.pipeline.fragments)}")
            panel.app.status.set("Отрывок готов; следующий готовится в фоне")
            if self._intent:
                panel.player.play()
            self.save_checkpoint()
        except (JobCancelled, CancelledError):
            self._intent = self._waiting = False
        except Exception as error:  # noqa: BLE001 - Keep model failures off Tk's event loop.
            self.stop()
            panel._error(error)

    def poll(self) -> None:
        panel = self.panel
        pending = []
        for old in self._retired:
            if old.finished:
                old.close()
            else:
                pending.append(old)
        self._retired = pending
        state = panel.player.snapshot()
        was_playing = panel._state.playing
        panel._state = state
        if state.buffered and not self._waiting:
            finished = (
                was_playing
                and not state.playing
                and state.position >= state.duration - 0.1
            )
            if state.playing or was_playing:
                panel._consume(state.position + (0.13 if finished else 0.0))
            if panel._reading is not None and panel._word_starts:
                index = max(
                    0, bisect.bisect_right(panel._word_starts, state.position) - 1
                )
                word = panel._timeline.words[index]
                cursor = (
                    word.char_end
                    if state.position >= word.end + 0.12
                    else word.char_start
                )
                for first, last in panel._reading.removed:
                    if first <= cursor < last:
                        cursor = last
                start, end = (
                    panel._reading.visible_offset(word.char_start),
                    panel._reading.visible_offset(word.char_end),
                )
                if panel._tk_surrogates:
                    start, end = (
                        panel._reading.tk_offset(start),
                        panel._reading.tk_offset(end),
                    )
                panel.app.text.tag_remove("read_current", "1.0", "end")
                if start < end and state.playing:
                    panel.app.text.tag_add(
                        "read_current", f"1.0+{start}c", f"1.0+{end}c"
                    )
                    panel.app.text.see(f"1.0+{start}c")
                panel.position.set(self.part.end if finished else cursor)
            if finished and self._intent and self.pipeline is not None:
                if self.index + 1 < len(self.pipeline.fragments):
                    self.index += 1
                    self._waiting = True
                    self.part = self.pipeline.fragments[self.index]
                    self._epoch = self.pipeline.request(self.index)
                    panel.player.close()
                    ready = self.pipeline.future(self.index)
                    if ready is not None and ready.done():
                        self._load(ready)
                    else:
                        panel.name.set(
                            f"Фрагмент {self.index + 1}/{len(self.pipeline.fragments)} — подготовка"
                        )
                else:
                    self._intent = False
                    self.stop()
                    panel.app.status.set("Чтение завершено")
            panel.clock.set(
                f"{format_time(state.position)} / {format_time(state.duration)}"
            )
            self.save_checkpoint()
        panel._update_play_icon("pause" if self._intent else "play")

    def seek(self, offset: int) -> None:
        playing = self._intent
        if self.pipeline is None:
            if self.checkpoint:
                self.checkpoint["cursor"] = int(offset)
                self.checkpoint["position"] = 0.0
                self.checkpoint["run_start"] = 0.0
                self.checkpoint["restart_at_cursor"] = True
            return
        self._request(int(offset))
        self._intent = playing

    def save_checkpoint(self) -> dict:
        if self.pipeline is None or self.part is None:
            return dict(self.checkpoint)
        state = self.panel.player.snapshot()
        self.checkpoint = {
            "version": 1,
            "source": self.pipeline.source,
            "options": self.pipeline.saved_options(),
            "index": self.index,
            "clip_start": self.part.start,
            "cursor": int(self.panel.position.get()),
            "position": state.position if state.buffered else self._resume_position,
            "run_start": self.panel._run_start
            if state.buffered
            else self._resume_run_start,
            "removed": list(self.panel._reading.removed) if self.panel._reading else [],
        }
        return dict(self.checkpoint)

    def stop(self, *, forget: bool = False) -> None:
        panel = self.panel
        if self.pipeline is None:
            if forget:
                self.checkpoint.clear()
                panel._reading = panel._timeline = None
                panel.delete_check.configure(state="disabled")
                panel.app.text.tag_remove("read_current", "1.0", "end")
            return
        if panel.player.snapshot().buffered:
            if not forget:
                panel._consume(panel.player.snapshot().position)
            panel.player.pause()
            self.save_checkpoint()
            panel.player.close()
        else:
            self.save_checkpoint()
        self._intent = self._waiting = False
        if self.pipeline is not None:
            self.pipeline.close(wait=False)
            self._retired.append(self.pipeline)
            self.pipeline = None
        if forget:
            self.checkpoint.clear()
            panel._reading = panel._timeline = None
            panel.delete_check.configure(state="disabled")
        panel.app.text.tag_remove("read_current", "1.0", "end")
        panel.app.start_button.configure(
            state="disabled" if panel.app._document_pending else "normal"
        )
        panel.app.stop_button.configure(state="disabled")
        panel.app._update_voice_controls()

    def close(self) -> None:
        self.stop()
        for pipeline in self._retired:
            pipeline.close()
        self._retired.clear()
