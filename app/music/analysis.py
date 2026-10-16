import json
import math
import os
from pathlib import Path
import shutil
import uuid

import numpy as np
from scipy import ndimage, signal
import soundfile as sf
import soxr

from model.audio import spectrum_from_magnitude
from music.metadata import read_metadata
from music.storage import atomic_json, check_cancel, content_id


ANALYSIS_VERSION = 3
FEATURE_NAMES = ("time", "rms", "bass", "mid", "treble", "flux", "loudness",
                 "intensity", "trend", "spectral_change")
SPECTRUM_START = len(FEATURE_NAMES)
FEATURE_COUNT = SPECTRUM_START + 32


def remove_work_directory(path, root):
    path, root = Path(path).resolve(), Path(root).resolve()
    if not path.is_relative_to(root) or not path.name.startswith(".work-"):
        raise ValueError("Refusing to remove a directory outside the cache workspace")
    shutil.rmtree(path, ignore_errors=True)


def normalise(values, low=5, high=98):
    values = np.asarray(values, dtype=np.float32)
    minimum, maximum = np.percentile(values, (low, high))
    if maximum - minimum < 1e-9:
        return np.zeros_like(values)
    return np.clip((values - minimum) / (maximum - minimum), 0, 1).astype(np.float32)


def estimate_tempo(novelty, frame_rate):
    """Autocorrelation supplies a tempo prior, not a list of bass peaks."""
    count = min(len(novelty), int(frame_rate * 120))
    if count < frame_rate * 2 or not np.any(novelty):
        return 0.0, 0.0
    # Select an informative region instead of assuming the intro contains drums.
    if len(novelty) > count:
        energy = np.cumsum(np.r_[0, np.asarray(novelty, dtype=np.float64) ** 2])
        start = int(np.argmax(energy[count:] - energy[:-count]))
        values = novelty[start:start + count]
    else:
        values = novelty
    values = np.asarray(values, dtype=np.float64)
    values = values - values.mean()
    correlation = signal.correlate(values, values, mode="full", method="fft")[len(values) - 1:]
    if correlation[0] < 1e-9:
        return 0.0, 0.0
    correlation /= correlation[0]
    lo, hi = max(1, int(frame_rate * 60 / 200)), min(len(correlation), int(frame_rate * 60 / 60) + 1)
    peaks, _ = signal.find_peaks(correlation[lo:hi])
    if not len(peaks):
        return 0.0, 0.0
    lags = peaks + lo
    # A mild 120 BPM prior resolves octave ties without inventing beats.
    bpms = 60 * frame_rate / lags
    scores = correlation[lags] * np.exp(-0.15 * np.log2(bpms / 120) ** 2)
    selected = int(np.argmax(scores))
    lag = float(lags[selected])
    index = int(lag)
    if 0 < index < len(correlation) - 1:
        a, b, c = correlation[index - 1:index + 2]
        denominator = a - 2 * b + c
        if abs(denominator) > 1e-9:
            lag += float(np.clip(0.5 * (a - c) / denominator, -0.5, 0.5))
    return float(60 * frame_rate / lag), float(np.clip(correlation[index], 0, 1))


class AnalysisData:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.metadata = json.loads((self.directory / "analysis.json").read_text(encoding="utf-8"))
        if self.metadata.get("version") != ANALYSIS_VERSION or not self.metadata.get("complete"):
            raise ValueError("Устаревший или неполный кэш анализа")
        self.features = np.load(self.directory / "features.npy", mmap_mode="r", allow_pickle=False)
        if self.features.ndim != 2 or self.features.shape[1] != FEATURE_COUNT or not len(self.features):
            raise ValueError("Повреждённый кэш признаков")
        self.beats = np.load(self.directory / "beats.npy", mmap_mode="r", allow_pickle=False)
        self.onsets = np.load(self.directory / "onsets.npy", mmap_mode="r", allow_pickle=False)

    @property
    def duration(self):
        return self.metadata["duration"]

    def frame_at(self, seconds):
        step = self.metadata["hop"] / self.metadata["samplerate"]
        offset = self.metadata["window_size"] / (2 * self.metadata["samplerate"])
        index = math.floor((seconds - offset) / step + 1e-7)
        return np.asarray(self.features[max(0, min(index, len(self.features) - 1))])

    def close(self):
        for value in (self.features, self.beats, self.onsets):
            mapping = getattr(value, "_mmap", None)
            if mapping is not None:
                mapping.close()


