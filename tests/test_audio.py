from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np
import soundfile as sf

from aibook.audio import combine_chunks, prepare_reference_wav


class AudioTests(unittest.TestCase):
    def test_failed_assembly_preserves_previous_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "previous.wav"
            sf.write(output, np.ones(2400, dtype=np.float32) * 0.1, 24000)
            original = output.read_bytes()
            first, second = root / "one.wav", root / "two.wav"
            sf.write(first, np.zeros(2400), 24000)
            sf.write(second, np.zeros(2205), 22050)
            with self.assertRaises(ValueError):
                combine_chunks([first, second], [0, 0], output)
            self.assertEqual(output.read_bytes(), original)
            self.assertFalse(list(root.glob("*.aibook.raw.wav")))

    def test_combines_chunks_with_requested_pause(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sample_rate = 24000
            first = root / "first.wav"
            second = root / "second.wav"
            output = root / "combined.wav"
            sf.write(first, np.full(sample_rate, 0.1, dtype=np.float32), sample_rate)
            sf.write(
                second, np.full(sample_rate // 2, -0.1, dtype=np.float32), sample_rate
            )

            combine_chunks([first, second], [250, 0], output)

            info = sf.info(output)
            self.assertEqual(info.samplerate, sample_rate)
            self.assertEqual(info.channels, 1)
            self.assertEqual(
                info.frames, sample_rate + sample_rate // 4 + sample_rate // 2
            )

    def test_prepares_mono_22050_reference(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "stereo.wav"
            destination = root / "prepared.wav"
            samples = np.column_stack(
                (
                    np.linspace(-0.2, 0.2, 16000 * 4, dtype=np.float32),
                    np.linspace(0.2, -0.2, 16000 * 4, dtype=np.float32),
                )
            )
            sf.write(source, samples, 16000)

            prepare_reference_wav(source, destination)

            info = sf.info(destination)
            self.assertEqual(info.samplerate, 22050)
            self.assertEqual(info.channels, 1)
            self.assertGreaterEqual(info.duration, 3.9)

    def test_applies_pitch_and_volume_without_changing_duration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sample_rate = 24000
            seconds = 2
            time = np.arange(sample_rate * seconds, dtype=np.float32) / sample_rate
            source = root / "tone.wav"
            output = root / "processed.wav"
            sf.write(source, 0.1 * np.sin(2 * np.pi * 440 * time), sample_rate)

            combine_chunks(
                [source],
                [0],
                output,
                volume_db=6.0,
                pitch_semitones=3.0,
            )

            samples, output_rate = sf.read(output, dtype="float32")
            frequencies = np.fft.rfftfreq(len(samples), 1 / output_rate)
            dominant = frequencies[np.argmax(np.abs(np.fft.rfft(samples)))]
            self.assertAlmostEqual(len(samples) / output_rate, seconds, delta=0.08)
            self.assertAlmostEqual(dominant, 523.25, delta=15.0)
            self.assertGreater(float(np.sqrt(np.mean(samples * samples))), 0.12)

    def test_playback_speed_compresses_final_audio(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sample_rate = 24000
            source = root / "source.wav"
            output = root / "fast.wav"
            sf.write(source, np.zeros(sample_rate * 2, dtype=np.float32), sample_rate)

            combine_chunks([source], [0], output, playback_speed=2.0)

            self.assertAlmostEqual(sf.info(output).duration, 1.0, delta=0.08)


if __name__ == "__main__":
    unittest.main()
