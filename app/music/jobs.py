from collections import deque
import hashlib
import os
from pathlib import Path
import threading
import uuid

from PyQt5 import QtCore

from music.analysis import MusicAnalyser
from music.downloads import MusicDownloader
from music.playlist_import import load_playlist, save_playlist
from music.sources import SourceImporter
from music.storage import CancelledAnalysis, SUPPORTED_EXTENSIONS, atomic_json, check_cancel
from music.timeline import TimelineGenerator


class MusicJobs(QtCore.QThread):
    progress = QtCore.pyqtSignal(str, int, str)
    analysed = QtCore.pyqtSignal(str, dict)
    failed = QtCore.pyqtSignal(str, str)
    cancelled = QtCore.pyqtSignal(str)
    timelineReady = QtCore.pyqtSignal(str, str, str)
    filesFound = QtCore.pyqtSignal(list)
    filesImported = QtCore.pyqtSignal(list, str)
    playlistFound = QtCore.pyqtSignal(dict)
    catalogFound = QtCore.pyqtSignal(dict)
    downloaded = QtCore.pyqtSignal(str, dict)
    textImported = QtCore.pyqtSignal(str, dict)
    importProgress = QtCore.pyqtSignal(str)
    replacementReady = QtCore.pyqtSignal(str, dict)
    replacementFailed = QtCore.pyqtSignal(str, str)
    replacementProgress = QtCore.pyqtSignal(str, int, str)
    queueChanged = QtCore.pyqtSignal(int)

    def __init__(self, root, parent=None, *, playlist_root=None):
        super().__init__(parent)
        self.analyser = MusicAnalyser(root)
        self.generator = TimelineGenerator(root)
        self.downloader = MusicDownloader(root)
        self.playlist_cache = Path(playlist_root) if playlist_root is not None else Path(root) / "playlists"
        self.source_importer = SourceImporter(root, self.playlist_cache)
        self.condition = threading.Condition()
        self.queue = deque()
        self.cancellations = {}
        self.stopping = False

    def _enqueue(self, kind, identity, payload, priority=False):
        with self.condition:
            if self.stopping:
                return
            key = (kind, identity)
            if key in self.cancellations:
                if priority:
                    for job in list(self.queue):
                        if job[0] == key:
                            self.queue.remove(job)
                            self.queue.appendleft(job)
                            break
                return
            cancel = threading.Event()
            self.cancellations[key] = cancel
            job = (key, payload, cancel)
            self.queue.appendleft(job) if priority else self.queue.append(job)
            self.condition.notify()
            self.queueChanged.emit(len(self.cancellations))

    def analyse(self, track, settings, priority=False):
        self._enqueue("analysis", track["id"], (dict(track), settings), priority=priority)

    def replace_recording(self, track, settings):
        excluded = list(track.get("excluded_urls") or [])
        if track.get("resolved_url") and track["resolved_url"] not in excluded:
            excluded.append(track["resolved_url"])
        revision = int(track.get("download_revision") or 0) + 1
        replacement = dict(track) | {"search_by_name": True, "download_revision": revision, "excluded_urls": excluded}
        self._enqueue("replacement", track["id"], (replacement, settings), priority=True)

    def import_playlists(self, paths):
        self._enqueue("playlists", uuid.uuid4().hex, list(paths), priority=True)

    def load_playlists(self, directories):
        self._enqueue("catalog", "saved", list(directories), priority=True)

    def import_text(self, text, playlist_id=""):
        self._enqueue("text_import", uuid.uuid4().hex, (text, playlist_id), priority=True)

    def _import_playlists(self, paths, cancel):
        for value in paths:
            path = Path(value)
            sources = sorted(path.glob("*.txt")) if path.is_dir() else [path]
            for source in sources:
                check_cancel(cancel)
                try:
                    if source.suffix.lower() == ".json":
                        document = load_playlist(source)
                        identity = hashlib.sha256(document["source"].encode("utf-8")).hexdigest()[:12]
                        atomic_json(self.playlist_cache / (Path(document["source"]).stem + "-" + identity + ".json"), document)
                    else:
                        _, document = save_playlist(source, self.playlist_cache, cancel)
                    self.playlistFound.emit(document)
                except (OSError, ValueError) as exc:
                    self.failed.emit("", str(exc))

    def timeline(self, track_id, analysis_path, settings, token):
        with self.condition:
            for key, event in self.cancellations.items():
                if key[0] == "timeline":
                    event.set()
        self._enqueue("timeline", token, (track_id, analysis_path, settings), priority=True)

    def import_directory(self, path):
        self.import_paths([path])

    def import_paths(self, paths, playlist_id=None):
        # Resolution/stat can block on unavailable drives; never do it in the GUI.
        self._enqueue("import", uuid.uuid4().hex, (list(paths), playlist_id), priority=True)

    def _discover_files(self, paths, cancel, playlist_id=None):
        batch = []

        def emit():
            if playlist_id:
                self.filesImported.emit(batch[:], playlist_id)
            else:
                self.filesFound.emit(batch[:])
            batch.clear()

        def add(path):
            if path.suffix.lower() in SUPPORTED_EXTENSIONS and path.is_file():
                batch.append(str(path.resolve()))
            if len(batch) >= 50:
                emit()

        for value in paths:
            check_cancel(cancel)
            path = Path(value).expanduser().resolve()
            if path.is_dir():
                for directory, directories, names in os.walk(path, followlinks=False):
                    check_cancel(cancel)
                    directories.sort()
                    for name in sorted(names):
                        check_cancel(cancel)
                        add(Path(directory) / name)
            else:
                add(path)
        if batch:
            emit()

    def cancel_track(self, track_id):
        with self.condition:
            for kind in ("analysis", "replacement"):
                event = self.cancellations.get((kind, track_id))
                if event:
                    event.set()

    def stop(self):
        with self.condition:
            self.stopping = True
            for event in self.cancellations.values():
                event.set()
            self.condition.notify_all()

    def run(self):
        while True:
            with self.condition:
                while not self.queue and not self.stopping:
                    self.condition.wait()
                if self.stopping:
                    return
                key, payload, cancel = self.queue.popleft()
            kind, identity = key
            last_progress = None

            def report(percent, stage):
                nonlocal last_progress
                state = (int(percent), stage)
                if state != last_progress:
                    last_progress = state
                    if kind == "replacement":
                        self.replacementProgress.emit(identity, *state)
                    else:
                        self.progress.emit(identity if kind == "analysis" else payload[0], *state)

            try:
                check_cancel(cancel)
                if kind in ("analysis", "replacement"):
                    track, settings = payload
                    path = track["path"]
                    download = {}
                    if track.get("remote"):
                        download = self.downloader.fetch(track, cancel, report)
                        path = download["path"]
                        if kind == "analysis":
                            self.downloaded.emit(identity, download)
                    result = self.analyser.analyse(path, cancel, lambda value, stage: report(20 + value * 0.7, stage))
                    if track.get("remote"):
                        result.update(title=track["title"], artist=track.get("artist", ""))
                    check_cancel(cancel)
                    timeline_path = self.generator.generate(result["cache_path"], settings, cancel,
                                                           lambda value, stage: report(90 + value * 0.1, stage))
                    if kind == "replacement":
                        self.replacementReady.emit(identity, result | download | {"timeline_path": timeline_path,
                            "search_by_name": True, "download_revision": track["download_revision"],
                            "excluded_urls": track["excluded_urls"]})
                    else:
                        self.analysed.emit(identity, result | {"timeline_path": timeline_path})
                elif kind == "timeline":
                    track_id, analysis_path, settings = payload
                    path = self.generator.generate(analysis_path, settings, cancel, report)
                    check_cancel(cancel)
                    self.timelineReady.emit(track_id, identity, path)
                elif kind == "playlists":
                    self._import_playlists(payload, cancel)
                elif kind == "text_import":
                    source, playlist_id = payload
                    result = self.source_importer.resolve(source, cancel, self.importProgress.emit)
                    for document in result["documents"]:
                        check_cancel(cancel)
                        identity = hashlib.sha256(document["source"].encode()).hexdigest()[:16]
                        atomic_json(self.playlist_cache / ("import-" + identity + ".json"), document)
                        self.playlistFound.emit(document)
                    self.textImported.emit(playlist_id, result)
                elif kind == "catalog":
                    saved = sorted(self.playlist_cache.glob("*.json"))
                    if saved:
                        for path in saved:
                            check_cancel(cancel)
                            try:
                                self.catalogFound.emit(load_playlist(path))
                            except (OSError, ValueError) as exc:
                                self.failed.emit("", str(exc))
                    else:
                        self._import_playlists(payload, cancel)
                elif kind == "import":
                    paths, playlist_id = payload
                    if playlist_id:
                        self._discover_files(paths, cancel, playlist_id)
                    else:
                        self._discover_files(paths, cancel)
            except CancelledAnalysis:
                if kind == "analysis":
                    self.cancelled.emit(identity)
                elif kind == "replacement":
                    self.replacementFailed.emit(identity, "Поиск другой записи отменён")
            except Exception as exc:
                if kind == "replacement":
                    self.replacementFailed.emit(identity, str(exc))
                else:
                    self.failed.emit(identity if kind != "timeline" else payload[0], str(exc))
            finally:
                with self.condition:
                    self.cancellations.pop(key, None)
                    self.queueChanged.emit(len(self.cancellations))
