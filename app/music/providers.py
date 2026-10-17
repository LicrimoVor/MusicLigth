"""Bounded catalog requests; credentials are held in memory, never in manifests."""
import hashlib
import json
import os
import time
from urllib.parse import urlparse
from xml.etree import ElementTree

import requests

from music.storage import check_cancel


def request_bytes(url, cancel, *, params=None, data=None, headers=None, limit=4 * 1024 * 1024):
    check_cancel(cancel)
    if urlparse(url).scheme != "https":
        raise ValueError("Источник должен использовать HTTPS")
    try:
        with requests.request("POST" if data else "GET", url, params=params, data=data,
                              headers={"User-Agent": "MusicLight/1.0", **(headers or {})},
                              timeout=(4, 8), stream=True) as response:
            response.raise_for_status()
            chunks, size = [], 0
            deadline = time.monotonic() + 30
            for block in response.iter_content(65536):
                check_cancel(cancel)
                size += len(block)
                if size > limit:
                    raise ValueError("Ответ источника слишком большой")
                if time.monotonic() > deadline:
                    raise TimeoutError("Источник не ответил вовремя")
                chunks.append(block)
            return b"".join(chunks)
    except requests.RequestException as exc:
        # Request exceptions may include credential-bearing URLs. Do not expose them.
        status = exc.response.status_code if exc.response is not None else None
        raise RuntimeError(f"Источник недоступен (HTTP {status})" if status else "Не удалось связаться с источником") from None


def safe_choice(candidate):
    return {key: candidate[key] for key in ("webpage_url", "title", "artist", "duration", "provider", "provider_key", "source_id")
            if key in candidate}


class CatalogProviders:
    def __init__(self, credentials=None):
        self.credentials = credentials if credentials is not None else {}

    def token(self, name):
        return self.credentials.get(name) or os.environ.get(f"MUSICLIGHT_{name.upper()}_TOKEN", "")

    def _yandex(self, endpoint, cancel, params=None):
        token = self.token("yandex")
        headers = {"Authorization": "OAuth " + token} if token else {}
        result = json.loads(request_bytes("https://api.music.yandex.net" + endpoint, cancel, params=params, headers=headers))
        if result.get("error"):
            raise ValueError("Яндекс Музыка: требуется авторизация или доступ к записи")
        return result.get("result")

    def _vk(self, method, cancel, **params):
        token = self.token("vk")
        if not token:
            raise ValueError("Нужен токен VK с доступом к аудио: кнопка «Источники»")
        result = json.loads(request_bytes("https://api.vk.com/method/" + method, cancel,
            data={**params, "access_token": token, "v": "5.199"}))
        if result.get("error"):
            code = result["error"].get("error_code", "?")
            raise ValueError(f"VK: нет доступа к аудио или авторизация не принята (код {code})")
        return result.get("response") or {}

    def search(self, provider, query, cancel):
        if provider == "yandex":
            result = self._yandex("/search", cancel, {"text": query, "type": "track", "page": 0, "nocorrect": "true"}) or {}
            entries = []
            for track in (result.get("tracks") or {}).get("results", [])[:8]:
                albums = track.get("albums") or []
                if not track.get("id") or not albums or not albums[0].get("id"):
                    continue
                identity = str(track["id"])
                entries.append({"title": track.get("title", "") + (" (" + track["version"] + ")" if track.get("version") else ""),
                    "artist": ", ".join(artist.get("name", "") for artist in track.get("artists", [])),
                    "duration": (track.get("durationMs") or 0) / 1000, "source_id": identity,
                    "webpage_url": f"https://music.yandex.ru/album/{albums[0]['id']}/track/{identity}",
                    "provider": "Яндекс Музыка", "provider_key": "yandex"})
            return entries
        result = self._vk("audio.search", cancel, q=query, count=8, auto_complete=1)
        return [{"title": track.get("title", ""), "artist": track.get("artist", ""),
                 "duration": track.get("duration") or 0, "source_id": f"{track['owner_id']}_{track['id']}",
                 "webpage_url": f"https://vk.com/audio{track['owner_id']}_{track['id']}",
                 "provider": "VK Музыка", "provider_key": "vk"}
                for track in result.get("items", [])[:8] if track.get("id") and track.get("owner_id") is not None]

    def inspect(self, candidate, cancel):
        key, identity = candidate["provider_key"], candidate["source_id"]
        if key == "vk":
            tracks = self._vk("audio.getById", cancel, audios=identity)
            if not isinstance(tracks, list) or not tracks:
                raise ValueError("VK не предоставил эту запись")
            track = tracks[0]
            link = track.get("url") or ""
            # Protected/HLS recordings are left to the service's own player.
            if not link.startswith("https://") or ".m3u8" in urlparse(link).path:
                raise ValueError("VK не предоставил доступный MP3 для этой записи")
            audio = {"format_id": "http", "acodec": "mp3", "vcodec": "none", "abr": 0, "url": link}
        else:
            options = self._yandex(f"/tracks/{identity}/download-info", cancel) or []
            usable = [item for item in options if not item.get("preview") and item.get("codec") == "mp3"
                      and item.get("downloadInfoUrl") and (item.get("bitrateInKbps") or 0) >= 128]
            if not usable:
                raise ValueError("Яндекс не предоставил полную запись. Проверьте авторизацию и доступ аккаунта")
            selected = max(usable, key=lambda item: item.get("bitrateInKbps", 0))
            raw = request_bytes(selected["downloadInfoUrl"], cancel, limit=65536)
            try:
                xml = ElementTree.fromstring(raw)
            except ElementTree.ParseError:
                raise ValueError("Яндекс вернул неверные данные загрузки") from None
            host, path, stamp, salt = [xml.findtext(name) or "" for name in ("host", "path", "ts", "s")]
            if not host or "/" in host or not path.startswith("/") or not stamp or not salt:
                raise ValueError("Яндекс вернул неверные данные загрузки")
            signature = hashlib.md5(("XGRlBW9FXlekgbPrRHuSiA" + path[1:] + salt).encode()).hexdigest()
            audio = {"format_id": "http", "acodec": "mp3", "vcodec": "none", "abr": selected["bitrateInKbps"],
                     "url": f"https://{host}/get-mp3/{signature}/{stamp}{path}"}
        return candidate | {"formats": [audio], "direct_audio": True}


def download_audio(url, target, cancel, limit=100 * 1024 * 1024):
    check_cancel(cancel)
    if not url.startswith("https://"):
        raise ValueError("Нужен HTTPS-источник аудио")
    try:
        with requests.get(url, timeout=(4, 8), stream=True) as response:
            response.raise_for_status()
            size, deadline = 0, time.monotonic() + 240
            with target.open("wb") as file:
                for block in response.iter_content(131072):
                    check_cancel(cancel)
                    size += len(block)
                    if size > limit or time.monotonic() > deadline:
                        raise ValueError("Запись слишком большая или загрузка заняла слишком много времени")
                    file.write(block)
    except requests.RequestException:
        raise ValueError("Не удалось скачать запись; ссылка могла устареть или доступ ограничен") from None
