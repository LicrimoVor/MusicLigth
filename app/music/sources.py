"""Import mixed lines of track names and public music links in a worker."""
import hashlib
from html import unescape
import json
from pathlib import Path
import re
import sys
import tempfile
from urllib.parse import urlparse

import requests

from music.downloads import MusicDownloader
from music.playlist_import import PlaylistParser
from music.storage import check_cancel


def classify_line(line):
    line = line.strip()
    if not line:
        return "empty", ""
    if re.match(r"^(?:www\.|m\.)?(?:youtube\.com|youtu\.be|vk\.com|vk\.ru|music\.yandex\.)", line, re.I):
        line = "https://" + line
    parsed = urlparse(line)
    host = (parsed.hostname or "").casefold()
    if parsed.scheme and ("://" in line or parsed.scheme in ("http", "https", "file", "javascript", "data")):
        if parsed.scheme not in ("http", "https"):
            raise ValueError("Поддерживаются ссылки https/http и названия песен")
        if host in ("youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"):
            return "youtube", line
        if host in ("vk.com", "m.vk.com", "www.vk.com", "vk.ru", "m.vk.ru"):
            return "vk", line
        if host in tuple("music.yandex." + tld for tld in ("ru", "com", "kz", "by", "ua", "uz")):
            return "yandex", line
        raise ValueError("Неизвестный сервис: используйте VK, YouTube, Яндекс Музыку или название")
    return "name", line


def name_entry(text):
    artist, title = "", text.strip()
    for separator in (" — ", " – ", " - "):
        if separator in title:
            artist, title = title.split(separator, 1)
            break
    if not title.strip():
        raise ValueError("Пустое название песни")
    return {"title": title.strip(), "artist": artist.strip(), "duration": 0}


class _VKParser(PlaylistParser):
    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        encoded = attributes.get("data-audio")
        if encoded:
            try:
                audio = json.loads(encoded)
                self.entries.append({"title": unescape(re.sub("<[^>]+>", "", str(audio[3]))),
                                     "artist": unescape(re.sub("<[^>]+>", "", str(audio[4]))),
                                     "duration": float(audio[5]),
                                     "source_url": f"https://vk.com/audio{audio[1]}_{audio[0]}"})
            except (ValueError, TypeError, IndexError, KeyError):
                pass
        super().handle_starttag(tag, attrs)


