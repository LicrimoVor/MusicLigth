import numpy as np
import librosa


def spect_level(data, samplerate=44100):
    """
    Вычисляет уровень на основе спектральных изменений (onsets)
    Возвращает 0..1, где пики соответствуют ударам
    """
    # Преобразуем в float32
    y = np.array(data, dtype=np.float32)

    # Если данные моно, то ок. Если стерео, берем среднее
    if y.ndim > 1:
        y = np.mean(y, axis=1)

    # Librosa expects float32 in range [-1,1], нормализуем
    y = y / max(1e-6, np.max(np.abs(y)))

    # Onset envelope
    onset_env = librosa.onset.onset_strength(y=y, sr=samplerate)
    if len(onset_env) == 0:
        return 0.0

    # Берём максимум в текущем окне
    level = float(np.max(onset_env))
    # Нормализуем (0..1)
    level = min(level / 5.0, 1.0)  # 5.0 — эмпирически подбирается
    return level
