"""On-demand public audio search/download, isolated from the GUI process."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

import numpy as np
import soundfile as sf

from music.matching import (audio_format, choose_result, expected_duration, match_score,
                            rank_results, recording_url, search_query, source_name)
from music.playlist_import import normalized
from music.storage import CancelledAnalysis, atomic_json, check_cancel


class MusicDownloader:
    SOURCES = (("YouTube", "ytsearch8:"), ("SoundCloud", "scsearch8:"))

    def __init__(self, root):
        self.root = Path(root) / "downloads"

    def _run(self, args, directory, cancel, timeout=180):
        check_cancel(cancel)
        # File-backed output avoids pipe deadlock and unbounded RAM for subprocess logs.
        with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
            process = subprocess.Popen(args, cwd=directory, stdout=output, stderr=errors,
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            try:
                deadline = time.monotonic() + timeout
                while process.poll() is None:
                    check_cancel(cancel)
                    if time.monotonic() > deadline:
                        raise TimeoutError("Сервис не ответил вовремя. Повторите попытку позже.")
                    if cancel is not None:
                        cancel.wait(0.1)
                    else:
                        time.sleep(0.1)
                check_cancel(cancel)
                if process.returncode:
                    errors.seek(0, 2)
                    errors.seek(max(0, errors.tell() - 2000))
                    message = errors.read().decode("utf-8", "replace").strip()
                    raise RuntimeError("Не удалось загрузить музыку: " + message)
                output.seek(0)
                return output.read().decode("utf-8", "replace")
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(3)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()

    @staticmethod
    def _command():
        return [sys.executable, "-m", "yt_dlp", "--ignore-config", "--no-warnings", "--quiet",
                "--socket-timeout", "12", "--retries", "1", "--extractor-retries", "1", "--no-playlist"]

    def _search_source(self, provider, prefix, query, cancel):
        with tempfile.TemporaryDirectory(prefix=".search-", dir=self.root) as temporary:
            data = json.loads(self._run(self._command() + ["--dump-single-json", "--flat-playlist",
                "--skip-download", prefix + query], temporary, cancel, timeout=40))
            if not isinstance(data, dict):
                raise ValueError("Источник вернул неверный список результатов")
            return [entry | {"provider": provider} for entry in data.get("entries") or [] if isinstance(entry, dict)]

    def search(self, track, cancel=None, progress=lambda value, stage: None):
        self.root.mkdir(parents=True, exist_ok=True)
        query = search_query(track)
        entries, errors = [], []
        progress(0, "Поиск: YouTube и SoundCloud")
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = {pool.submit(self._search_source, name, prefix, query, cancel): name for name, prefix in self.SOURCES}
            for future in as_completed(futures):
                check_cancel(cancel)
                try:
                    entries.extend(future.result())
                except CancelledAnalysis:
                    raise
                except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
                    errors.append(f"{futures[future]}: {exc}")
        ranked = rank_results(entries, track)
        if not ranked:
            detail = ("\n" + "\n".join(errors)) if errors else ""
            raise ValueError("Не найдена подходящая запись на YouTube и SoundCloud. Уточните исполнителя и название." + detail)
        return ranked

    def _inspect(self, candidate, track, cancel, direct=False):
        with tempfile.TemporaryDirectory(prefix=".inspect-", dir=self.root) as temporary:
            info = json.loads(self._run(self._command() + ["--dump-single-json", "--skip-download",
                candidate["webpage_url"]], temporary, cancel, timeout=50))
        if not isinstance(info, dict):
            raise ValueError("Источник вернул неверные метаданные")
        score = candidate.get("match_score", 1.0) if direct else match_score(info, track)
        if score is None or info.get("is_live"):
            raise ValueError("Уточнённые метаданные не совпадают с искомой песней")
        selected = audio_format(info)
        return info | {"webpage_url": candidate["webpage_url"], "provider": candidate.get("provider") or source_name(candidate["webpage_url"]),
                       "match_score": score, "selected_audio": selected,
                       "selection_score": score + 0.12 * selected["quality_score"]}

    def _candidates(self, track, cancel, progress):
        direct = track.get("download_url") if not track.get("search_by_name") else None
        if direct:
            if not direct.startswith("https://"):
                raise ValueError("Нужна HTTPS-ссылка на аудиоисточник")
            ranked = [{"webpage_url": direct, "title": track["title"]}]
        else:
            ranked = self.search(track, cancel, progress)
        progress(3, "Сравнение версий и качества аудио")
        inspected, errors = [], []
        shortlist = ranked[:4]
        for provider, _prefix in self.SOURCES:
            if not any(item.get("provider") == provider for item in shortlist):
                other = next((item for item in ranked if item.get("provider") == provider), None)
                if other:
                    shortlist.append(other)
        for candidate in ranked:
            if len(shortlist) >= 6:
                break
            if candidate not in shortlist:
                shortlist.append(candidate)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = {pool.submit(self._inspect, candidate, track, cancel, bool(direct)): candidate for candidate in shortlist}
            for future in as_completed(futures):
                check_cancel(cancel)
                try:
                    inspected.append(future.result())
                except CancelledAnalysis:
                    raise
                except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
                    errors.append(str(exc))
        if not inspected:
            raise ValueError("Не найдено полноценное аудио подходящего качества.\n" + "\n".join(errors[:3]))
        return sorted(inspected, key=lambda item: (-item["selection_score"], item["webpage_url"]))

    @staticmethod
    def _validate_audio(path, track, candidate, cancel):
        info = sf.info(path)
        duration = info.frames / info.samplerate
        if not info.frames or info.channels != 2 or info.samplerate != 48000:
            raise ValueError("Аудиофайл пуст или не удалось подготовить PCM")
        advertised = float(candidate.get("duration") or 0)
        if advertised and abs(duration - advertised) > max(3, advertised * 0.03):
            raise ValueError("Загружено неполное аудио или превью вместо полной записи")
        wanted = expected_duration(track)
        if wanted >= 60 and abs(duration - wanted) > max(8, wanted * 0.08):
            raise ValueError("Длительность скачанного файла не совпадает с искомой песней")
        square_sum, sample_count, clipped = 0.0, 0, 0
        with sf.SoundFile(path) as source:
            for block in source.blocks(blocksize=131072, dtype="float32", always_2d=True):
                check_cancel(cancel)
                square_sum += float(np.sum(np.asarray(block, dtype=np.float64) ** 2))
                sample_count += block.size
                clipped += int(np.count_nonzero(abs(block) >= 0.9999))
        rms = (square_sum / max(1, sample_count)) ** 0.5
        if not np.isfinite(rms) or rms < 1e-5:
            raise ValueError("В загруженной записи нет пригодного звука")
        if clipped / max(1, sample_count) > 0.2:
            raise ValueError("В записи слишком много перегрузки и искажений")
        return duration

    @contextmanager
    def _download(self, candidate, track, cancel, progress):
        with tempfile.TemporaryDirectory(prefix=".download-", dir=self.root) as temporary:
            selected = candidate["selected_audio"]
            quality = selected["acodec"] + (f" · {selected['abr']:.0f} кбит/с" if selected["abr"] else "")
            progress(8, f"Загрузка: {candidate['provider']} · {quality}")
            self._run(self._command() + ["--format", str(selected["format_id"]), "--max-filesize", "100M",
                "--output", "source.%(ext)s", candidate["webpage_url"]], temporary, cancel, timeout=240)
            files = [path for path in Path(temporary).glob("source.*") if path.suffix not in (".part", ".ytdl", ".json")]
            if len(files) != 1:
                raise ValueError("Сервис не предоставил аудиофайл")
            import imageio_ffmpeg
            progress(15, "Проверка загруженного звука")
            converted = Path(temporary) / "audio.wav"
            self._run([imageio_ffmpeg.get_ffmpeg_exe(), "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(files[0]), "-vn", "-ac", "2", "-ar", "48000", "-c:a", "pcm_s16le", str(converted)],
                temporary, cancel, timeout=120)
            duration = self._validate_audio(converted, track, candidate, cancel)
            check_cancel(cancel)
            # The caller moves the validated file atomically before this context exits.
            yield converted, duration

    def fetch(self, track, cancel=None, progress=lambda value, stage: None):
        query = search_query(track)
        direct = track.get("download_url") if not track.get("search_by_name") else None
        identity = direct or normalized(query)
        if track.get("download_revision"):
            identity += ":revision:" + str(track["download_revision"])
        directory = self.root / hashlib.sha256(identity.encode("utf-8")).hexdigest()
        audio, manifest = directory / "audio.wav", directory / "source.json"
        check_cancel(cancel)
        if audio.is_file() and manifest.is_file():
            try:
                info = sf.info(audio)
                metadata = json.loads(manifest.read_text(encoding="utf-8"))
                excluded = {recording_url({"url": value}) for value in track.get("excluded_urls", [])}
                if (info.frames and info.samplerate == 48000 and info.channels == 2 and isinstance(metadata, dict)
                        and recording_url({"url": metadata.get("resolved_url", "")}) not in excluded):
                    return metadata | {"path": str(audio)}
            except (OSError, ValueError, RuntimeError):
                pass
        self.root.mkdir(parents=True, exist_ok=True)
        candidates = self._candidates(track, cancel, progress)
        errors = []
        for candidate in candidates[:4]:
            check_cancel(cancel)
            try:
                with self._download(candidate, track, cancel, progress) as (converted, duration):
                    selected = candidate["selected_audio"]
                    metadata = {"selection_version": 2, "resolved_url": candidate["webpage_url"],
                        "resolved_title": candidate.get("title", ""), "resolved_source": candidate["provider"],
                        "source_codec": selected["acodec"], "source_bitrate_kbps": selected["abr"],
                        "source_sample_rate": selected.get("asr") or 0, "source_duration": duration, "search_query": query}
                    directory.mkdir(parents=True, exist_ok=True)
                    os.replace(converted, audio)
                    atomic_json(manifest, metadata)
                    return metadata | {"path": str(audio)}
            except CancelledAnalysis:
                raise
            except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
                errors.append(f"{candidate['provider']}: {exc}")
                progress(6, "Запись отклонена — пробую следующий источник")
        raise ValueError("Не удалось получить подходящую запись.\n" + "\n".join(errors))
