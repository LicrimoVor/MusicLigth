from collections import deque
from functools import lru_cache
import math

import numpy as np

from model.audio import audio_features


@lru_cache(maxsize=8)
def frequency_bands(size, samplerate):
    frequencies = np.fft.rfftfreq(size, 1.0 / samplerate)
    return tuple((frequencies >= low) & (frequencies < high)
                 for low, high in ((35, 180), (180, 2000), (2000, 10000)))


class BeatOnsetDetector:
    """Adaptive percussive onset detector with an audio-time refractory period.

    Returns a decaying pulse in 0..1. ``last_onset`` marks actual attacks;
    sustained tones and the refractory period cannot retrigger the envelope.
    """

    def __init__(self, samplerate=44100, history_size=96, min_rms=0.0015,
                 decay=0.72, cooldown_frames=3, warmup_frames=3):
        self.samplerate = samplerate
        self.history = deque(maxlen=history_size)
        self.band_history = deque(maxlen=history_size)
        self.min_rms = min_rms
        # Keep the old options in units of the former 50 ms frames.
        self.release_seconds = -0.05 / math.log(max(0.01, min(0.99, decay)))
        self.refractory_seconds = max(0.10, cooldown_frames * 0.05)
        self.warmup_seconds = warmup_frames * 0.05
        self.reset()

    def reset(self):
        self.history.clear()
        self.band_history.clear()
        self.prev_mag = None
        self.prev_rms = 0.0
        self.prev_band_energy = np.zeros(3)
        self.output = 0.0
        self.cooldown = 0.0
        self.elapsed = 0.0
        self.last_onset = False

    def __call__(self, data, *, magnitude=None, rms=None, hop_size=None):
        size = np.asarray(data).shape[0] if np.asarray(data).ndim else 0
        self.last_onset = False
        if size < 16:
            return 0.0
        if magnitude is None or rms is None:
            magnitude, rms = audio_features(data)
        dt = (size if hop_size is None else hop_size) / self.samplerate
        self.elapsed += dt
        self.cooldown = max(0.0, self.cooldown - dt)
        self.output *= math.exp(-dt / self.release_seconds)

        bands = frequency_bands(size, self.samplerate)
        energies = np.array([float(np.sum(magnitude[mask])) for mask in bands])
        novelty = 0.0
        attack = False
        if self.prev_mag is not None and self.prev_mag.shape == magnitude.shape:
            # Normalise each band independently so bass is not buried by vocals.
            positive = np.maximum(magnitude - self.prev_mag, 0.0)
            flux = np.array([float(np.sum(positive[mask])) for mask in bands])
            flux /= np.maximum(np.maximum(energies, self.prev_band_energy), 1e-6)
            energy_jump = np.maximum(energies - self.prev_band_energy, 0.0)
            energy_jump /= np.maximum(np.maximum(energies, self.prev_band_energy), 1e-6)
            rms_jump = max(0.0, rms - self.prev_rms) / max(rms, self.prev_rms, 1e-6)
            novelty = float(np.dot(0.65 * flux + 0.35 * energy_jump, (0.55, 0.30, 0.15)))
            # Phase changes in stationary low tones must not count as attacks.
            novelty *= min(1.0, rms_jump / 0.08)
            attack = rms_jump >= 0.12
            if len(self.band_history) >= 8:
                past = np.asarray(self.band_history)
                band_baseline = np.median(past, axis=0)
                band_spread = 1.4826 * np.median(np.abs(past - band_baseline), axis=0)
                strong_band = (energies > band_baseline + 6.0 * band_spread)
                strong_band &= energies > np.maximum(band_baseline * 3.0, 1e-5)
                attack = attack or bool(np.any(strong_band & (energy_jump > 0.55)))

        if rms < self.min_rms:
            novelty = 0.0
        baseline, spread = self._adaptive_stats()
        threshold = max(0.12, baseline + max(0.08, 2.5 * spread))
        if (self.elapsed >= self.warmup_seconds and self.cooldown == 0.0
                and attack and rms >= self.min_rms and novelty > threshold):
            # Soft compression preserves differences between attacks instead of
            # clipping almost every percussive transient to a full-scale flash.
            strength = -math.expm1(-(novelty - baseline) / max(0.55, threshold - baseline))
            self.output = max(self.output, strength)
            self.cooldown = self.refractory_seconds
            self.last_onset = True

        # Compare to past frames before adding this attack to the baseline.
        self.history.append(novelty)
        self.band_history.append(energies)
        self.prev_mag = magnitude
        self.prev_rms = rms
        self.prev_band_energy = energies
        return float(np.clip(self.output, 0.0, 1.0))

    def _adaptive_stats(self):
        if len(self.history) < 8:
            return 0.0, 0.0
        values = np.asarray(self.history)
        baseline = float(np.median(values))
        spread = float(np.median(np.abs(values - baseline))) * 1.4826
        return baseline, spread
