from __future__ import annotations

import logging
import os
import queue
import re
import threading
import tkinter as tk
import webbrowser
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Any

from .audio import validate_reference_audio
from .config import (
    CPML_URL,
    accept_noncommercial_license,
    is_license_accepted,
    output_dir,
)
from .editor import TextEditing, edit_group
from .engine import BUILTIN_SPEAKERS, VoiceSpec, XTTSEngine
from .history import HistoryEntry, ResultHistory
from .job import JobCancelled, SynthesisJob, SynthesisOptions
from .settings import SettingsStore
from .text_processing import SUPPORTED_DOCUMENTS, load_document
from .ui_helpers import Tooltip, icon_button
from .voice_library import SavedVoice, VoiceLibrary

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AppProfile:
    title: str = "AIBook XTTS v2"
    speakers: tuple[str, ...] = tuple(BUILTIN_SPEAKERS)
    engine_factory: Callable[[], Any] = XTTSEngine
    license_label: str = "CPML: личное некоммерческое использование"
    license_url: str = CPML_URL
    needs_cpml: bool = True
    voice_studio: bool = False
    icon_path: Path | None = None


class AIBookApp:
    def __init__(self, root: tk.Tk, profile: AppProfile | None = None) -> None:
        self.profile = profile or AppProfile()
        self.root = root
        self.root.title(self.profile.title)
        if self.profile.icon_path is not None and self.profile.icon_path.is_file():
            self.root.iconbitmap(str(self.profile.icon_path))
        self.root.geometry("1280x900" if self.profile.voice_studio else "1280x820")
        self.root.minsize(1050, 800 if self.profile.voice_studio else 680)

        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.io_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="aibook-io")
        self.synthesis_pool = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="aibook-xtts"
        )
        self.settings_pool = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="aibook-settings"
        )
        self.engine = self.profile.engine_factory()
        self.job = SynthesisJob(self.engine)
        self.cancel_event = threading.Event()
        self.job_future: Future[Path] | None = None
        self.clone_future: Future[SavedVoice] | None = None
        self._closing = False
        self._play_after_job = False
        self.voice_library = VoiceLibrary() if self.profile.voice_studio else None
        self._saved_voices: dict[str, SavedVoice] = {}
        self.last_output: Path | None = None
        self.history_store = ResultHistory(limit=10)
        self.history_paths: dict[str, Path] = {}
        self.settings_store = SettingsStore()
        self._settings_after_id: str | None = None
        saved = self.settings_store.load()
        self._document_request = 0
        self._document_pending = False
        self._loaded_source_path = saved["source_path"]

        self.source_path = tk.StringVar(value=saved["source_path"])
        self.output_folder = tk.StringVar(value=saved["output_folder"])
        self.output_format = tk.StringVar(value=saved["output_format"])
        self.voice_mode = tk.StringVar(value=saved["voice_mode"])
        self.speaker = tk.StringVar(
            value=saved["speaker"]
            if saved["speaker"] in self.profile.speakers
            else self.profile.speakers[0]
        )
        self.reference_path = tk.StringVar(value=saved["reference_path"])
        self.reference_text = tk.StringVar(value=saved["reference_text"])
        self.num_steps = tk.IntVar(value=saved["num_steps"])
        self.device = tk.StringVar(value=saved["device"])
        self.speed = tk.DoubleVar(value=saved["model_speed"])
        self.playback_speed = tk.DoubleVar(value=saved["playback_speed"])
        self.temperature = tk.DoubleVar(value=saved["temperature"])
        self.max_chars = tk.IntVar(value=saved["max_chars"])
        self.seed = tk.IntVar(value=saved["seed"])
        self.pitch_semitones = tk.DoubleVar(value=saved["pitch_semitones"])
        self.volume_db = tk.DoubleVar(value=saved["volume_db"])
        self.status = tk.StringVar(value="Готово к работе")
        self.text_stats = tk.StringVar(value="0 символов")

        self._configure_style()
        self._build_ui()
        if self.voice_library is not None:
            try:
                self.voice_library.import_cached()
                self._refresh_voices(saved.get("saved_voice_id", ""))
            except (OSError, RuntimeError, ValueError) as error:
                logger.exception("Voice library loading failed")
                self.status.set(str(error))
        draft = self.settings_store.load_draft()
        if draft:
            self.text.insert("1.0", draft)
            self.text.edit_modified(True)
            self._on_text_modified()
        self._update_voice_controls()
        self._attach_settings_traces()
        if self.profile.voice_studio:
            self.player_panel.restore(saved)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._drain_after_id = self.root.after(100, self._drain_events)

    def _configure_style(self) -> None:
        style = ttk.Style(self.root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Title.TLabel", font=("Segoe UI Semibold", 17))
        style.configure("Section.TLabelframe.Label", font=("Segoe UI Semibold", 10))
        style.configure(
            "Primary.TButton", font=("Segoe UI Semibold", 10), padding=(14, 7)
        )
        style.configure("TButton", padding=(9, 5))
        style.configure("Tool.TButton", padding=(5, 3))

    def _build_ui(self) -> None:
        container = ttk.Frame(
            self.root, padding=10 if self.profile.voice_studio else 14
        )
        container.pack(fill="both", expand=True)
        container.columnconfigure(0, weight=1)
        container.columnconfigure(1, weight=0, minsize=290)
        container.rowconfigure(3, weight=1)
        sidebar = None
        if self.profile.voice_studio:
            container.rowconfigure(3, minsize=190)
            sidebar = ttk.Frame(container)
            sidebar.grid(row=1, column=1, rowspan=7, sticky="nsew", padx=(12, 0))
            sidebar.columnconfigure(0, weight=1)
            sidebar.rowconfigure(0, weight=1)

        header = ttk.Frame(container)
        header.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 10))
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text=self.profile.title, style="Title.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        license_label = ttk.Label(
            header,
            text=self.profile.license_label,
            foreground="#555555",
            cursor="hand2",
        )
        license_label.grid(row=0, column=1, sticky="e")
        license_label.bind(
            "<Button-1>", lambda _event: webbrowser.open(self.profile.license_url)
        )

        files = ttk.LabelFrame(
            container, text="Книга и результат", style="Section.TLabelframe", padding=10
        )
        files.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        files.columnconfigure(1, weight=1)
        ttk.Label(files, text="Документ").grid(
            row=0, column=0, sticky="w", padx=(0, 8), pady=3
        )
        ttk.Entry(files, textvariable=self.source_path).grid(
            row=0, column=1, sticky="ew", pady=3
        )
        ttk.Button(files, text="Открыть", command=self._choose_document).grid(
            row=0, column=2, padx=(8, 0), pady=3
        )
        ttk.Label(files, text="Папка результата").grid(
            row=1, column=0, sticky="w", padx=(0, 8), pady=3
        )
        ttk.Entry(files, textvariable=self.output_folder).grid(
            row=1, column=1, sticky="ew", pady=3
        )
        ttk.Button(files, text="Выбрать", command=self._choose_output_folder).grid(
            row=1, column=2, padx=(8, 0), pady=3
        )
        if self.profile.voice_studio:
            format_frame = ttk.Frame(files)
            format_frame.grid(row=1, column=3, sticky="e", padx=(10, 0), pady=3)
            ttk.Label(format_frame, text="Формат").grid(row=0, column=0, padx=(0, 6))
            format_parent = format_frame
        else:
            ttk.Label(files, text="Формат").grid(
                row=2, column=0, sticky="w", padx=(0, 8), pady=3
            )
            format_parent = files
        ttk.Combobox(
            format_parent,
            textvariable=self.output_format,
            values=("wav", "mp3"),
            state="readonly",
            width=8,
        ).grid(row=0 if self.profile.voice_studio else 2, column=1, sticky="w", pady=3)

        voice = ttk.LabelFrame(
            container, text="Голос", style="Section.TLabelframe", padding=10
        )
        voice.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        voice.columnconfigure(2, weight=1)
        ttk.Radiobutton(
            voice,
            text="Готовый голос" if self.profile.voice_studio else "Встроенный",
            variable=self.voice_mode,
            value="builtin",
            command=self._update_voice_controls,
        ).grid(row=0, column=0, sticky="w")
        self.speaker_box = ttk.Combobox(
            voice,
            textvariable=self.speaker,
            values=self.profile.speakers,
            state="readonly",
            width=30 if self.profile.voice_studio else 24,
        )
        self.speaker_box.grid(row=0, column=1, sticky="w", padx=(8, 18))
        self.speaker_box.bind(
            "<<ComboboxSelected>>", lambda _event: self._update_voice_controls()
        )
        Tooltip(self.speaker_box, self.speaker)
        ttk.Radiobutton(
            voice,
            text="Клонировать из образца",
            variable=self.voice_mode,
            value="clone",
            command=self._update_voice_controls,
        ).grid(row=0, column=2, sticky="w")
        self.reference_entry = ttk.Entry(voice, textvariable=self.reference_path)
        self.reference_entry.grid(
            row=1, column=0, columnspan=3, sticky="ew", pady=(8, 0)
        )
        self.reference_button = ttk.Button(
            voice, text="Образец голоса", command=self._choose_reference
        )
        self.reference_button.grid(
            row=1, column=3, sticky="e", padx=(8, 0), pady=(8, 0)
        )
        if self.profile.voice_studio:
            voice_actions = ttk.Frame(voice)
            voice_actions.grid(row=0, column=3, sticky="e")
            self.save_voice_button = icon_button(
                voice_actions, "save", "Сохранить клон голоса", self._save_voice
            )
            self.save_voice_button.grid(row=0, column=0, padx=2)
            self.rename_voice_button = icon_button(
                voice_actions, "pencil", "Переименовать голос", self._rename_voice
            )
            self.rename_voice_button.grid(row=0, column=1, padx=2)
            self.delete_voice_button = icon_button(
                voice_actions, "trash-2", "Удалить голос", self._delete_voice
            )
            self.delete_voice_button.grid(row=0, column=2, padx=2)
            self.transcript_label = ttk.Label(voice, text="Текст образца")
            self.transcript_label.grid(row=2, column=0, sticky="w", pady=(8, 0))
            self.transcript_entry = ttk.Entry(voice, textvariable=self.reference_text)
            self.transcript_entry.grid(
                row=2, column=1, columnspan=3, sticky="ew", pady=(8, 0)
            )

        editor = ttk.LabelFrame(
            container, text="Текст", style="Section.TLabelframe", padding=8
        )
        editor.grid(row=3, column=0, sticky="nsew", pady=(0, 10))
        editor.columnconfigure(0, weight=1)
        editor.rowconfigure(0, weight=1)
        self.text = tk.Text(
            editor, wrap="word", undo=True, font=("Segoe UI", 11), padx=8, pady=8
        )
        self.text.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(editor, orient="vertical", command=self.text.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.text.configure(yscrollcommand=scrollbar.set)
        self.text_editing = TextEditing(self.text)
        self.text.bind("<<Modified>>", self._on_text_modified)
        ttk.Label(editor, textvariable=self.text_stats).grid(
            row=1, column=0, sticky="e", pady=(5, 0)
        )
        if self.profile.voice_studio:
            from .playback_ui import PlaybackPanel

            self.player_panel = PlaybackPanel(
                container, self, self.settings_store.load()
            )
            self.player_panel.grid(row=4, column=0, sticky="ew", pady=(0, 10))
        settings = ttk.LabelFrame(
            sidebar if sidebar is not None else container,
            text="Параметры",
            style="Section.TLabelframe",
            padding=10,
        )
        if sidebar is not None:
            settings.grid(row=1, column=0, sticky="ew", pady=(12, 0))
        else:
            settings.grid(row=4, column=0, sticky="ew", pady=(0, 10))
        for column in range(6):
            settings.columnconfigure(column, weight=1 if column in {1, 3, 5} else 0)
        ttk.Label(settings, text="Устройство").grid(row=0, column=0, sticky="w")
        ttk.Combobox(
            settings,
            textvariable=self.device,
            values=("auto", "cuda", "cpu"),
            state="readonly",
            width=8,
        ).grid(row=0, column=1, sticky="w", padx=(6, 16))
        ttk.Label(settings, text="Скорость модели").grid(row=0, column=2, sticky="w")
        ttk.Spinbox(
            settings,
            textvariable=self.speed,
            from_=0.7,
            to=3.0,
            increment=0.1,
            width=7,
        ).grid(row=0, column=3, sticky="w", padx=(6, 16))
        if self.profile.voice_studio:
            ttk.Label(settings, text="Шаги синтеза").grid(row=0, column=4, sticky="w")
            ttk.Combobox(
                settings,
                textvariable=self.num_steps,
                values=(16, 32, 64),
                state="readonly",
                width=7,
            ).grid(row=0, column=5, sticky="w", padx=(6, 16))
        else:
            ttk.Label(settings, text="Выразительность").grid(
                row=0, column=4, sticky="w"
            )
            ttk.Spinbox(
                settings,
                textvariable=self.temperature,
                from_=0.1,
                to=1.0,
                increment=0.05,
                width=7,
            ).grid(row=0, column=5, sticky="w", padx=(6, 16))
        ttk.Label(settings, text="Фрагмент").grid(
            row=1, column=0, sticky="w", pady=(8, 0)
        )
        ttk.Spinbox(
            settings,
            textvariable=self.max_chars,
            from_=100,
            to=300,
            increment=10,
            width=7,
        ).grid(row=1, column=1, sticky="w", padx=(6, 16), pady=(8, 0))
        ttk.Label(settings, text="Seed").grid(row=1, column=2, sticky="w", pady=(8, 0))
        ttk.Spinbox(
            settings, textvariable=self.seed, from_=0, to=999999, increment=1, width=8
        ).grid(row=1, column=3, sticky="w", padx=(6, 16), pady=(8, 0))
        ttk.Label(settings, text="Тембр, полутона").grid(
            row=1, column=4, sticky="w", pady=(8, 0)
        )
        ttk.Spinbox(
            settings,
            textvariable=self.pitch_semitones,
            from_=-6.0,
            to=6.0,
            increment=0.5,
            width=7,
        ).grid(row=1, column=5, sticky="w", padx=(6, 16), pady=(8, 0))
        ttk.Label(settings, text="Громкость, дБ").grid(
            row=2, column=0, sticky="w", pady=(8, 0)
        )
        ttk.Spinbox(
            settings,
            textvariable=self.volume_db,
            from_=-20.0,
            to=12.0,
            increment=1.0,
            width=7,
        ).grid(row=2, column=1, sticky="w", padx=(6, 16), pady=(8, 0))
        ttk.Label(settings, text="Скорость файла").grid(
            row=2, column=2, sticky="w", pady=(8, 0)
        )
        ttk.Spinbox(
            settings,
            textvariable=self.playback_speed,
            from_=0.5,
            to=3.0,
            increment=0.1,
            width=7,
        ).grid(row=2, column=3, sticky="w", padx=(6, 16), pady=(8, 0))

        if sidebar is not None:
            # Keep the same controls, arranged vertically beside the book editor.
            for widget in settings.winfo_children():
                placement = widget.grid_info()
                column = int(placement["column"])
                widget.grid_configure(
                    row=int(placement["row"]) * 3 + column // 2,
                    column=column % 2,
                    pady=4,
                    padx=(12, 0) if column % 2 else 0,
                )
            for column in range(6):
                settings.columnconfigure(column, weight=1 if column == 1 else 0)

        actions = ttk.Frame(container)
        actions.grid(row=5, column=0, sticky="ew")
        actions.columnconfigure(2, weight=1)
        self.start_button = ttk.Button(
            actions,
            text="Сохранить аудио" if self.profile.voice_studio else "Начать озвучку",
            style="Primary.TButton",
            command=self._start_synthesis,
        )
        self.start_button.grid(row=0, column=0, sticky="w")
        self.stop_button = ttk.Button(
            actions, text="Остановить", command=self._stop_synthesis, state="disabled"
        )
        self.stop_button.grid(row=0, column=1, sticky="w", padx=(8, 0))
        self.open_button = ttk.Button(
            actions,
            text="Открыть результат",
            command=self._open_result,
            state="disabled",
        )
        self.open_button.grid(row=0, column=3, sticky="e")

        self.progress = ttk.Progressbar(container, mode="determinate", maximum=1)
        self.progress.grid(row=6, column=0, sticky="ew", pady=(10, 4))
        status_label = ttk.Label(container, textvariable=self.status, wraplength=700)
        status_label.grid(row=7, column=0, sticky="ew")
        status_label.bind(
            "<Configure>",
            lambda event: status_label.configure(wraplength=max(100, event.width)),
        )
        self._build_history_panel(sidebar if sidebar is not None else container)
        self._refresh_history()

    def _build_history_panel(self, container: ttk.Frame) -> None:
        history = ttk.LabelFrame(
            container,
            text="Последние результаты",
            style="Section.TLabelframe",
            padding=8,
        )
        if self.profile.voice_studio:
            history.grid(row=0, column=0, sticky="nsew")
        else:
            history.grid(row=1, column=1, rowspan=7, sticky="nsew", padx=(12, 0))
        history.columnconfigure(0, weight=1)
        history.rowconfigure(0, weight=1)

        self.history_tree = ttk.Treeview(
            history,
            columns=("name", "created"),
            show="headings",
            selectmode="browse",
            height=18,
        )
        self.history_tree.heading("name", text="Файл")
        self.history_tree.heading("created", text="Создан")
        self.history_tree.column("name", width=185, minwidth=130, stretch=True)
        self.history_tree.column("created", width=82, minwidth=72, stretch=False)
        self.history_tree.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(
            history, orient="vertical", command=self.history_tree.yview
        )
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.history_tree.configure(yscrollcommand=scrollbar.set)
        self.history_tree.bind("<Double-1>", lambda _event: self._open_history_result())

        buttons = ttk.Frame(history)
        buttons.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        buttons.columnconfigure(0, weight=1)
        buttons.columnconfigure(1, weight=1)
        ttk.Button(buttons, text="Открыть", command=self._open_history_result).grid(
            row=0, column=0, sticky="ew", padx=(0, 4)
        )
        ttk.Button(buttons, text="Папка", command=self._open_history_folder).grid(
            row=0, column=1, sticky="ew", padx=(4, 0)
        )

    def _choose_document(self) -> None:
        patterns = " ".join(f"*{suffix}" for suffix in SUPPORTED_DOCUMENTS)
        selected = filedialog.askopenfilename(
            title="Выберите книгу",
            filetypes=[("Поддерживаемые документы", patterns), ("Все файлы", "*.*")],
        )
        if not selected:
            return
        self._document_request += 1
        request = self._document_request
        self._document_pending = True
        self.start_button.configure(state="disabled")
        self.source_path.set(selected)
        self.status.set("Чтение документа...")
        future = self.io_pool.submit(load_document, selected)
        future.add_done_callback(
            lambda item: self.events.put(("document", (request, selected, item)))
        )

    def _choose_output_folder(self) -> None:
        current = Path(self.output_folder.get().strip() or output_dir())
        selected = filedialog.askdirectory(
            title="Выберите папку для аудиокниг",
            initialdir=str(current if current.is_dir() else output_dir()),
        )
        if selected:
            self.output_folder.set(selected)

    def _choose_reference(self) -> None:
        selected = filedialog.askopenfilename(
            title="Выберите чистую запись голоса",
            filetypes=[
                ("Аудио", "*.wav *.mp3 *.flac *.ogg *.m4a"),
                ("Все файлы", "*.*"),
            ],
        )
        if selected:
            self.reference_path.set(selected)

    def _update_voice_controls(self) -> None:
        clone = self.voice_mode.get() == "clone"
        self.speaker_box.configure(state="disabled" if clone else "readonly")
        self.reference_entry.configure(state="normal" if clone else "disabled")
        self.reference_button.configure(state="normal" if clone else "disabled")
        if self.profile.voice_studio:
            self.transcript_entry.configure(state="normal" if clone else "disabled")
            for widget in (
                self.reference_entry,
                self.reference_button,
                self.transcript_label,
                self.transcript_entry,
            ):
                if clone:
                    widget.grid()
                else:
                    widget.grid_remove()
            busy = self._synthesis_busy()
            saved = self._selected_saved_voice() is not None and not clone
            self.save_voice_button.configure(
                state="normal" if clone and not busy else "disabled"
            )
            for button in (self.rename_voice_button, self.delete_voice_button):
                button.configure(state="normal" if saved and not busy else "disabled")

    def _synthesis_busy(self) -> bool:
        panel = getattr(self, "player_panel", None)
        return self._model_jobs_busy() or (panel is not None and panel.reader.active)

    def _model_jobs_busy(self) -> bool:
        return any(
            future is not None and not future.done()
            for future in (self.job_future, self.clone_future)
        )

    def reading_options(self):
        from .buffered_reading import ReadingOptions

        options = ReadingOptions(
            voice=self._voice_spec(),
            device=self.device.get(),
            max_chars=int(self.max_chars.get()),
            speed=float(self.speed.get()),
            playback_speed=float(self.playback_speed.get()),
            pitch_semitones=float(self.pitch_semitones.get()),
            volume_db=float(self.volume_db.get()),
            seed=int(self.seed.get()),
            num_steps=int(self.num_steps.get()),
        )
        options.validate()
        return options

    def prepare_reading_voice(
        self, voice: VoiceSpec, device: str, cancel: threading.Event
    ) -> VoiceSpec:
        path = self.engine.create_voice(
            voice,
            device,
            lambda message: self.events.put(("voice_progress", message)),
            cancel,
        )
        if cancel.is_set():
            raise JobCancelled("Reading stopped")
        entry = self.voice_library.add(
            path, voice.reference_audio.stem[:70], unique_name=True
        )
        self.events.put(
            ("voice_saved", (entry, (str(voice.reference_audio), voice.reference_text)))
        )
        return VoiceSpec(saved_prompt=self.voice_library.path(entry.id))

    def _selected_saved_voice(self) -> SavedVoice | None:
        return self._saved_voices.get(self.speaker.get())

    def _refresh_voices(self, selected_id: str = "") -> None:
        if self.voice_library is None:
            return
        if not selected_id and (selected := self._selected_saved_voice()):
            selected_id = selected.id
        entries = self.voice_library.list()
        self._saved_voices = {entry.label: entry for entry in entries}
        self.speaker_box.configure(values=(*self.profile.speakers, *self._saved_voices))
        selected = next((entry for entry in entries if entry.id == selected_id), None)
        if selected is not None:
            self.speaker.set(selected.label)
        elif self.speaker.get() not in (*self.profile.speakers, *self._saved_voices):
            self.speaker.set(self.profile.speakers[0])
        self._update_voice_controls()

    def _voice_spec(self) -> VoiceSpec:
        if self.voice_mode.get() != "clone":
            if self.voice_library is not None and (
                saved := self._selected_saved_voice()
            ):
                return VoiceSpec(saved_prompt=self.voice_library.path(saved.id))
            return VoiceSpec(speaker=self.speaker.get())
        reference = Path(self.reference_path.get().strip()).expanduser()
        duration = validate_reference_audio(reference)
        transcript = (
            self.reference_text.get().strip() if self.profile.voice_studio else ""
        )
        if self.profile.voice_studio:
            if not transcript:
                raise ValueError("Введите точный текст, произнесённый в образце голоса")
            if duration > 20:
                raise ValueError("Образец с текстом должен быть от 3 до 20 секунд")
        elif duration > 60:
            self.status.set("XTTS использует до 30 секунд образца")
        return VoiceSpec(reference_audio=reference, reference_text=transcript)

    def _prepare_voice(
        self, voice: VoiceSpec, name: str, device: str, *, automatic: bool = False
    ) -> SavedVoice:
        path = self.engine.create_voice(
            voice,
            device,
            lambda message: self.events.put(("voice_progress", message)),
            self.cancel_event,
        )
        if self.cancel_event.is_set():
            raise JobCancelled("Клонирование остановлено")
        return self.voice_library.add(path, name, unique_name=automatic)

    def _use_saved_voice(self, entry: SavedVoice, reference: tuple[str, str]) -> None:
        self._refresh_voices(entry.id)
        current = self.reference_path.get().strip()
        same_path = (
            bool(current)
            and Path(current).expanduser().resolve()
            == Path(reference[0]).expanduser().resolve()
        )
        if same_path and self.reference_text.get().strip() == reference[1]:
            self.reference_path.set("")
            self.reference_text.set("")
            self.voice_mode.set("builtin")
            self.speaker.set(entry.label)
        self._update_voice_controls()

    def _save_voice(self) -> None:
        if self._synthesis_busy():
            return
        try:
            voice = self._voice_spec()
            if voice.reference_audio is None:
                return
            name = simpledialog.askstring(
                "Название голоса",
                "Название",
                initialvalue=voice.reference_audio.stem[:70],
                parent=self.root,
            )
            if name is None:
                return
            VoiceLibrary._name(name, self.voice_library.list())
            reference = (
                self.reference_path.get().strip(),
                self.reference_text.get().strip(),
            )
            self.cancel_event.clear()
            self.clone_future = self.synthesis_pool.submit(
                self._prepare_voice, voice, name, self.device.get()
            )
            self.clone_future.add_done_callback(
                lambda future: self.events.put(("clone", (future, reference)))
            )
            self.start_button.configure(state="disabled")
            self.stop_button.configure(state="normal")
            self.status.set("Клонирование голоса...")
            self._update_voice_controls()
        except (OSError, ValueError, RuntimeError) as error:
            messagebox.showerror(
                "Клонирование голоса", _friendly_error(error), parent=self.root
            )

    def _rename_voice(self) -> None:
        entry = self._selected_saved_voice()
        if entry is None or self._synthesis_busy():
            return
        name = simpledialog.askstring(
            "Переименовать голос", "Название", initialvalue=entry.name, parent=self.root
        )
        if name is not None:
            try:
                self.voice_library.rename(entry.id, name)
                self._refresh_voices(entry.id)
                self._schedule_settings_save()
            except (OSError, ValueError, RuntimeError) as error:
                messagebox.showerror("Голоса", _friendly_error(error), parent=self.root)

    def _delete_voice(self) -> None:
        entry = self._selected_saved_voice()
        if entry is None or self._synthesis_busy():
            return
        if not messagebox.askyesno(
            "Удалить голос?",
            f"Удалить сохранённый голос «{entry.name}»?",
            parent=self.root,
        ):
            return
        try:
            self.voice_library.delete(entry.id)
            self._refresh_voices()
            self._schedule_settings_save()
        except (OSError, ValueError, RuntimeError) as error:
            messagebox.showerror("Голоса", _friendly_error(error), parent=self.root)

    def _on_text_modified(self, _event: object | None = None) -> None:
        if not self.text.edit_modified():
            return
        value = self.text.get("1.0", "end-1c")
        self.text_stats.set(f"{len(value):,} символов".replace(",", " "))
        self.text.edit_modified(False)
        if self.profile.voice_studio:
            self.player_panel.editor_changed()
        self._schedule_settings_save()

    def _attach_settings_traces(self) -> None:
        variables = (
            self.source_path,
            self.output_folder,
            self.output_format,
            self.voice_mode,
            self.speaker,
            self.reference_path,
            self.reference_text,
            self.num_steps,
            self.device,
            self.speed,
            self.playback_speed,
            self.temperature,
            self.max_chars,
            self.seed,
            self.pitch_semitones,
            self.volume_db,
        )
        for variable in variables:
            variable.trace_add("write", self._schedule_settings_save)

    def _schedule_settings_save(self, *_args: object) -> None:
        if not hasattr(self, "text") or self._closing:
            return
        if self._settings_after_id is not None:
            self.root.after_cancel(self._settings_after_id)
        self._settings_after_id = self.root.after(600, self._queue_settings_save)

    def _queue_settings_save(self) -> None:
        if self._settings_after_id is not None:
            self.root.after_cancel(self._settings_after_id)
        self._settings_after_id = None
        payload = self._settings_payload()
        draft = self.text.get("1.0", "end-1c")
        self.settings_pool.submit(self.settings_store.save, payload, draft)

    def _settings_payload(self) -> dict[str, object]:
        payload = {
            "source_path": self._loaded_source_path
            if self._document_pending
            else self.source_path.get(),
            "output_folder": self.output_folder.get(),
            "output_format": self.output_format.get(),
            "voice_mode": self.voice_mode.get(),
            "speaker": self.speaker.get(),
            "reference_path": self.reference_path.get(),
            "reference_text": self.reference_text.get(),
            "num_steps": _variable_value(self.num_steps, 32),
            "device": self.device.get(),
            "model_speed": _variable_value(self.speed, 1.0),
            "playback_speed": _variable_value(self.playback_speed, 1.0),
            "temperature": _variable_value(self.temperature, 0.65),
            "max_chars": _variable_value(self.max_chars, 220),
            "seed": _variable_value(self.seed, 42),
            "pitch_semitones": _variable_value(self.pitch_semitones, 0.0),
            "volume_db": _variable_value(self.volume_db, 0.0),
        }
        selected = self._selected_saved_voice()
        payload["saved_voice_id"] = selected.id if selected else ""
        if self.profile.voice_studio:
            payload.update(self.player_panel.settings())
        return payload

    def _save_settings_now(self) -> None:
        if self._settings_after_id is not None:
            self.root.after_cancel(self._settings_after_id)
            self._settings_after_id = None
        payload = self._settings_payload()
        draft = self.text.get("1.0", "end-1c")
        self.settings_pool.shutdown(wait=True, cancel_futures=True)
        self.settings_store.save(payload, draft)

    def _start_synthesis(self, *, play_after: bool = False) -> None:
        if self._document_pending:
            return
        if self._synthesis_busy():
            if play_after:
                self._play_after_job = True
            return
        try:
            text = self.text.get("1.0", "end-1c")
            if not text.strip():
                raise ValueError("Добавьте текст книги")
            destination_folder = Path(
                self.output_folder.get().strip() or output_dir()
            ).expanduser()
            destination_folder.mkdir(parents=True, exist_ok=True)
            output_format = self.output_format.get().lower()
            if output_format not in {"wav", "mp3"}:
                raise ValueError("Формат результата должен быть WAV или MP3")
            destination = _unique_output_path(
                destination_folder,
                _result_stem(self.source_path.get(), text),
                output_format,
            )

            voice = self._voice_spec()

            max_chars = int(self.max_chars.get())
            temperature = float(self.temperature.get())
            speed = float(self.speed.get())
            playback_speed = float(self.playback_speed.get())
            seed = int(self.seed.get())
            pitch_semitones = float(self.pitch_semitones.get())
            volume_db = float(self.volume_db.get())
            if not 100 <= max_chars <= 300:
                raise ValueError("Размер фрагмента должен быть от 100 до 300 символов")
            if not 0.1 <= temperature <= 1.0:
                raise ValueError("Выразительность должна быть от 0.1 до 1.0")
            if not 0.7 <= speed <= 3.0:
                raise ValueError("Скорость должна быть от 0.7 до 3.0")
            if not 0.5 <= playback_speed <= 3.0:
                raise ValueError("Скорость файла должна быть от 0.5 до 3.0")
            if not -6.0 <= pitch_semitones <= 6.0:
                raise ValueError("Тембр должен быть от -6 до +6 полутонов")
            if not -20.0 <= volume_db <= 12.0:
                raise ValueError("Громкость должна быть от -20 до +12 дБ")

            if self.profile.voice_studio and int(self.num_steps.get()) not in {
                16,
                32,
                64,
            }:
                raise ValueError("Выберите 16, 32 или 64 шага синтеза")

            if self.profile.needs_cpml and not is_license_accepted():
                accepted = messagebox.askyesno(
                    "Лицензия XTTS v2",
                    "XTTS v2 распространяется по CPML. Продолжая, вы подтверждаете личное "
                    "некоммерческое использование и принятие условий лицензии.\n\nПродолжить?",
                    parent=self.root,
                )
                if not accepted:
                    return
                accept_noncommercial_license()
                os.environ["COQUI_TOS_AGREED"] = "1"

            options = SynthesisOptions(
                output_path=destination,
                voice=voice,
                device=self.device.get(),
                max_chars=max_chars,
                temperature=temperature,
                speed=speed,
                playback_speed=playback_speed,
                seed=seed,
                pitch_semitones=pitch_semitones,
                volume_db=volume_db,
                num_steps=int(self.num_steps.get())
                if self.profile.voice_studio
                else None,
                include_text=self.profile.voice_studio,
            )
        except (OSError, ValueError, tk.TclError) as exc:
            message = (
                "Проверьте числовые значения параметров озвучки"
                if isinstance(exc, tk.TclError)
                else str(exc)
            )
            messagebox.showerror("Невозможно начать озвучку", message, parent=self.root)
            return

        self.cancel_event.clear()
        self._play_after_job = play_after
        self.last_output = None
        self.progress.configure(value=0, maximum=1)
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.open_button.configure(state="disabled")
        self.status.set("Подготовка...")
        self.job_future = self.synthesis_pool.submit(
            self._run_book,
            text,
            options,
            self.cancel_event,
            lambda done, total, message: self.events.put(
                ("progress", (done, total, message))
            ),
        )
        self.job_future.add_done_callback(lambda item: self.events.put(("job", item)))
        self._update_voice_controls()

    def _run_book(
        self, text: str, options: SynthesisOptions, cancel: threading.Event, progress
    ) -> Path:
        if self.profile.voice_studio and options.voice.reference_audio is not None:
            original = options.voice
            entry = self._prepare_voice(
                original,
                original.reference_audio.stem[:70],
                options.device,
                automatic=True,
            )
            self.events.put(
                (
                    "voice_saved",
                    (entry, (str(original.reference_audio), original.reference_text)),
                )
            )
            options = replace(
                options, voice=VoiceSpec(saved_prompt=self.voice_library.path(entry.id))
            )
        return self.job.run(text, options, cancel, progress)

    def _stop_synthesis(self) -> None:
        if self.profile.voice_studio and self.player_panel.reader.active:
            self.player_panel.reader.stop()
            self.status.set("Чтение остановлено")
            return
        self.cancel_event.set()
        self.stop_button.configure(state="disabled")
        self.status.set(
            "Остановка..."
            if self.profile.voice_studio
            else "Остановка после текущего фрагмента..."
        )

    def _open_result(self) -> None:
        if self.last_output and self.last_output.is_file():
            if self.profile.voice_studio:
                if self.player_panel._state.path == self.last_output.resolve():
                    self.player_panel.toggle()
                else:
                    self.player_panel.load(self.last_output, autoplay=True)
            else:
                os.startfile(self.last_output)  # type: ignore[attr-defined]

    def _refresh_history(self, entries: list[HistoryEntry] | None = None) -> None:
        current = entries if entries is not None else self.history_store.load()
        self.history_paths.clear()
        for item in self.history_tree.get_children():
            self.history_tree.delete(item)
        for entry in current[:10]:
            item_id = self.history_tree.insert(
                "",
                "end",
                values=(entry.path.name, entry.display_time),
            )
            self.history_paths[item_id] = entry.path

    def _selected_history_path(self) -> Path | None:
        selection = self.history_tree.selection()
        if not selection:
            return None
        path = self.history_paths.get(selection[0])
        if path is None:
            return None
        if not path.is_file():
            self._refresh_history()
            messagebox.showwarning(
                "Файл не найден",
                "Результат был перемещен или удален.",
                parent=self.root,
            )
            return None
        return path

    def _open_history_result(self) -> None:
        if path := self._selected_history_path():
            if self.profile.voice_studio:
                self.player_panel.load(path, autoplay=True)
            else:
                os.startfile(path)  # type: ignore[attr-defined]

    def _open_history_folder(self) -> None:
        if path := self._selected_history_path():
            os.startfile(path.parent)  # type: ignore[attr-defined]

    def _drain_events(self) -> None:
        if self._closing:
            return
        self.root.after_cancel(self._drain_after_id)
        try:
            while True:
                event, payload = self.events.get_nowait()
                if event == "progress":
                    done, total, message = payload  # type: ignore[misc]
                    self.progress.configure(maximum=max(1, total), value=done)
                    self.status.set(message)
                elif event == "document":
                    request, source, future = payload  # type: ignore[misc]
                    if request == self._document_request:
                        self._finish_document_load(future, source)
                elif event == "job":
                    self._finish_job(payload)  # type: ignore[arg-type]
                elif event == "voice_progress":
                    self.status.set(str(payload))
                elif event == "voice_saved":
                    self._use_saved_voice(*payload)
                elif event == "clone":
                    self._finish_clone(*payload)
        except queue.Empty:
            pass
        self._drain_after_id = self.root.after(100, self._drain_events)

    def _finish_clone(
        self, future: Future[SavedVoice], reference: tuple[str, str]
    ) -> None:
        self.start_button.configure(
            state="disabled" if self._document_pending else "normal"
        )
        self.stop_button.configure(state="disabled")
        try:
            entry = future.result()
            self._use_saved_voice(entry, reference)
            self.status.set(f"Голос сохранён: {entry.name}")
        except JobCancelled as error:
            self.status.set(str(error))
        except Exception as error:
            logger.error(
                "Voice cloning failed",
                exc_info=(type(error), error, error.__traceback__),
            )
            self.status.set("Ошибка клонирования голоса")
            messagebox.showerror(
                "Клонирование голоса", _friendly_error(error), parent=self.root
            )
        self._update_voice_controls()

    def _finish_document_load(self, future: Future[str], source: str) -> None:
        self._document_pending = False
        self.start_button.configure(
            state="disabled" if self._synthesis_busy() else "normal"
        )
        try:
            value = future.result()
        except Exception as exc:
            logger.error(
                "Document loading failed",
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            self.status.set("Ошибка чтения документа")
            self.source_path.set(self._loaded_source_path)
            messagebox.showerror(
                "Не удалось открыть документ", _friendly_error(exc), parent=self.root
            )
            return
        self._loaded_source_path = source
        self.source_path.set(source)
        with edit_group(self.text):
            self.text.delete("1.0", "end")
            self.text.insert("1.0", value)
        self.text.edit_modified(True)
        self._on_text_modified()
        self.status.set("Документ загружен")

    def _finish_job(self, future: Future[Path]) -> None:
        self.start_button.configure(
            state="disabled" if self._document_pending else "normal"
        )
        self.stop_button.configure(state="disabled")
        self._update_voice_controls()
        try:
            result = future.result()
        except JobCancelled as exc:
            self.status.set(str(exc))
            return
        except Exception as exc:
            logger.error(
                "Speech synthesis failed",
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            self.status.set("Озвучка завершилась с ошибкой")
            messagebox.showerror(
                "Ошибка озвучки", _friendly_error(exc), parent=self.root
            )
            return
        self.last_output = result
        self._refresh_history(self.history_store.add(result))
        self.open_button.configure(state="normal")
        self.status.set(
            f"Готово: {result.name if self.profile.voice_studio else result}"
        )
        if self.profile.voice_studio:
            if self.player_panel.mode.get() == "file" or self._play_after_job:
                self.player_panel.load(result, autoplay=self._play_after_job)
            self._play_after_job = False

    def _on_close(self) -> None:
        if self._closing:
            return
        if self._model_jobs_busy():
            if not messagebox.askyesno(
                "Остановить озвучку?",
                "Готовые фрагменты останутся в кэше для продолжения.",
                parent=self.root,
            ):
                return
            self.cancel_event.set()
        self._closing = True
        self.root.after_cancel(self._drain_after_id)
        if self.profile.voice_studio:
            self.player_panel.close()
        self._save_settings_now()
        self.io_pool.shutdown(wait=True, cancel_futures=True)
        self.synthesis_pool.submit(self.engine.unload)
        self.synthesis_pool.shutdown(wait=True)
        from .ui_helpers import release_images

        release_images(self.root)
        self.root.update_idletasks()
        self.root.destroy()


def run_app(*, smoke_test: bool = False, profile: AppProfile | None = None) -> None:
    root = tk.Tk()
    app = AIBookApp(root, profile=profile)
    if smoke_test:
        root.withdraw()
        root.after(500, app._on_close)
    root.mainloop()


def _friendly_error(error: BaseException) -> str:
    message = str(error).strip()
    lowered = message.lower()
    if "libtorchcodec" in lowered or "audiodecoder" in lowered:
        return (
            "Не удалось прочитать образец голоса. Перезапустите AIBook, затем "
            "повторно выберите аудиофайл. Подробности записаны в aibook.log."
        )
    if "out of memory" in lowered:
        return "Недостаточно памяти. Закройте приложения, использующие GPU, или выберите CPU."
    first_line = next(
        (line.strip() for line in message.splitlines() if line.strip()),
        "Неизвестная ошибка",
    )
    return first_line[:600]


def _result_stem(source_path: str, text: str) -> str:
    candidate = Path(source_path).stem if source_path.strip() else ""
    if not candidate:
        candidate = next(
            (line.strip() for line in text.splitlines() if line.strip()), "audiobook"
        )[:64]
    candidate = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", candidate)
    candidate = re.sub(r"\s+", " ", candidate).strip(" .")
    reserved = {"CON", "PRN", "AUX", "NUL"}
    reserved.update({f"COM{index}" for index in range(1, 10)})
    reserved.update({f"LPT{index}" for index in range(1, 10)})
    if not candidate or candidate.upper() in reserved:
        return "audiobook"
    return candidate


def _unique_output_path(folder: Path, stem: str, extension: str) -> Path:
    candidate = folder / f"{stem}.{extension}"
    index = 2
    while candidate.exists():
        candidate = folder / f"{stem}_{index}.{extension}"
        index += 1
    return candidate


def _variable_value(variable: tk.Variable, fallback: object) -> object:
    try:
        return variable.get()
    except tk.TclError:
        return fallback
