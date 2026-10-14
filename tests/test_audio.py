from pathlib import Path
import sys
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from model.audio import SAMPLE_RATE, audio_features, spectrum
from model.level.beat_onset import BeatOnsetDetector
from model.audio_stream import LiveAudioBuffer
from model.settings import normalized_settings


class AudioTests(unittest.TestCase):
    def setUp(self):
        self.times = np.arange(SAMPLE_RATE * 6) / SAMPLE_RATE
        self.attacks = np.arange(0.5, 5.9, 0.5)

    def kicks(self, amplitude=0.12):
        audio = np.zeros(len(self.times))
        for start in self.attacks:
            elapsed = self.times - start
            mask = (elapsed >= 0) & (elapsed < 0.18)
            audio[mask] += amplitude * np.sin(2 * np.pi * 70 * elapsed[mask]) * np.exp(-30 * elapsed[mask])
        return audio

    def detect(self, audio, frame_size=1024):
        detector = BeatOnsetDetector()
        found, levels = [], []
        for offset in range(0, len(audio) - frame_size, frame_size):
            levels.append(detector(audio[offset:offset + frame_size]))
            if detector.last_onset:
                found.append(offset / SAMPLE_RATE)
        return np.asarray(found), np.asarray(levels)

    def assert_attacks(self, audio, frame_size=1024):
        found, levels = self.detect(audio, frame_size)
        self.assertEqual(len(found), len(self.attacks), found)
        np.testing.assert_allclose(found, self.attacks, atol=frame_size / SAMPLE_RATE)
        self.assertTrue(np.all(np.isfinite(levels)))
        self.assertTrue(np.all((levels >= 0) & (levels <= 1)))

    def test_kicks_across_volume_and_frame_sizes(self):
        for frame_size in (1024, 2205):
            for amplitude in (0.01, 0.12, 0.7):
                with self.subTest(frame_size=frame_size, amplitude=amplitude):
                    self.assert_attacks(self.kicks(amplitude), frame_size)

    def test_beats_over_tone_and_background_noise(self):
        tone = 0.25 * np.sin(2 * np.pi * 440 * self.times)
        noise = 0.02 * np.random.default_rng(7).normal(size=len(self.times))
        self.assert_attacks(self.kicks() + tone)
        self.assert_attacks(self.kicks() + noise)

    def test_broadband_percussion(self):
        audio = np.zeros(len(self.times))
        rng = np.random.default_rng(7)
        for start in self.attacks:
            elapsed = self.times - start
            mask = (elapsed >= 0) & (elapsed < 0.18)
            audio[mask] += 0.12 * rng.normal(size=sum(mask)) * np.exp(-30 * elapsed[mask])
        self.assert_attacks(audio)

    def test_percussive_strengths_do_not_clip_to_full_scale(self):
        found, levels = self.detect(self.kicks(0.7))
        self.assertEqual(len(found), len(self.attacks))
        self.assertGreater(levels.max(), 0.5)
        self.assertLess(levels.max(), 0.9)

    def test_stationary_signals_do_not_trigger(self):
        signals = [np.zeros(len(self.times)), 0.02 * np.random.default_rng(7).normal(size=len(self.times))]
        signals += [0.2 * np.sin(2 * np.pi * hz * self.times) for hz in (60, 90, 440, 3000)]
        for index, audio in enumerate(signals):
            with self.subTest(signal=index):
                found, levels = self.detect(audio)
                self.assertEqual(len(found), 0, found)
                self.assertEqual(float(np.max(levels)), 0.0)

    def test_refractory_period_cannot_retrigger_output(self):
        detector = BeatOnsetDetector(warmup_frames=0)
        silence = np.zeros(1024)
        detector(silence)
        pulse = np.sin(2 * np.pi * 70 * np.arange(1024) / SAMPLE_RATE) * 0.4
        previous = detector(pulse)
        self.assertTrue(detector.last_onset)
        for frame in (silence, pulse, silence, pulse):
            level = detector(frame)
            self.assertFalse(detector.last_onset)
            self.assertLess(level, previous)
            previous = level

    def test_spectrum_preserves_absolute_loudness(self):
        wave = np.sin(2 * np.pi * 440 * np.arange(1024) / SAMPLE_RATE)
        quiet, loud = spectrum(wave * 0.002), spectrum(wave * 0.2)
        self.assertGreater(float(loud.max() - quiet.max()), 0.4)
        self.assertLess(float(quiet.max()), 0.3)
        np.testing.assert_array_equal(spectrum(np.zeros((1024, 2))), np.zeros(32))

    def test_shared_fft_and_invalid_audio(self):
        data = self.kicks()[21000:22024]
        first, second = BeatOnsetDetector(), BeatOnsetDetector()
        magnitude, rms = audio_features(data)
        self.assertEqual(first(data), second(data, magnitude=magnitude, rms=rms))
        for audio in ([], [float("nan")] * 1024, [float("inf")] * 1024):
            self.assertTrue(np.all(np.isfinite(spectrum(audio))))
            self.assertEqual(BeatOnsetDetector()(audio), 0.0)

    def test_variable_backend_packets_have_short_onset_latency(self):
        audio = self.kicks() + 0.25 * np.sin(2 * np.pi * 440 * self.times)
        for packet_size in (441, 480, 512):
            with self.subTest(packet_size=packet_size):
                buffer, detector = LiveAudioBuffer(), BeatOnsetDetector()
                found = []
                for offset in range(0, len(audio) - packet_size, packet_size):
                    frame = buffer.push(audio[offset:offset + packet_size])
                    if frame is None:
                        continue
                    window, hop = frame
                    detector(window, hop_size=hop)
                    if detector.last_onset:
                        found.append((offset + packet_size) / SAMPLE_RATE)
                    self.assertLessEqual(len(buffer.samples), 1024)
                self.assertEqual(len(found), len(self.attacks))
                latency = np.asarray(found) - self.attacks
                self.assertTrue(np.all((latency >= 0) & (latency < 0.035)), latency)

    def test_lamp_rate_settings_are_bounded_and_default_to_twenty(self):
        self.assertEqual(normalized_settings({})["lamp_update_hz"], 20)
        self.assertEqual(normalized_settings({"lamp_update_hz": 100})["lamp_update_hz"], 30)
        self.assertEqual(normalized_settings({"lamp_update_hz": 0})["lamp_update_hz"], 5)


if __name__ == "__main__":
    unittest.main()
