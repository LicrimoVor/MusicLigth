import math
from pathlib import Path
import sys
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from model.level.beat_response import BeatEnvelope, smooth_lamp_level


class BeatResponseTests(unittest.TestCase):
    def test_attack_starts_immediately_and_rises_without_a_flash(self):
        envelope = BeatEnvelope()
        dt = 1 / 60
        levels = [envelope.update(0.8, dt, onset=True)]
        self.assertGreater(levels[0], 0)
        self.assertLess(levels[0], 0.04)
        for _ in range(20):
            levels.append(envelope.update(0, dt, onset=False))
        self.assertGreater(max(levels), 0.35)
        changes = np.diff(levels)
        self.assertTrue(np.all(changes <= 0.035))
        self.assertTrue(np.all(changes >= -0.015))

    def test_dense_rolls_do_not_retrigger_every_frame(self):
        envelope = BeatEnvelope()
        attacks = []
        levels = []
        for step in range(120):
            levels.append(envelope.update(0.8, 0.01, onset=True))
            if envelope.accepted_onset:
                attacks.append(step * 0.01)
        self.assertLessEqual(len(attacks), 5)
        self.assertTrue(np.all(np.diff(attacks) >= 0.235))
        self.assertLess(np.ptp(levels[70:]), 0.08)

    def test_weak_attacks_between_strong_beats_do_not_extend_the_hold(self):
        with_hats, silent = BeatEnvelope(), BeatEnvelope()
        with_hats.update(0.8, 1 / 60, onset=True)
        silent.update(0.8, 1 / 60, onset=True)
        levels, control = [], []
        for step in range(90):
            hat = step % 6 == 0
            levels.append(with_hats.update(0.12 if hat else 0, 1 / 60, onset=hat))
            control.append(silent.update(0, 1 / 60, onset=False))
            self.assertFalse(with_hats.accepted_onset)
        np.testing.assert_allclose(levels, control)
        self.assertLess(levels[-1], 0.3)

    def test_gate_does_not_toggle_around_the_threshold(self):
        envelope = BeatEnvelope()
        for _ in range(20):
            self.assertEqual(envelope.update(0.29, 0.016, onset=True, threshold=0.3), 0)
        envelope.update(0.35, 0.016, onset=True, threshold=0.3)
        self.assertTrue(envelope.gate_open)
        for strength in (0.29, 0.31, 0.27, 0.3):
            envelope.update(strength, 0.016, onset=False, threshold=0.3)
            self.assertTrue(envelope.gate_open)
        envelope.update(0.15, 0.016, onset=False, threshold=0.3)
        self.assertFalse(envelope.gate_open)

    def test_regular_beats_keep_rhythm_with_much_smaller_color_swings(self):
        envelope = BeatEnvelope()
        dt, previous = 1 / 60, 0.0
        raw_levels, lamp_levels = [], []
        for step in range(600):
            phase = step % 30
            raw = 0.8 * math.exp(-phase * dt / 0.15)
            level = envelope.update(raw, dt, onset=phase == 0)
            previous = smooth_lamp_level(previous, level, dt)
            raw_levels.append(raw)
            lamp_levels.append(previous)
        raw = np.asarray(raw_levels)[60:]
        lamps = np.asarray(lamp_levels)[60:]
        self.assertLess(np.ptp(lamps), 0.2)
        self.assertGreater(np.ptp(lamps), 0.03)
        self.assertLess(abs(np.diff(lamps)).sum(), abs(np.diff(raw)).sum() * 0.3)

    def test_tiny_input_reversals_do_not_jitter_the_palette(self):
        level = 0.5
        for step in range(240):
            target = 0.5 + (0.012 if step % 2 else -0.012)
            level = smooth_lamp_level(level, target, 1 / 60)
            self.assertEqual(level, 0.5)

    def test_spectrum_and_delayed_frames_also_have_bounded_changes(self):
        level = 0.5
        for step in range(240):
            before = level
            level = smooth_lamp_level(level, step % 2, 1 / 60, spectrum=True)
            self.assertLess(abs(level - before), 0.04)
        self.assertLess(smooth_lamp_level(0, 1, 0.8), 0.1)
        self.assertGreater(smooth_lamp_level(1, 0, 0.8), 0.95)

    def test_silence_fades_to_zero_and_reset_clears_the_envelope(self):
        envelope = BeatEnvelope()
        envelope.update(0.8, 0.016, onset=True)
        for _ in range(600):
            envelope.update(0, 1 / 60, onset=False)
        self.assertEqual(envelope.level, 0)
        envelope.update(0.8, 0.016, onset=True)
        envelope.reset()
        self.assertEqual(envelope.update(0, 0.016, onset=False), 0)
        self.assertFalse(envelope.gate_open)

    def test_output_is_consistent_at_twenty_and_sixty_updates_per_second(self):
        output = []
        for rate in (20, 60):
            envelope = BeatEnvelope()
            levels = []
            for step in range(rate * 3):
                phase = step % (rate // 2)
                raw = 0.8 * math.exp(-(phase / rate) / 0.15)
                levels.append(envelope.update(raw, 1 / rate, onset=phase == 0))
            output.append(np.asarray(levels)[rate:][rate // 20 - 1::rate // 20])
        np.testing.assert_allclose(output[0], output[1], atol=0.055)


if __name__ == "__main__":
    unittest.main()
