"""Asynchronous LRCLIB lookup with a small local cache and LRC parsing."""
from difflib import SequenceMatcher
import hashlib
import json
import math
from itertools import islice
from pathlib import Path
import re
import time

from PyQt5 import QtCore, QtNetwork

from music.matching import normalized, search_query
from music.storage import atomic_json


MAX_LYRICS_BYTES = 2 * 1024 * 1024
TIMESTAMP = re.compile(r"\[(?:(\d{1,4}):)?(\d{1,3}):(\d{2}(?:\.\d{1,3})?)\]")


def parse_lrc(text):
    offset = re.search(r"\[offset:([+-]?\d{1,8})\]", text, flags=re.I)
    offset = int(offset[1]) / 1000 if offset else 0
    lines = []
    for line in text.splitlines()[:10000]:
        if len(lines) >= 10000:
            break
        matches = list(islice(TIMESTAMP.finditer(line), 10000 - len(lines)))
        if not matches:
            continue
        words = TIMESTAMP.sub("", line).strip()
        for match in matches:
            seconds = int(match[1] or 0) * 3600 + int(match[2]) * 60 + float(match[3]) - offset
            lines.append((max(0.0, seconds), words))
    lines.sort(key=lambda item: item[0])
    merged = []
    for seconds, words in lines:
        if merged and seconds == merged[-1][0]:
            merged[-1] = (seconds, "\n".join(filter(None, (merged[-1][1], words))))
        else:
            merged.append((seconds, words))
    return merged


def track_identity(track):
    title = track.get("original_title") or track.get("title") or Path(track.get("path") or "").stem
    artist = str(track.get("artist") or "").strip()
    if artist:
        title = re.sub(r"^" + re.escape(artist) + r"\s*[-–—:]\s*", "", title, flags=re.I)
    title = re.sub(r"\s*[\[(](?:official (?:audio|video|music video)|lyrics?|audio only)[\])]", "", title, flags=re.I).strip()
    try:
        duration = float(track.get("duration") or track.get("expected_duration") or 0)
        duration = duration if math.isfinite(duration) and duration > 0 else 0
    except (TypeError, ValueError):
        duration = 0
    return title, artist, duration


def select_lyrics(records, track):
    title, artist, duration = track_identity(track)
    best, best_score = None, -1
    for record in records:
        if not isinstance(record, dict) or not (record.get("plainLyrics") or record.get("syncedLyrics") or record.get("instrumental")):
            continue
        if any(record.get(key) is not None and not isinstance(record[key], str) for key in ("plainLyrics", "syncedLyrics")):
            continue
        if not isinstance(record.get("trackName"), str) or not isinstance(record.get("artistName", ""), str):
            continue
        candidate = normalized(record.get("trackName") or "")
        score = SequenceMatcher(None, normalized(title), candidate).ratio()
        if score < 0.82:
            continue
        if artist:
            expected = set(normalized(artist).split())
            found = set(normalized(record.get("artistName") or "").split())
            agreement = len(expected & found) / max(1, len(expected))
            if agreement < 0.6:
                continue
            score += agreement
        try:
            candidate_duration = float(record.get("duration") or 0)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(candidate_duration):
            continue
        difference = abs(candidate_duration - duration) if duration > 0 and candidate_duration > 0 else 0
        if difference > max(8, duration * 0.06):
            continue
        score += 0.2 if record.get("syncedLyrics") else 0
        score += max(0, 0.1 - difference / 100)
        if score > best_score:
            best, best_score = record, score
    return best


class LyricsCache:
    def __init__(self, root):
        self.root = Path(root) / "lyrics"

    def path(self, track):
        title, artist, duration = track_identity(track)
        identity = [normalized(title), normalized(artist), round(duration)]
        key = hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode("utf-8")).hexdigest()
        return self.root / f"{key}.json"

    def load(self, track):
        path = self.path(track)
        try:
            if path.stat().st_size > MAX_LYRICS_BYTES * 4:
                return None
            document = json.loads(path.read_text(encoding="utf-8"))
            if document.get("version") != 1 or document.get("status") not in ("found", "not_found", "instrumental"):
                return None
            if not all(isinstance(document.get(key, ""), str) for key in ("plain", "synced", "source")):
                return None
            if any(document.get(key) is not None and not isinstance(document[key], str) for key in ("matched_artist", "matched_title")):
                return None
            if document["status"] == "not_found" and time.time() - float(document.get("saved_at", 0)) > 21600:
                return None
            if document["status"] == "found" and not (document.get("plain") or document.get("synced")):
                return None
            return document | {"cached": True}
        except (OSError, ValueError, TypeError, AttributeError):
            return None

    def save(self, track, document):
        atomic_json(self.path(track), document | {"version": 1, "saved_at": time.time()})


