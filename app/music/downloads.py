"""On-demand public audio search/download, isolated from the GUI process."""
from difflib import SequenceMatcher
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

import soundfile as sf

from music.playlist_import import normalized
from music.storage import atomic_json, check_cancel


def choose_result(entries, track):
    title, artist = normalized(track["title"]), normalized(track.get("artist", ""))
    duration = float(track.get("expected_duration") or track.get("duration") or 0)
    results = []
    for entry in entries:
        if not entry or entry.get("is_live") or entry.get("live_status") == "is_live":
            continue
        label = normalized(entry.get("title") or "")
        channel = normalized(entry.get("channel") or entry.get("uploader") or "")
        title_words = set(title.split())
        overlap = len(title_words & set(label.split())) / max(1, len(title_words))
        similarity = max(SequenceMatcher(None, title, label).ratio(), overlap)
        artist_words = set(artist.split())
        artist_score = len(artist_words & set((label + " " + channel).split())) / max(1, len(artist_words)) if artist else 1
        if similarity < 0.65 or (artist and artist_score < 0.4):
            continue
        unwanted = {"remix", "live", "cover", "nightcore", "slowed", "sped", "karaoke"}
        if (set(label.split()) & unwanted) - set(title.split()):
            continue
        seconds = float(entry.get("duration") or 0)
        difference = abs(seconds - duration) if seconds and duration else 0
        # Copied VK rows sometimes contain a short preview duration.
        if duration >= 60 and seconds and difference > max(45, duration * 0.3):
            continue
        if duration < 60:
            difference = 0
        score = 0.65 * similarity + 0.25 * artist_score + 0.1 * max(0, 1 - difference / max(15, duration * 0.2))
        results.append((score, entry))
    if not results:
        raise ValueError("Не найден подходящий трек. Уточните название или добавьте локальный файл.")
    return max(results, key=lambda item: item[0])[1]


class MusicDownloader:
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
                    errors.seek(0)
                    message = errors.read()[-2000:].decode("utf-8", "replace").strip()
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

    def fetch(self, track, cancel=None, progress=lambda value, stage: None):
        query = track.get("search_query") or " ".join(filter(None, (track.get("artist"), track["title"])))
        direct = track.get("download_url") or ""
        key = hashlib.sha256((direct or normalized(query)).encode("utf-8")).hexdigest()
        directory = self.root / key
        audio, manifest = directory / "audio.wav", directory / "source.json"
        check_cancel(cancel)
        if audio.is_file() and manifest.is_file():
            try:
                info = sf.info(audio)
                if info.frames and info.samplerate == 48000 and info.channels == 2:
                    return json.loads(manifest.read_text(encoding="utf-8")) | {"path": str(audio)}
            except (OSError, ValueError, RuntimeError):
                pass
        self.root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".download-", dir=self.root) as temporary:
            command = [sys.executable, "-m", "yt_dlp", "--ignore-config", "--no-warnings", "--quiet",
                       "--socket-timeout", "10", "--retries", "1", "--extractor-retries", "1", "--no-playlist"]
            if direct:
                result = {"webpage_url": direct, "title": track["title"]}
            else:
                progress(0, "Поиск на YouTube")
                data = json.loads(self._run(command + ["--dump-single-json", "--flat-playlist", "--skip-download",
                                                     "ytsearch5:" + query], temporary, cancel, timeout=90))
                result = choose_result(data.get("entries") or [], track)
            url = result.get("webpage_url") or result.get("url")
            if not url or not url.startswith("https://"):
                raise ValueError("Сервис вернул некорректную ссылку")
            progress(5, "Скачивание аудио")
            self._run(command + ["--format", "bestaudio/best", "--max-filesize", "100M", "--output",
                                 "source.%(ext)s", url], temporary, cancel, timeout=240)
            files = [path for path in Path(temporary).glob("source.*") if path.suffix not in (".part", ".ytdl", ".json")]
            if len(files) != 1:
                raise ValueError("Сервис не предоставил аудиофайл")
            import imageio_ffmpeg
            progress(15, "Подготовка локального аудио")
            converted = Path(temporary) / "audio.wav"
            self._run([imageio_ffmpeg.get_ffmpeg_exe(), "-nostdin", "-hide_banner", "-loglevel", "error",
                       "-y", "-i", str(files[0]), "-vn", "-ac", "2", "-ar", "48000", "-c:a", "pcm_s16le",
                       str(converted)], temporary, cancel, timeout=120)
            info = sf.info(converted)
            if not info.frames:
                raise ValueError("Загружен пустой аудиофайл")
            check_cancel(cancel)
            directory.mkdir(parents=True, exist_ok=True)
            metadata = {"resolved_url": url, "resolved_title": result.get("title", ""), "search_query": query}
            os.replace(converted, audio)
            atomic_json(manifest, metadata)
            return metadata | {"path": str(audio)}
