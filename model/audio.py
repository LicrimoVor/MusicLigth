import numpy as np

from model.color import clamp


SAMPLE_RATE = 44100
FRAME_SIZE = 2205
SPECTRUM_BINS = 32


def spectrum(data, bins=SPECTRUM_BINS):
    y = np.asarray(data, dtype=np.float32)
    if y.ndim > 1:
        y = np.mean(y, axis=1)
    if y.size == 0:
        return np.zeros(bins)

    y = y - np.mean(y)
    window = np.hanning(y.size)
    fft = np.abs(np.fft.rfft(y * window))[1:]
    if fft.size == 0:
        return np.zeros(bins)

    chunks = np.array_split(fft, bins)
    bars = np.array([np.mean(chunk) if len(chunk) else 0.0 for chunk in chunks])
    bars = np.log1p(bars * 80)
    peak = np.max(bars)
    if peak > 0:
        bars = bars / peak
    return np.clip(bars, 0.0, 1.0)


def lamp_level(mode, position, rms_level, bars):
    if mode == "pulse":
        return clamp(rms_level * 1.25)

    if mode == "spectrum" and len(bars):
        index = int(clamp(position[0]) * (len(bars) - 1))
        return clamp(float(bars[index]))

    dist_to_center = ((position[0] - 0.5) ** 2 + (position[1] - 0.5) ** 2) ** 0.5
    return clamp(rms_level * (0.65 + dist_to_center))
