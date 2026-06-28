import torch
import torchaudio
from BeatNet.BeatNet import BeatNet


def beatnet_level(data, samplerate=44100):
    """
    Определяет уровень удара (0–1) в аудиоданных с использованием модели BeatNet.

    :param data: Аудиоданные в формате numpy.ndarray (моно, нормализованные в диапазоне [-1, 1]).
    :param samplerate: Частота дискретизации аудиоданных.
    :return: Уровень удара (0–1).
    """
    # Преобразуем numpy-массив в тензор PyTorch
    waveform = torch.tensor(data).float()

    # Преобразуем аудиофайл в спектрограмму
    mel_specgram = torchaudio.transforms.MelSpectrogram(
        sample_rate=samplerate, n_fft=2048, hop_length=512, n_mels=128
    )(waveform)

    # Нормализуем спектрограмму
    mel_specgram = (mel_specgram - mel_specgram.mean()) / mel_specgram.std()

    # Загружаем предобученную модель BeatNet
    model = BeatNet(1, mode="realtime", inference_model="PF", plot=["beat_particles"], thread=False)

    # Прогоняем спектрограмму через модель
    with torch.no_grad():
        beat_probabilities = model(mel_specgram.unsqueeze(0))

    # Получаем вероятность наличия удара в последнем фрейме
    beat_level = beat_probabilities.squeeze()[-1].item()

    return beat_level
