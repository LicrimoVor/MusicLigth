from collections import deque

import numpy as np


class BeatOnsetDetector:
    """Adaptive onset detector for real-time beat-like impulses.

    The detector is stateful: call it once per audio frame. It combines spectral
    flux, low-frequency energy changes and RMS gating, then adapts its threshold
    to the recent history. The return value is a visual control level in 0..1.
    """

    def __init__(
        self,
        samplerate=44100,
        history_size=48,
        min_rms=0.006,
        decay=0.72,
        cooldown_frames=2,
        warmup_frames=6,
    ):
        self.samplerate = samplerate
        self.history = deque(maxlen=history_size)
        self.prev_mag = None
        self.prev_low_energy = 0.0
        self.prev_mid_energy = 0.0
        self.energy_peak = 1e-6
        self.output = 0.0
        self.cooldown = 0
        self.frames_seen = 0
        self.min_rms = min_rms
        self.decay = decay
        self.cooldown_frames = cooldown_frames
        self.warmup_frames = warmup_frames

    def reset(self):
        self.history.clear()
        self.prev_mag = None
        self.prev_low_energy = 0.0
        self.prev_mid_energy = 0.0
        self.energy_peak = 1e-6
        self.output = 0.0
        self.cooldown = 0
        self.frames_seen = 0

    def __call__(self, data):
        y = np.asarray(data, dtype=np.float32)
        if y.ndim > 1:
            y = np.mean(y, axis=1)
        if y.size < 16:
            return 0.0

        y = y - np.mean(y)
        rms = float(np.sqrt(np.mean(np.square(y))))
        self.frames_seen += 1

        window = np.hanning(y.size).astype(np.float32)
        mag = np.abs(np.fft.rfft(y * window))
        freqs = np.fft.rfftfreq(y.size, 1.0 / self.samplerate)
        total_energy = float(np.sum(mag) + 1e-9)

        low_energy = self._band_energy(mag, freqs, 35, 180)
        mid_energy = self._band_energy(mag, freqs, 180, 4500)

        if rms < self.min_rms:
            self.history.append(0.0)
            self.prev_mag = mag
            self.prev_low_energy = low_energy
            self.prev_mid_energy = mid_energy
            self.output *= self.decay
            self._tick_cooldown()
            return float(np.clip(self.output, 0.0, 1.0))

        if self.prev_mag is None or self.prev_mag.shape != mag.shape:
            flux = 0.0
            low_jump = 0.0
            mid_jump = 0.0
        else:
            diff = mag - self.prev_mag
            flux = float(np.sum(diff[diff > 0]) / total_energy)
            low_jump = max(0.0, low_energy - self.prev_low_energy) / max(low_energy, self.prev_low_energy, 1e-9)
            mid_jump = max(0.0, mid_energy - self.prev_mid_energy) / max(mid_energy, self.prev_mid_energy, 1e-9)
        rms_norm = self._rms_level(rms)

        novelty = (0.55 * flux) + (0.30 * low_jump) + (0.15 * mid_jump)
        novelty *= 0.65 + 0.70 * rms_norm
        novelty = float(np.clip(novelty, 0.0, 4.0))

        self.history.append(novelty)
        if self.frames_seen <= self.warmup_frames:
            self.prev_mag = mag
            self.prev_low_energy = low_energy
            self.prev_mid_energy = mid_energy
            return 0.0

        baseline, spread = self._adaptive_stats()
        onset = max(0.0, novelty - baseline)
        onset_score = onset / max(spread * 1.6, 0.035)
        onset_score = float(np.clip(onset_score, 0.0, 1.0))

        is_onset = onset_score > 0.28 and self.cooldown == 0
        if is_onset:
            self.output = max(self.output, onset_score)
            self.cooldown = self.cooldown_frames
        else:
            self.output *= self.decay
            if onset_score > self.output:
                self.output = 0.35 * self.output + 0.65 * onset_score
            self._tick_cooldown()

        self.prev_mag = mag
        self.prev_low_energy = low_energy
        self.prev_mid_energy = mid_energy
        return float(np.clip(self.output, 0.0, 1.0))

    def _adaptive_stats(self):
        if len(self.history) < 8:
            return 0.0, 0.12

        values = np.asarray(self.history, dtype=np.float32)
        baseline = float(np.median(values))
        high = float(np.percentile(values, 88))
        spread = max(high - baseline, float(np.std(values)), 0.025)
        return baseline, spread

    def _rms_level(self, rms):
        self.energy_peak = max(self.energy_peak * 0.995, rms, 1e-6)
        return float(np.clip(rms / self.energy_peak, 0.0, 1.0))

    def _tick_cooldown(self):
        if self.cooldown > 0:
            self.cooldown -= 1

    @staticmethod
    def _band_energy(mag, freqs, low, high):
        mask = (freqs >= low) & (freqs < high)
        if not np.any(mask):
            return 0.0
        return float(np.sum(mag[mask]))
