from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import soundfile as sf

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PlaybackState:
    path: Path | None = None
    duration: float = 0.0
    position: float = 0.0
    playing: bool = False


class AudioPlayer:
    """Stream decoding is bounded; the audio callback never touches Tk or control locks."""

    SAMPLE_RATE = 24000
    BUFFER_SECONDS = 0.075

    def __init__(self, device_factory=None) -> None:
        self._device_factory = device_factory
        self._control_lock = threading.RLock()
        self._state_lock = threading.Lock()
        self._device = None
        self._stream = None
        self._path: Path | None = None
        self._duration = self._position = self._offset = 0.0
        self._playing = self._eof = False
        self._submitted = 0
        self._clock = 0.0
        self._volume = 0.8
        self._generation = uuid4().hex
        self._error: Exception | None = None

    def load(self, path: Path, position: float = 0.0) -> PlaybackState:
        path = path.resolve()
        if not path.is_file():
            raise FileNotFoundError(f"Аудиофайл не найден: {path}")
        if path.suffix.lower() not in {".wav", ".mp3"}:
            raise ValueError("Плеер поддерживает WAV и MP3")
        duration = sf.info(str(path)).duration
        if duration <= 0:
            raise ValueError("Аудиофайл пуст")
        with self._control_lock:
            self.close()
            with self._state_lock:
                self._path, self._duration = path, duration
                self._position = min(duration, max(0.0, float(position)))
            return self.snapshot()

    def snapshot(self) -> PlaybackState:
        with self._state_lock:
            if self._error is not None:
                error, self._error = self._error, None
                raise RuntimeError(f"Ошибка проигрывания: {error}") from error
            position = self._position
            if self._playing:
                elapsed = max(0.0, time.monotonic() - self._clock - self.BUFFER_SECONDS)
                available = max(
                    0.0,
                    self._submitted / self.SAMPLE_RATE
                    - (0.0 if self._eof else self.BUFFER_SECONDS),
                )
                position = min(self._duration, self._offset + min(elapsed, available))
                if self._eof and elapsed >= self._submitted / self.SAMPLE_RATE:
                    self._playing = False
                    self._position = position = min(
                        self._duration,
                        self._offset + self._submitted / self.SAMPLE_RATE,
                    )
            return PlaybackState(self._path, self._duration, position, self._playing)

    def _halt(self) -> None:
        try:
            state = self.snapshot()
        except RuntimeError:
            logger.exception("Stopping playback after a decoder failure")
            with self._state_lock:
                state = PlaybackState(self._path, self._duration, self._position)
        # ma_device_stop waits for its callback; never hold _state_lock here.
        if self._device is not None:
            self._device.stop()
        if self._stream is not None:
            self._stream.close()
            self._stream = None
        with self._state_lock:
            self._position = state.position
            self._playing = False
            self._generation = uuid4().hex

    def play(self) -> None:
        import miniaudio

        with self._control_lock:
            self._halt()
            state = self.snapshot()
            if state.path is None:
                raise ValueError("Сначала выберите аудиофайл")
            if self._device is None:
                try:
                    self._device = (self._device_factory or miniaudio.PlaybackDevice)(
                        output_format=miniaudio.SampleFormat.FLOAT32,
                        nchannels=1,
                        sample_rate=self.SAMPLE_RATE,
                        buffersize_msec=50,
                    )
                except miniaudio.MiniaudioError as error:
                    raise RuntimeError(
                        "Не найдено доступное звуковое устройство. Проверьте аудиовыход Windows."
                    ) from error
            position = 0.0 if state.position >= self._duration - 0.1 else state.position
            decoder = miniaudio.stream_file(
                str(state.path),
                output_format=miniaudio.SampleFormat.FLOAT32,
                nchannels=1,
                sample_rate=self.SAMPLE_RATE,
                seek_frame=round(position * self.SAMPLE_RATE),
            )
            with self._state_lock:
                self._offset = self._position = position
                self._submitted = 0
                self._eof = False
                self._error = None
                self._clock = time.monotonic()
                self._playing = True
                generation = self._generation
            self._stream = self._callback(decoder, generation)
            next(self._stream)
            try:
                self._device.start(self._stream)
            except BaseException:
                self._halt()
                raise
            with self._state_lock:
                self._clock = time.monotonic()

    def _callback(self, decoder, generation: str):
        import numpy as np

        wanted = yield b""
        try:
            while True:
                with self._state_lock:
                    volume, eof = self._volume, self._eof
                if eof:
                    samples = np.zeros(wanted, dtype=np.float32)
                else:
                    try:
                        block = decoder.send(wanted)
                        samples = np.asarray(block, dtype=np.float32) * volume
                    except StopIteration:
                        samples = np.zeros(wanted, dtype=np.float32)
                        with self._state_lock:
                            self._eof = True
                    except Exception as error:  # noqa: BLE001 - Contain failures inside the native audio callback.
                        samples = np.zeros(wanted, dtype=np.float32)
                        with self._state_lock:
                            self._error, self._eof = error, True
                    else:
                        with self._state_lock:
                            if generation == self._generation:
                                self._submitted += len(samples)
                        if len(samples) < wanted:
                            samples = np.pad(samples, (0, wanted - len(samples)))
                wanted = yield samples
        finally:
            decoder.close()

    def pause(self) -> None:
        with self._control_lock:
            self._halt()

    def seek(self, position: float) -> None:
        with self._control_lock:
            playing = self.snapshot().playing
            self._halt()
            with self._state_lock:
                self._position = min(self._duration, max(0.0, float(position)))
            if playing:
                self.play()

    def volume(self, value: float) -> None:
        with self._state_lock:
            self._volume = min(1.0, max(0.0, value))

    def close(self) -> None:
        with self._control_lock:
            try:
                self._halt()
            finally:
                if self._device is not None:
                    self._device.close()
                    self._device = None
                with self._state_lock:
                    self._path = None
                    self._duration = self._position = 0.0


def format_time(value: float) -> str:
    seconds = max(0, int(value))
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return (
        f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"
    )