class MusicAnalyser:
    def __init__(self, root):
        self.root = Path(root)
        self.cache_root = self.root / "analysis"
        self.audio_root = self.root / "audio"
        self.cache_root.mkdir(parents=True, exist_ok=True)
        self.audio_root.mkdir(parents=True, exist_ok=True)

    def cache_directory(self, identifier):
        return self.cache_root / f"{identifier}-v{ANALYSIS_VERSION}"

    def analyse(self, path, cancel=None, progress=lambda value, stage: None):
        path = Path(path)
        progress(0, "Проверка файла")
        before = path.stat()
        identifier = content_id(path, cancel)
        check_cancel(cancel)
        directory = self.cache_directory(identifier)
        if (directory / "analysis.json").exists():
            try:
                cached = AnalysisData(directory)
                metadata = dict(cached.metadata)
                cached.close()
                if Path(metadata["playback_path"]).is_file():
                    if not metadata.get("title_from_tags"):
                        metadata["title"] = path.stem
                    progress(100, "Кэш готов")
                    return metadata | {"cache_path": str(directory), "cache_hit": True}
            except (OSError, ValueError, KeyError):
                pass

        work = self.cache_root / (".work-" + uuid.uuid4().hex)
        work.mkdir()
        wave_path = self.audio_root / (identifier + ".wav")
        wave_work = self.audio_root / (".work-" + uuid.uuid4().hex + ".wav")
        features = None
        try:
            tags, cover = read_metadata(path)
            with sf.SoundFile(path) as source:
                if source.frames <= 0 or source.samplerate < 8000:
                    raise ValueError("Пустой файл или неподдерживаемая частота дискретизации")
                samplerate, samples = source.samplerate, source.frames
                hop = max(1, round(samplerate / 50))
                window_size = 2 ** int(round(math.log2(samplerate * 0.046)))
                count = math.ceil(samples / hop)
                features = np.lib.format.open_memmap(work / "features.npy", mode="w+", dtype="float32",
                                                    shape=(count, FEATURE_COUNT))
                window = np.hanning(window_size).astype(np.float32)
                scale = 2 / max(window.sum(), 1)
                frequencies = np.fft.rfftfreq(window_size, 1 / samplerate)
                bands = [(frequencies >= low) & (frequencies < high)
                         for low, high in ((35, 180), (180, 2500), (2500, min(16000, samplerate / 2)))]
                channels, playback_rate = 2, 48000
                resampler = soxr.ResampleStream(samplerate, playback_rate, channels, dtype="float32")
                large_wave = samples / samplerate * playback_rate * channels * 2 > 4_000_000_000
                with sf.SoundFile(wave_work, "w", playback_rate, channels, subtype="PCM_16",
                                  format="RF64" if large_wave else "WAV") as playback:
                    pending = np.zeros(0, dtype=np.float32)
                    frame_index, frame_start, read_count = 0, 0, 0
                    previous = None

                    def write_frame(frame):
                        nonlocal frame_index, frame_start, previous
                        frame = frame - frame.mean()
                        rms = float(np.sqrt(np.sum(frame * frame * window) / window.sum()))
                        magnitude = np.abs(np.fft.rfft(frame * window)) * scale
                        logarithm = np.log1p(magnitude * 100)
                        flux = float(np.maximum(logarithm - previous, 0).mean()) if previous is not None else 0.0
                        previous = logarithm
                        energy = [float(np.sqrt(np.sum(magnitude[mask] ** 2))) for mask in bands]
                        features[frame_index, :SPECTRUM_START] = (
                            min(samples / samplerate, (frame_start + window_size / 2) / samplerate),
                            rms, *energy, flux, 0, 0, 0, 0)
                        features[frame_index, SPECTRUM_START:] = spectrum_from_magnitude(
                            magnitude, window_size, samplerate=samplerate)
                        frame_index += 1
                        frame_start += hop

                    for block in source.blocks(blocksize=65536, dtype="float32", always_2d=True):
                        check_cancel(cancel)
                        block = np.nan_to_num(block)
                        audio = block if source.channels == 2 else np.repeat(block.mean(axis=1)[:, None], 2, axis=1)
                        playback.write(resampler.resample_chunk(audio, last=read_count + len(block) >= samples))
                        read_count += len(block)
                        pending = np.concatenate((pending, block.mean(axis=1)))
                        offset = 0
                        while len(pending) - offset >= window_size and frame_index < count:
                            write_frame(pending[offset:offset + window_size])
                            offset += hop
                        pending = pending[offset:]
                        progress(int(70 * read_count / samples), "Частоты и динамика")
                    while frame_index < count:
                        check_cancel(cancel)
                        write_frame(np.pad(pending[:window_size], (0, max(0, window_size - len(pending)))))
                        pending = pending[min(hop, len(pending)):]

                check_cancel(cancel)
                progress(72, "Нормализация")
                rms = np.asarray(features[:, 1]).copy()
                reference = max(float(np.percentile(rms, 98)) * 2.0, 1e-8)
                for column in (2, 3, 4):
                    # All bands share a gain reference. Tiny treble leakage must
                    # not be stretched to the same strength as a real bass drum.
                    features[:, column] = np.clip(features[:, column] / reference, 0, 1)
                db = 20 * np.log10(np.maximum(rms, 1e-9))
                features[:, 6] = normalise(db, 5, 98)
                frame_rate = samplerate / hop
                intensity = ndimage.gaussian_filter1d(features[:, 6], frame_rate * 0.3)
                features[:, 7] = intensity
                slow = ndimage.gaussian_filter1d(intensity, frame_rate)
                features[:, 8] = np.gradient(slow) * frame_rate if len(slow) > 1 else 0
                flux = np.asarray(features[:, 5]).copy()
                median = float(np.median(flux))
                spread = float(np.median(abs(flux - median))) * 1.4826
                novelty = normalise(np.maximum(0, flux - median - 1.5 * spread), 0, 99)
                novelty[rms < 1e-6] = 0
                # A stable sinusoid is not a rhythm despite FFT phase fluctuations.
                valid_rms = rms[:max(1, count - math.ceil(window_size / hop))]
                if valid_rms.mean() > 0 and valid_rms.std() / valid_rms.mean() < 0.02:
                    novelty[:] = 0
                features[:, 5] = novelty
                features[:, 9] = normalise(flux, 5, 99)
                progress(78, "Доли и начала звуков")
                check_cancel(cancel)
                import librosa
                onsets = librosa.onset.onset_detect(onset_envelope=novelty, sr=samplerate,
                                                    hop_length=hop, units="frames", normalize=False)
                bpm, confidence = estimate_tempo(novelty, frame_rate)
                if confidence >= 0.18 and len(onsets) >= 3:
                    _, beat_frames = librosa.beat.beat_track(onset_envelope=novelty, sr=samplerate,
                                                           hop_length=hop, bpm=bpm, trim=True)
                else:
                    bpm, confidence, beat_frames = 0.0, 0.0, np.zeros(0, dtype=int)
                check_cancel(cancel)
                offset = window_size / (2 * samplerate)
                beats = np.asarray(beat_frames, dtype=float) * hop / samplerate + offset
                onset_times = np.asarray(onsets, dtype=float) * hop / samplerate + offset
                duration = samples / samplerate
                beats, onset_times = beats[beats < duration], onset_times[onset_times < duration]
                np.save(work / "beats.npy", beats.astype("float64"), allow_pickle=False)
                np.save(work / "onsets.npy", onset_times.astype("float64"), allow_pickle=False)
                progress(93, "Структура композиции")
                sections, transitions = self._structure(slow, frame_rate, duration)
                beat_indices = np.minimum(np.asarray(beat_frames, dtype=int), count - 1)
                strong = []
                if confidence >= 0.4 and len(beat_indices) > 4:
                    strengths = intensity[beat_indices]
                    strong = beats[:len(strengths)][strengths[:len(beats)] > np.median(strengths) + 0.12].tolist()
                metadata = {"version": ANALYSIS_VERSION, "complete": True, "content_id": identifier,
                            "duration": duration, "samplerate": samplerate, "channels": source.channels,
                            "hop": hop, "window_size": window_size, "frame_count": count,
                            "bpm": bpm, "tempo_confidence": confidence, "strong_beats": strong,
                            "bars": [], "sections": sections, "transitions": transitions,
                            "energy_peaks": features[signal.find_peaks(intensity, height=0.7,
                                distance=max(1, int(frame_rate)))[0], 0].tolist(),
                            "playback_path": str(wave_path), "title": tags["title"],
                            "title_from_tags": tags["title_from_tags"],
                            "artist": tags["artist"], "album": tags["album"],
                            "cover_path": str(directory / "cover.bin") if cover else ""}
                if cover:
                    (work / "cover.bin").write_bytes(cover)
                features.flush()
                features._mmap.close()
                features = None
                after = path.stat()
                if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    raise ValueError("Файл изменился во время анализа; добавьте его повторно")
                check_cancel(cancel)
                atomic_json(work / "analysis.json", metadata)
                os.replace(wave_work, wave_path)
                if directory.exists():
                    directory.rename(directory.with_name(directory.name + ".invalid-" + uuid.uuid4().hex))
                os.replace(work, directory)
                progress(100, "Готово")
                return metadata | {"cache_path": str(directory), "cache_hit": False}
        finally:
            if features is not None:
                features._mmap.close()
            wave_work.unlink(missing_ok=True)
            if work.exists():
                remove_work_directory(work, self.cache_root)

    @staticmethod
    def _structure(energy, frame_rate, duration):
        sections, transitions = [], []
        labels = np.where(energy < 0.3, "quiet", np.where(energy > 0.67, "intense", "steady"))
        starts = np.r_[0, np.flatnonzero(labels[1:] != labels[:-1]) + 1]
        for start, end in zip(starts, np.r_[starts[1:], len(labels)]):
            if (end - start) / frame_rate >= 2:
                sections.append({"start": float(start / frame_rate), "end": min(duration, float(end / frame_rate)),
                                 "type": str(labels[start])})
        lag = max(1, int(frame_rate * 2))
        if len(energy) > lag:
            rise = energy[lag:] - energy[:-lag]
            peaks, _ = signal.find_peaks(rise, height=0.3, distance=max(1, int(frame_rate * 6)))
            for index in peaks:
                when = float((index + lag / 2) / frame_rate)
                transitions.append({"time": max(0, when - 2), "type": "build_up", "strength": float(rise[index]), "duration": 2.0})
                transitions.append({"time": when, "type": "drop", "strength": float(rise[index]), "duration": 0.5})
        return sections, transitions
