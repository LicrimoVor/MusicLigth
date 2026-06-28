import numpy as np


class RMSPeakLevel:
    def __init__(self, smoothing=0.2, history_size=5):
        self.smoothing = smoothing
        self.prev_level = 0.0
        self.history = []

        self.history_size = history_size

    def __call__(self, data):
        # RMS блока
        rms = np.sqrt(np.mean(np.square(data)))

        # Нормализация (0..1)
        norm = min(rms * 10, 1.0)

        # Экспоненциальное сглаживание
        smoothed = self.prev_level + self.smoothing * (norm - self.prev_level)
        self.prev_level = smoothed

        # История для пиков
        self.history.append(norm)
        if len(self.history) > self.history_size:
            self.history.pop(0)

        peak_boost = max(self.history)
        level = 0.7 * smoothed + 0.3 * peak_boost
        return level
