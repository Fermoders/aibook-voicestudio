from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path

from filelock import FileLock, Timeout

from .config import app_data_dir
from .job import JobCancelled
from .timeline import AudioTimeline, sidecar_path
from .voice_studio_config import workspace_dir


def align_audio(audio: Path, cancel: threading.Event) -> AudioTimeline | None:
    lock = FileLock(str(sidecar_path(audio)) + ".lock")
    while not cancel.is_set():
        try:
            lock.acquire(timeout=0.1)
            break
        except Timeout:
            continue
    else:
        raise JobCancelled("Синхронизация остановлена")
    try:
        timeline = AudioTimeline.load(audio)
        if timeline is None or timeline.words:
            return timeline
        executable = Path(sys.executable)
        if executable.name.lower() == "pythonw.exe":
            executable = executable.with_name("python.exe")
        log = app_data_dir() / "alignment.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        environment = os.environ.copy()
        environment.update(
            {
                "HF_HUB_OFFLINE": "1",
                "TRANSFORMERS_OFFLINE": "1",
                "PYTHONUTF8": "1",
                "PYTHONNOUSERSITE": "1",
                "OMP_NUM_THREADS": "4",
                "NUMBA_CACHE_DIR": str(app_data_dir() / "numba"),
            }
        )
        with log.open("ab", buffering=0) as output:
            process = subprocess.Popen(
                [
                    str(executable),
                    "-u",
                    "-m",
                    "aibook.alignment_worker",
                    str(audio.resolve()),
                ],
                cwd=workspace_dir(),
                stdout=output,
                stderr=output,
                env=environment,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            try:
                while process.poll() is None:
                    if cancel.wait(0.1):
                        raise JobCancelled("Синхронизация остановлена")
                if process.returncode:
                    raise RuntimeError(
                        "Не удалось синхронизировать слова. Подробности: alignment.log"
                    )
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
        return AudioTimeline.load(audio)
    finally:
        lock.release()