class SourceImporter:
    def __init__(self, root, catalog=None):
        self.downloader = MusicDownloader(root)
        self.library_path = Path(root) / "library.json"
        self.catalog = Path(catalog) if catalog is not None else Path(root).parent / "playlists"

    def _known_vk(self, url):
        identity = re.search(r"/audio(-?\d+_\d+)", urlparse(url).path)
        documents = []
        for path in self.catalog.glob("*.json"):
            try:
                documents.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        if identity and self.library_path.is_file():
            try:
                documents.append({"entries": json.loads(self.library_path.read_text(encoding="utf-8")).get("tracks", [])})
            except (OSError, ValueError):
                pass
        for document in documents:
            if document.get("source") == url and document.get("entries"):
                return document.get("name", "VK"), document["entries"], True
            if identity:
                for entry in document.get("entries", []):
                    other = re.search(r"/audio(-?\d+_\d+)", urlparse(entry.get("source_url", "")).path)
                    if other and identity[1] == other[1]:
                        return "VK", [entry], False
        return None

    def _request(self, url, cancel, data=None):
        check_cancel(cancel)
        with requests.request("POST" if data else "GET", url, data=data, timeout=(5, 12), stream=True,
                              headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json,text/html"}) as response:
            response.raise_for_status()
            blocks, size = [], 0
            for block in response.iter_content(65536):
                check_cancel(cancel)
                size += len(block)
                if size > 16 * 1024 * 1024:
                    raise ValueError("Ответ сервиса слишком большой")
                blocks.append(block)
            return b"".join(blocks).decode("utf-8", "replace")

    def _youtube(self, url, cancel):
        with tempfile.TemporaryDirectory() as temporary:
            data = json.loads(self.downloader._run([sys.executable, "-m", "yt_dlp", "--ignore-config", "--quiet",
                "--no-warnings", "--socket-timeout", "10", "--retries", "1", "--extractor-retries", "1",
                "--dump-single-json", "--flat-playlist", "--skip-download", "--playlist-end", "2000", url],
                temporary, cancel, timeout=120))
        collection = isinstance(data.get("entries"), list)
        entries = []
        for item in data["entries"] if collection else [data]:
            check_cancel(cancel)
            if not item or not item.get("title") or item["title"] in ("[Deleted video]", "[Private video]"):
                continue
            link = item.get("webpage_url") or item.get("url")
            if not link or not link.startswith("http"):
                link = "https://www.youtube.com/watch?v=" + item["id"]
            entries.append({"title": item["title"], "artist": item.get("artist") or item.get("channel") or item.get("uploader") or "",
                            "duration": item.get("duration") or 0, "source_url": link, "download_url": link})
        return data.get("title") or "YouTube", entries, collection

    def _yandex(self, url, cancel):
        parsed = urlparse(url)
        path = parsed.path.rstrip("/")
        match = re.search(r"/track/(\d+)", path)
        base = "https://api.music.yandex.net"
        if match:
            result = json.loads(self._request(base + "/tracks/" + match[1], cancel)).get("result", [])
            tracks, title, collection = result, "Яндекс Музыка", False
        else:
            user = re.fullmatch(r"/users/([^/]+)/playlists/(\d+)", path)
            playlist = re.fullmatch(r"/playlists/([\w-]+)", path)
            album = re.fullmatch(r"/album/(\d+)", path)
            if user:
                endpoint = f"/users/{user[1]}/playlists/{user[2]}"
            elif playlist:
                endpoint = "/playlists/" + playlist[1]
            elif album:
                endpoint = f"/albums/{album[1]}/with-tracks"
            else:
                raise ValueError("Нужна ссылка на трек, альбом или плейлист Яндекс Музыки")
            result = json.loads(self._request(base + endpoint, cancel)).get("result") or {}
            title = result.get("title") or "Яндекс Музыка"
            tracks = [track for volume in result["volumes"] for track in volume] if result.get("volumes") else [item.get("track") or item for item in result.get("tracks", [])]
            collection = True
            missing = [str(track["id"]) for track in tracks if not track.get("title") and track.get("id")]
            resolved = {}
            for offset in range(0, len(missing), 100):
                details = json.loads(self._request(base + "/tracks", cancel, {"track-ids": ",".join(missing[offset:offset + 100])}))
                resolved.update({str(item["id"]): item for item in details.get("result", [])})
            tracks = [track if track.get("title") else resolved.get(str(track.get("id")), {}) for track in tracks]
        entries = [{"title": item["title"], "artist": ", ".join(artist["name"] for artist in item.get("artists", []) if artist.get("name")),
                    "duration": (item.get("durationMs") or 0) / 1000,
                    "source_url": "https://music.yandex.ru/track/" + str(item["id"])}
                   for item in tracks if item.get("title")]
        return title, entries, collection

    def _vk(self, url, cancel):
        known = self._known_vk(url)
        if known:
            return known
        parser = _VKParser()
        parser.feed(self._request(url, cancel))
        parser.close()
        entries = parser.entries
        if not entries:
            raise ValueError("VK не открыл список без авторизации. Вставьте скопированный HTML списка или импортируйте его TXT-файл.")
        return "VK · " + urlparse(url).path.strip("/"), entries, len(entries) > 1 or "playlist" in url

    def resolve(self, text, cancel=None, progress=lambda message: None):
        documents, entries, errors = [], [], []
        if "data-audio" in text and "<" in text:
            parser = _VKParser()
            parser.feed(text)
            parser.close()
            if not parser.entries:
                raise ValueError("В HTML не найдены музыкальные треки")
            return {"documents": [{"version": 1, "name": "Импорт VK", "source": "html:" + hashlib.sha256(text.encode()).hexdigest(),
                                    "entries": parser.entries}], "entries": [], "errors": []}
        for number, line in enumerate(text.splitlines(), 1):
            check_cancel(cancel)
            if not line.strip():
                continue
            try:
                kind, value = classify_line(line)
                progress(f"Импорт, строка {number}: {kind}")
                if kind == "name":
                    entries.append(name_entry(value))
                    continue
                title, found, collection = getattr(self, "_" + kind)(value, cancel)
                if not found:
                    raise ValueError("Сервис не вернул доступных треков")
                if collection:
                    documents.append({"version": 1, "name": title, "source": value, "entries": found})
                else:
                    entries.extend(found)
            except (ValueError, RuntimeError, requests.RequestException, TimeoutError, KeyError, TypeError) as exc:
                errors.append(f"Строка {number}: {exc}")
        return {"documents": documents, "entries": entries, "errors": errors}
