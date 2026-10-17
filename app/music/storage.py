import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import threading
import uuid

from music.playlist_import import track_key


DEFAULT_MUSIC_DIR = Path(__file__).resolve().parents[1] / "data" / "music"
SUPPORTED_EXTENSIONS = {".mp3", ".wav", ".flac", ".ogg", ".opus", ".aif", ".aiff",
                        ".au", ".snd", ".caf", ".w64", ".rf64", ".wavex"}


def atomic_json(path, data, *, indent=2):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as file:
            file.write(json.dumps(data, ensure_ascii=False, indent=indent, allow_nan=False))
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class CancelledAnalysis(Exception):
    pass


def check_cancel(cancel):
    if cancel and cancel.is_set():
        raise CancelledAnalysis()


def content_id(path, cancel=None):
    digest = hashlib.sha256()
    with Path(path).open("rb") as file:
        while block := file.read(1024 * 1024):
            check_cancel(cancel)
            digest.update(block)
    return digest.hexdigest()


class MusicLibrary:
    """Small atomic manifest; original music files are referenced, never moved."""

    def __init__(self, root=DEFAULT_MUSIC_DIR):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "library.json"
        self.lock = threading.RLock()
        self.load_error = ""
        self.data = {"version": 1, "tracks": [], "playlists": [], "settings": {}}
        if self.path.exists():
            try:
                saved = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(saved, dict) or not isinstance(saved.get("tracks"), list):
                    raise ValueError("Invalid library manifest")
                self.data.update(saved)
                self.data["playlists"] = list(self.data.get("playlists") or [])
                self.data["settings"] = dict(self.data.get("settings") or {})
            except (ValueError, TypeError, OSError) as exc:
                backup = self.path.with_name("library.backup-" + uuid.uuid4().hex + ".json")
                shutil.copy2(self.path, backup)
                self.load_error = f"Библиотека восстановлена; резервная копия: {backup.name}. {exc}"
        for track in self.data["tracks"]:
            if track.get("status") == "Analyzing":
                track.update(status="Pending", progress=0)

    def save(self):
        with self.lock:
            # The compact encoder avoids walking every field in Python when a
            # theme, volume or widget preference saves a large music library.
            atomic_json(self.path, self.data, indent=None)

    def tracks(self):
        with self.lock:
            return copy.deepcopy(self.data["tracks"])

    def track(self, track_id):
        with self.lock:
            return copy.deepcopy(next((item for item in self.data["tracks"] if item["id"] == track_id), None))

    def add_files(self, paths, *, validated=False):
        added = []
        with self.lock:
            known = {os.path.normcase(item["path"]) for item in self.data["tracks"]}
            for path in paths:
                path = Path(path)
                if not validated:
                    path = path.expanduser().resolve()
                    if not path.is_file() or path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                        continue
                key = os.path.normcase(str(path))
                if key in known:
                    continue
                known.add(key)
                item = {"id": uuid.uuid4().hex, "path": str(path), "title": path.stem,
                        "artist": "", "album": "", "duration": 0.0, "status": "Pending",
                        "progress": 0, "error": "", "content_id": "", "analysis_version": 0}
                self.data["tracks"].append(item)
                added.append(copy.deepcopy(item))
            if added:
                self.save()
        return added

    def update_track(self, track_id, **fields):
        with self.lock:
            for item in self.data["tracks"]:
                if item["id"] == track_id:
                    item.update(fields)
                    self.save()
                    return copy.deepcopy(item)
        return None

    def rename_track(self, track_id, title):
        title = str(title).strip()
        if not title:
            raise ValueError("Введите название трека")
        with self.lock:
            track = self.track(track_id)
            if track:
                return self.update_track(track_id, title=title, custom_title=title,
                                         original_title=track.get("original_title", track["title"]))
        return None

    def record_listen(self, track_id):
        with self.lock:
            for track in self.data["tracks"]:
                if track["id"] == track_id:
                    track["play_count"] = int(track.get("play_count", 0)) + 1
                    self.save()
                    return copy.deepcopy(track)
        return None

    def remove_tracks(self, track_ids):
        removed = set(track_ids)
        with self.lock:
            self.data["tracks"] = [item for item in self.data["tracks"] if item["id"] not in removed]
            for playlist in self.data["playlists"]:
                playlist["tracks"] = [item for item in playlist["tracks"] if item not in removed]
            self.save()

    def playlists(self):
        with self.lock:
            return copy.deepcopy(self.data["playlists"])

    def create_playlist(self, name):
        name = str(name).strip()
        if not name:
            raise ValueError("Введите название плейлиста")
        item = {"id": uuid.uuid4().hex, "name": name[:120], "tracks": []}
        with self.lock:
            self.data["playlists"].append(item)
            self.save()
        return copy.deepcopy(item)

    def _remote_track(self, title, artist="", *, index=None, **fields):
        key = track_key(title, artist)
        existing = index.get(key) if index is not None else next((item for item in self.data["tracks"]
                         if track_key(item.get("original_title", item["title"]), item.get("artist", "")) == key), None)
        if existing:
            if fields.get("download_url") and not existing.get("download_url"):
                existing["download_url"] = fields["download_url"]
            return existing
        item = {"id": uuid.uuid4().hex, "path": "", "title": title.strip(), "artist": artist.strip(),
                "album": "", "duration": 0.0, "status": "NotDownloaded", "progress": 0,
                "error": "", "content_id": "", "analysis_version": 0, "remote": True, **fields}
        self.data["tracks"].append(item)
        if index is not None:
            index[key] = item
        return item

    def add_search_track(self, playlist_id, title, artist=""):
        title = title.strip()
        if not title:
            raise ValueError("Введите название трека")
        with self.lock:
            playlist = next((item for item in self.data["playlists"] if item["id"] == playlist_id), None)
            if not playlist:
                raise KeyError(playlist_id)
            track = self._remote_track(title, artist)
            if track["id"] not in playlist["tracks"]:
                playlist["tracks"].append(track["id"])
            self.save()
            return copy.deepcopy(track)

    def import_html_playlist(self, document, *, from_catalog=False):
        with self.lock:
            playlist = next((item for item in self.data["playlists"]
                             if item.get("html_source") == document["source"]), None)
            removed = set(self.data["settings"].get("removed_playlist_sources", []))
            if from_catalog:
                if document["source"] in removed:
                    return None
                if playlist is not None:
                    return copy.deepcopy(playlist)
            elif document["source"] in removed:
                removed.remove(document["source"])
                self.data["settings"]["removed_playlist_sources"] = sorted(removed)
            if playlist is None:
                playlist = {"id": uuid.uuid4().hex, "name": document["name"], "tracks": [],
                            "html_source": document["source"]}
                self.data["playlists"].append(playlist)
            # Keep user edits and append new items when a copied export is updated.
            index = {track_key(item.get("original_title", item["title"]), item.get("artist", "")): item for item in self.data["tracks"]}
            for entry in document["entries"]:
                track = self._remote_track(entry["title"], entry.get("artist", ""),
                                           index=index,
                                           expected_duration=entry.get("duration", 0),
                                           download_url=entry.get("download_url", ""),
                                           source_url=entry.get("source_url", ""))
                if track["id"] not in playlist["tracks"]:
                    playlist["tracks"].append(track["id"])
            self.save()
            return copy.deepcopy(playlist)

    def add_remote_entries(self, playlist_id, entries):
        with self.lock:
            playlist = next((item for item in self.data["playlists"] if item["id"] == playlist_id), None)
            if playlist is None:
                raise KeyError(playlist_id)
            index = {track_key(item.get("original_title", item["title"]), item.get("artist", "")): item for item in self.data["tracks"]}
            added = 0
            known = set(playlist["tracks"])
            for entry in entries:
                track = self._remote_track(entry["title"], entry.get("artist", ""), index=index,
                    expected_duration=entry.get("duration", 0), source_url=entry.get("source_url", ""),
                    download_url=entry.get("download_url", ""))
                if track["id"] not in known:
                    known.add(track["id"])
                    playlist["tracks"].append(track["id"])
                    added += 1
            self.save()
            return added

    def set_playlist_tracks(self, playlist_id, track_ids):
        with self.lock:
            known = {track["id"] for track in self.data["tracks"]}
            for playlist in self.data["playlists"]:
                if playlist["id"] == playlist_id:
                    playlist["tracks"] = [item for item in track_ids if item in known]
                    self.save()
                    return
            raise KeyError(playlist_id)

    def remove_playlist(self, playlist_id):
        with self.lock:
            playlist = next((item for item in self.data["playlists"] if item["id"] == playlist_id), None)
            if playlist and playlist.get("html_source"):
                removed = set(self.data["settings"].get("removed_playlist_sources", []))
                removed.add(playlist["html_source"])
                self.data["settings"]["removed_playlist_sources"] = sorted(removed)
            self.data["playlists"] = [item for item in self.data["playlists"] if item["id"] != playlist_id]
            self.save()

    def settings(self):
        with self.lock:
            return copy.deepcopy(self.data["settings"])

    def update_settings(self, settings):
        with self.lock:
            self.data["settings"].update(copy.deepcopy(settings))
            self.save()
