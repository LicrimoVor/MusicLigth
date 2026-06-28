import numpy as np
from collections import deque


class LevelMeter:
    def __init__(self, smoothing=0.2, history_size=5):
        self.smoothing = smoothing
        self.prev_level = 0.0
        self.history = deque(maxlen=history_size)

    def rms_level(self, data):
        # RMS (громкость блока)
        rms = np.sqrt(np.mean(np.square(data)))

        # Перевод в dB (от -∞ до 0)
        db = 20 * np.log10(rms + 1e-6)

        # Нормализация в 0..1 (допустим, -60 dB = 0, 0 dB = 1)
        norm = (db + 60) / 60
        norm = np.clip(norm, 0.0, 1.0)

        # --- сглаживание (экспоненциальное)
        smoothed = self.prev_level + self.smoothing * (norm - self.prev_level)
        self.prev_level = smoothed

        # --- дополнительно можно брать "пик" из последних значений
        self.history.append(norm)
        peak_boost = max(self.history)  # выделение ударных
        level = 0.7 * smoothed + 0.3 * peak_boost

        return level
