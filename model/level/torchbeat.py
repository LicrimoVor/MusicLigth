import torch
import librosa
import numpy as np
import time

# Загрузка предобученной модели TorchBeat
model = torch.hub.load("CPJKU/beat_this", "beat_this")


# Функция для предобработки аудио
def preprocess_audio(audio_data, sample_rate=44100):
    # Преобразование в моно
    audio_data = np.mean(audio_data, axis=0)
    # Приведение к нужной частоте дискретизации
    audio_data = librosa.resample(audio_data, orig_sr=sample_rate, target_sr=44100)
    # Нормализация
    audio_data = audio_data / np.max(np.abs(audio_data))
    return torch.tensor(audio_data).unsqueeze(0)


# Функция для детекции битов
def torchbeat_level(audio_data, sample_rate=44100):
    # Предобработка аудио
    audio_tensor = preprocess_audio(audio_data, sample_rate)
    # Прогон через модель
    with torch.no_grad():
        output = model(audio_tensor)
    # Получение временных меток битов
    beat_times = output["beats"].cpu().numpy()
    print(beat_times)
    return beat_times
