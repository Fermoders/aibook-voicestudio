from __future__ import annotations

import bisect
import logging
import queue
import time
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .alignment import align_audio
from .editor import edit_group
from .job import JobCancelled
from .player import AudioPlayer, PlaybackState, format_time
from .timeline import AudioTimeline, ReadingSession, audio_stamp
from .ui_helpers import icon_button

logger = logging.getLogger(__name__)


class PlaybackPanel(ttk.LabelFrame):
    def __init__(self, parent, app, saved: dict) -> None:
        super().__init__(
            parent, text="Проигрывание", style="Section.TLabelframe", padding=8
        )
        self.app = app
        self.player = AudioPlayer()
        self.pool = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="aibook-player"
        )
        self.events: queue.Queue = queue.Queue()
        self._request = 0
        self._loading = self._closed = self._editing = self._seeking = False
        self._resume_seek = False
        self._alignment_cancel = None
        self._alignment_pending = False
        self._state = PlaybackState()
        self._stamp = ""
        self._tk_surrogates = int(self.tk.call("string", "length", "\U0001f600")) == 2
        self._timeline: AudioTimeline | None = None
        self._reading: ReadingSession | None = None
        self._word_starts: list[float] = []
        self._delete_cursor = self._run_start = 0.0
        self._pending_play = False
        self._pending_offset: int | None = None
        self._last_save = time.monotonic()
        self.name = tk.StringVar(value="Аудио не выбрано")
        self.clock = tk.StringVar(value="0:00 / 0:00")
        self.position = tk.DoubleVar(value=0.0)
        self.volume = tk.DoubleVar(value=saved.get("player_volume", 0.8))
        self.delete_spoken = tk.BooleanVar(value=saved.get("delete_spoken", False))
        self.columnconfigure(0, weight=1)
        filename = ttk.Label(self, textvariable=self.name, wraplength=700)
        filename.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        filename.bind(
            "<Configure>",
            lambda event: filename.configure(wraplength=max(100, event.width)),
        )
        self.restore_button = icon_button(
            self,
            "rotate-ccw",
            "Восстановить текст записи",
            self.restore_text,
            state="disabled",
        )
        self.restore_button.grid(row=0, column=1, padx=3)
        icon_button(self, "folder-open", "Открыть аудио", self.choose_audio).grid(
            row=0, column=2, padx=3
        )
        controls = ttk.Frame(self)
        controls.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(6, 4))
        controls.columnconfigure(2, weight=1)
        self.play_button = icon_button(
            controls, "play", "Проиграть / пауза", self.toggle
        )
        self.play_button.grid(row=0, column=0, padx=(0, 3))
        self.stop_button = icon_button(
            controls, "square", "Остановить проигрывание", self.stop, state="disabled"
        )
        self.stop_button.grid(row=0, column=1, padx=(0, 8))
        self.seek_bar = ttk.Scale(
            controls, variable=self.position, from_=0, to=1, state="disabled"
        )
        self.seek_bar.grid(row=0, column=2, sticky="ew")
        self.seek_bar.bind("<ButtonPress-1>", self._begin_seek)
        self.seek_bar.bind("<ButtonRelease-1>", self._end_seek)
        self.seek_bar.bind(
            "<KeyRelease>", lambda _event: self.seek(self.position.get())
        )
        ttk.Label(controls, textvariable=self.clock, width=23, anchor="center").grid(
            row=0, column=3
        )
        ttk.Label(controls, text="Громкость").grid(row=0, column=4, padx=(4, 5))
        ttk.Scale(
            controls,
            variable=self.volume,
            from_=0,
            to=1,
            length=85,
            command=self._volume_changed,
        ).grid(row=0, column=5)
        self.delete_check = ttk.Checkbutton(
            self,
            text="Удалять произнесённый текст",
            variable=self.delete_spoken,
            command=self._deletion_changed,
            state="disabled",
        )
        self.delete_check.grid(row=2, column=0, columnspan=3, sticky="w")
        app.text.tag_configure(
            "read_current", background="#d5ecde", foreground="#172b20"
        )
        self.player.volume(self.volume.get())
        self._timer = self.after(100, self._poll)

    def restore(self, saved: dict) -> None:
        path = saved.get("player_path", "")
        if path and Path(path).is_file():
            self.load(Path(path), restored=saved)

    def choose_audio(self) -> None:
        path = filedialog.askopenfilename(
            title="Открыть аудиокнигу",
            filetypes=[("Аудио", "*.wav *.mp3")],
            parent=self,
        )
        if path:
            self.load(Path(path))

    def load(
        self, path: Path, *, restored: dict | None = None, autoplay: bool = False
    ) -> None:
        import threading

        if self._alignment_cancel is not None:
            self._alignment_cancel.set()
        if not self._loading:
            self._consume(self.player.snapshot().position)
            self.player.pause()
        self._request += 1
        token = self._request
        self._loading = True
        self._pending_play = False
        self._reading = self._timeline = None
        self._alignment_pending = False
        self._alignment_cancel = threading.Event()
        self.app.text.tag_remove("read_current", "1.0", "end")
        self.name.set(path.name)
        self.play_button.configure(state="disabled")
        self.delete_check.configure(state="disabled")
        self.seek_bar.configure(state="disabled")
        future = self.pool.submit(self._load_audio, path, restored or {})
        future.add_done_callback(
            lambda item: self.events.put(
                ("load", token, (item, restored or {}, autoplay))
            )
        )

    def _load_audio(self, path: Path, restored: dict):
        try:
            timeline = AudioTimeline.load(path)
        except ValueError:
            logger.exception("Invalid audio sidecar; plain playback remains available")
            timeline = None
        stamp = audio_stamp(path)
        same_audio = stamp == restored.get("player_stamp")
        position = float(restored.get("player_position", 0.0)) if same_audio else 0.0
        return self.player.load(path, position), timeline, same_audio, stamp

    def _bind_text(
        self, timeline: AudioTimeline | None, removed: list | None = None
    ) -> None:
        self._timeline = timeline
        self._reading = None
        self._word_starts = [word.start for word in timeline.words] if timeline else []
        if timeline is not None:
            try:
                reading = ReadingSession(timeline, removed)
                if reading.remaining_text() == self.app.text.get("1.0", "end-1c"):
                    self._reading = reading
            except (ValueError, TypeError):
                logger.warning("Invalid saved read-along state", exc_info=True)
        self.delete_check.configure(
            state="normal"
            if self._reading is not None and timeline.words
            else "disabled"
        )
        self.restore_button.configure(state="normal" if timeline else "disabled")
        if self._reading is None:
            self.delete_spoken.set(False)

    def _finish_load(self, future, restored: dict, autoplay: bool) -> None:
        self._loading = False
        self.play_button.configure(state="normal")
        try:
            state, timeline, same_audio, self._stamp = future.result()
            self._state = state
            removed = restored.get("player_removed", []) if same_audio else []
            self._bind_text(timeline, removed)
            self._run_start = (
                min(
                    state.position,
                    max(0.0, float(restored.get("player_run_start", state.position))),
                )
                if same_audio
                else state.position
            )
            self._delete_cursor = state.position
            self.player.volume(self.volume.get())
            self.stop_button.configure(state="normal")
            self.seek_bar.configure(to=max(1, state.duration), state="normal")
            if timeline is not None and not timeline.words:
                self._alignment_pending = True
                self.name.set(f"{state.path.name} - синхронизация слов...")
                token, cancel = self._request, self._alignment_cancel
                aligned = self.pool.submit(align_audio, state.path, cancel)
                aligned.add_done_callback(
                    lambda item: self.events.put(("align", token, item))
                )
            if autoplay:
                self.toggle()
            self.app._schedule_settings_save()
        except Exception as error:  # noqa: BLE001 - Surface asynchronous and native playback failures.
            self._error(error)

    def _finish_alignment(self, future) -> None:
        self._alignment_pending = False
        try:
            timeline = future.result()
            removed = list(self._reading.removed) if self._reading is not None else []
            self._bind_text(timeline, removed)
            if self._state.path:
                self.name.set(self._state.path.name)
            if self._pending_play:
                self._pending_play = False
                self._play(self._pending_offset)
        except JobCancelled:
            self._pending_play = False
        except Exception as error:
            self._pending_play = False
            self.app.status.set(
                "Проигрывание доступно; синхронизация текста не удалась"
            )
            logger.error(
                "Audio alignment failed",
                exc_info=(type(error), error, error.__traceback__),
            )
            if self._state.path:
                self.name.set(self._state.path.name)

    def _selection_offset(self) -> int | None:
        selection = self.app.text.tag_ranges("sel")
        if not selection:
            return None
        if self._reading is None:
            raise ValueError(
                "Текст в редакторе не совпадает с записью. Восстановите текст записи или озвучьте новый текст."
            )
        return self._reading.original_offset(
            len(self.app.text.get("1.0", str(selection[0])))
        )

    def toggle(self) -> None:
        if self._loading:
            return
        try:
            if self._pending_play:
                self._pending_play = False
                self.app.status.set("Проигрывание отменено")
                return
            self._state = self.player.snapshot()
            if self._state.playing:
                self._consume(self._state.position)
                self.player.pause()
            elif self._state.path is None:
                self.app._start_synthesis(play_after=True)
            else:
                offset = self._selection_offset()
                needs_words = offset is not None or self.delete_spoken.get()
                if needs_words and (self._timeline is None or not self._timeline.words):
                    if self._alignment_pending:
                        self._pending_play, self._pending_offset = True, offset
                        self.app.status.set("Ожидание синхронизации слов...")
                        return
                    raise ValueError(
                        "Для этой записи нет синхронизации текста. Озвучьте текст заново."
                    )
                self._play(offset)
            self.app._schedule_settings_save()
        except Exception as error:  # noqa: BLE001 - Contain native playback failures at the UI boundary.
            self._error(error)

    def _play(self, offset: int | None) -> None:
        if offset is not None:
            if self._reading is None:
                raise ValueError("Текст записи был изменён")
            self.seek(self._timeline.time_for_char(offset))
            self.app.text.tag_remove("sel", "1.0", "end")
        elif self._state.position >= self._state.duration - 0.1:
            self.seek(0.0)
        self.player.play()
        self._state = self.player.snapshot()

    def stop(self) -> None:
        self._pending_play = False
        try:
            self._consume(self.player.snapshot().position)
            self.player.pause()
            self.seek(0.0)
        except Exception as error:  # noqa: BLE001 - Contain native playback failures at the UI boundary.
            self._error(error)

    def seek(self, position: float) -> None:
        self._pending_play = False
        state = self.player.snapshot()
        self._consume(state.position)
        self.player.seek(position)
        self._state = self.player.snapshot()
        self._run_start = self._delete_cursor = self._state.position
        self.position.set(self._state.position)
        self.app._schedule_settings_save()

    def _begin_seek(self, _event) -> None:
        if self._loading or self._state.path is None:
            return
        self._seeking = True
        self._resume_seek = self.player.snapshot().playing
        self.player.pause()

    def _end_seek(self, _event) -> None:
        if not self._seeking:
            return
        try:
            self.seek(self.position.get())
            if self._resume_seek:
                self.player.play()
        except Exception as error:  # noqa: BLE001 - Contain native playback failures at the UI boundary.
            self._error(error)
        finally:
            self._seeking = False

    def _volume_changed(self, _value) -> None:
        self.player.volume(self.volume.get())
        self.app._schedule_settings_save()

    def _deletion_changed(self) -> None:
        self._delete_cursor = self.player.snapshot().position
        self.app._schedule_settings_save()

    def _consume(self, position: float) -> None:
        if (
            not self.delete_spoken.get()
            or self._reading is None
            or not self._reading.timeline.words
        ):
            self._delete_cursor = position
            return
        deletions = self._reading.consume(
            self._delete_cursor, position, self._run_start, tk_units=self._tk_surrogates
        )
        self._delete_cursor = position
        if not deletions:
            return
        self._editing = True
        try:
            with edit_group(self.app.text):
                for start, end in deletions:
                    self.app.text.delete(f"1.0+{start}c", f"1.0+{end}c")
            self.app._on_text_modified()
        finally:
            self._editing = False

    def editor_changed(self) -> None:
        if self._editing or self._reading is None:
            return
        if self.app.text.get("1.0", "end-1c") == self._reading.remaining_text():
            return
        if self.delete_spoken.get():
            self.player.pause()
            self.delete_spoken.set(False)
        self._pending_play = False
        self._reading = None
        self.delete_check.configure(state="disabled")
        self.app.text.tag_remove("read_current", "1.0", "end")

    def restore_text(self) -> None:
        if self._timeline is None:
            return
        if not messagebox.askyesno(
            "Восстановить текст?",
            "Заменить черновик исходным текстом этой записи?",
            parent=self,
        ):
            return
        self.player.pause()
        self._editing = True
        try:
            with edit_group(self.app.text):
                self.app.text.delete("1.0", "end")
                self.app.text.insert("1.0", self._timeline.source)
            self.app._on_text_modified()
            self._bind_text(self._timeline)
            self._delete_cursor = self.player.snapshot().position
        finally:
            self._editing = False

    def settings(self) -> dict:
        state = self._state if self._closed or self._loading else self.player.snapshot()
        return {
            "player_path": str(state.path) if state.path else "",
            "player_position": state.position,
            "player_run_start": self._run_start,
            "player_stamp": self._stamp,
            "player_removed": list(self._reading.removed) if self._reading else [],
            "delete_spoken": self.delete_spoken.get(),
            "player_volume": self.volume.get(),
        }

    def _poll(self) -> None:
        if self._closed:
            return
        while True:
            try:
                kind, token, payload = self.events.get_nowait()
            except queue.Empty:
                break
            if token != self._request:
                continue
            if kind == "load":
                self._finish_load(*payload)
            elif kind == "align":
                self._finish_alignment(payload)
        if not self._loading and not self._seeking:
            try:
                was_playing = self._state.playing
                self._state = self.player.snapshot()
                state = self._state
                if state.playing or was_playing:
                    finished = (
                        was_playing
                        and not state.playing
                        and state.position >= state.duration - 0.1
                    )
                    self._consume(state.position + (0.13 if finished else 0.0))
                    if not state.playing:
                        self.player.pause()
                    if self._reading is not None and self._word_starts:
                        index = max(
                            0,
                            bisect.bisect_right(self._word_starts, state.position) - 1,
                        )
                        word = self._timeline.words[index]
                        start, end = (
                            self._reading.visible_offset(word.char_start),
                            self._reading.visible_offset(word.char_end),
                        )
                        if self._tk_surrogates:
                            start, end = (
                                self._reading.tk_offset(start),
                                self._reading.tk_offset(end),
                            )
                        self.app.text.tag_remove("read_current", "1.0", "end")
                        if start < end and state.playing:
                            self.app.text.tag_add(
                                "read_current", f"1.0+{start}c", f"1.0+{end}c"
                            )
                            self.app.text.see(f"1.0+{start}c")
                self.position.set(state.position)
                self.clock.set(
                    f"{format_time(state.position)} / {format_time(state.duration)}"
                )
                image = "pause" if state.playing else "play"
                if getattr(self, "_button_image", None) != image:
                    if hasattr(self.play_button, "image"):
                        self.play_button.image.configure(
                            file=str(
                                Path(__file__).parent
                                / "assets"
                                / "icons"
                                / f"{image}.png"
                            )
                        )
                    self._button_image = image
                if state.playing and time.monotonic() - self._last_save > 1.5:
                    self._last_save = time.monotonic()
                    self.app._queue_settings_save()
            except Exception as error:  # noqa: BLE001 - Do not let a decoder failure stop UI event processing.
                self._error(error, modal=False)
        self._timer = self.after(100, self._poll)

    def _error(self, error: Exception, *, modal: bool = True) -> None:
        logger.error(
            "Player failed", exc_info=(type(error), error, error.__traceback__)
        )
        self.app.status.set(str(error).splitlines()[0])
        self.player.pause()
        if modal:
            messagebox.showerror("Проигрывание", str(error), parent=self)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.after_cancel(self._timer)
        if self._alignment_cancel is not None:
            self._alignment_cancel.set()
        self.pool.shutdown(wait=True, cancel_futures=True)
        try:
            try:
                self._state = self.player.snapshot()
            except RuntimeError:
                logger.exception("Closing playback after a decoder failure")
            self.player.pause()
            self._state = self.player.snapshot()
            self._consume(self._state.position)
        finally:
            self.player.close()
