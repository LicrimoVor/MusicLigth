from functools import lru_cache

import numpy as np

from model.color import clamp


SAMPLE_RATE = 44100
FRAME_SIZE = 1024
SPECTRUM_BINS = 32


def mono_audio(data):
    y = np.asarray(data, dtype=np.float32)
    if y.ndim > 1:
        y = np.mean(y, axis=1)
    return np.nan_to_num(y, nan=0.0, posinf=0.0, neginf=0.0).reshape(-1)


@lru_cache(maxsize=8)
def fft_window(size):
    return np.hanning(size).astype(np.float32)


def audio_features(data):
    """One windowed FFT shared by the spectrum and onset detector."""
    y = mono_audio(data)
    if y.size < 16:
        return np.zeros(0), 0.0

    y = y - np.mean(y)
    rms = float(np.sqrt(np.mean(y * y)))
    window = fft_window(y.size)
    magnitude = np.abs(np.fft.rfft(y * window)) * (2.0 / max(float(window.sum()), 1.0))
    return magnitude, rms


@lru_cache(maxsize=16)
def spectrum_ranges(size, bins, samplerate):
    frequencies = np.fft.rfftfreq(size, 1.0 / samplerate)
    edges = np.geomspace(35, min(16000, samplerate / 2), bins + 1)
    ranges = []
    for low, high in zip(edges[:-1], edges[1:]):
        start = max(1, int(np.searchsorted(frequencies, low)))
        end = min(len(frequencies), max(start + 1, int(np.searchsorted(frequencies, high))))
        ranges.append((start, end))
    return tuple(ranges)


def spectrum_from_magnitude(magnitude, size, bins=SPECTRUM_BINS, samplerate=SAMPLE_RATE):
    if len(magnitude) < 2:
        return np.zeros(bins, dtype=np.float32)

    # Absolute dB scale: quiet input must not become a full-height spectrum.
    ranges = spectrum_ranges(size, bins, samplerate)
    amplitudes = np.array([np.max(magnitude[start:end]) if end > start else 0.0
                           for start, end in ranges])
    db = 20 * np.log10(np.maximum(amplitudes, 1e-9))
    return np.clip((db + 65.0) / 65.0, 0.0, 1.0).astype(np.float32)


def spectrum(data, bins=SPECTRUM_BINS):
    magnitude, _ = audio_features(data)
    size = np.asarray(data).shape[0] if np.asarray(data).ndim else 0
    return spectrum_from_magnitude(magnitude, size, bins)


def lamp_level(mode, position, rms_level, bars):
    if mode == "pulse":
        return clamp(rms_level * 1.25)

    if mode == "spectrum" and len(bars):
        index = int(clamp(position[0]) * (len(bars) - 1))
        return clamp(float(bars[index]))

    dist_to_center = ((position[0] - 0.5) ** 2 + (position[1] - 0.5) ** 2) ** 0.5
    return clamp(rms_level * (0.65 + dist_to_center))