class LyricsClient(QtCore.QObject):
    ready = QtCore.pyqtSignal(str, dict)
    failed = QtCore.pyqtSignal(str, str)

    def __init__(self, root, parent=None, manager=None):
        super().__init__(parent)
        self.cache = LyricsCache(root)
        self.manager = manager or QtNetwork.QNetworkAccessManager(self)
        self.reply = None
        self.generation = 0
        self.track = None
        self.timer = QtCore.QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self._timeout)

    def cancel(self):
        self.generation += 1
        self.timer.stop()
        reply, self.reply = self.reply, None
        if reply is not None:
            reply.abort()

    def request(self, track, refresh=False):
        self.cancel()
        self.track = dict(track)
        try:
            cached = None if refresh else self.cache.load(track)
        except (ValueError, TypeError):
            cached = None
        if cached is not None:
            self.ready.emit(track["id"], cached)
            return
        title, artist, duration = track_identity(track)
        if not title:
            self.failed.emit(track["id"], "Для поиска нужно название песни")
            return
        self.timer.start(15000)
        params = {"track_name": title, "artist_name": artist}
        if track.get("album"):
            params["album_name"] = track["album"]
        if duration > 0:
            params["duration"] = str(round(duration))
        if artist:
            self._get("get", params)
        else:
            self._search()

    def _get(self, endpoint, params):
        url = QtCore.QUrl("https://lrclib.net/api/" + endpoint)
        query = QtCore.QUrlQuery()
        for key, value in params.items():
            if value:
                query.addQueryItem(key, str(value))
        url.setQuery(query)
        request = QtNetwork.QNetworkRequest(url)
        request.setRawHeader(b"User-Agent", b"MusicLight/1.0 (desktop lyrics client)")
        request.setRawHeader(b"Accept", b"application/json")
        reply = self.manager.get(request)
        self.reply = reply
        generation = self.generation
        data = bytearray()

        def read():
            if generation != self.generation or self.reply is not reply:
                return
            data.extend(bytes(reply.readAll()))
            if len(data) > MAX_LYRICS_BYTES:
                self._fail("Сервис вернул слишком большой ответ")

        def finished():
            if generation != self.generation or self.reply is not reply:
                reply.deleteLater()
                return
            read()
            if generation != self.generation:
                reply.deleteLater()
                return
            self.reply = None
            status = reply.attribute(QtNetwork.QNetworkRequest.HttpStatusCodeAttribute)
            error = reply.error()
            reply.deleteLater()
            if endpoint == "get" and status in (400, 404, 422):
                self._search()
                return
            if error != QtNetwork.QNetworkReply.NoError or status != 200:
                self._fail("Не удалось загрузить текст. Проверьте соединение и повторите поиск.")
                return
            try:
                response = json.loads(bytes(data))
                records = response if isinstance(response, list) else [response]
                record = select_lyrics(records, self.track)
                if record is None and endpoint == "get":
                    self._search()
                    return
                document = {"status": "not_found", "plain": "", "synced": "", "source": "LRCLIB"}
                if record:
                    document.update(status="instrumental" if record.get("instrumental") else "found",
                                    plain=record.get("plainLyrics") or "", synced=record.get("syncedLyrics") or "",
                                    source_id=record.get("id"), matched_artist=record.get("artistName"),
                                    matched_title=record.get("trackName"))
                self.timer.stop()
                try:
                    self.cache.save(self.track, document)
                    document["cached"] = True
                except OSError:
                    document["cached"] = False
                self.ready.emit(self.track["id"], document)
            except (ValueError, TypeError, AttributeError):
                self._fail("Сервис вернул некорректный текст песни")

        reply.readyRead.connect(read)
        reply.finished.connect(finished)

    def _search(self):
        self._get("search", {"q": search_query(self.track)})

    def _fail(self, message):
        identity = self.track["id"]
        self.cancel()
        self.failed.emit(identity, message)

    def _timeout(self):
        self._fail("Сервис текста не ответил за 15 секунд. Попробуйте ещё раз.")
