from collections import deque
import threading

from PyQt5 import QtCore

from music.downloads import MusicDownloader
from music.storage import CancelledAnalysis


class RecordingJobs(QtCore.QThread):
    results = QtCore.pyqtSignal(int, list, list)
    previewReady = QtCore.pyqtSignal(int, dict)
    failed = QtCore.pyqtSignal(int, str)
    progress = QtCore.pyqtSignal(int, int, str)

    def __init__(self, root, credentials=None, parent=None):
        super().__init__(parent)
        self.downloader = MusicDownloader(root, credentials)
        self.condition = threading.Condition()
        self.pending = deque()
        self.active_cancel = None
        self.stopping = False
        self.generation = 0

    def request(self, kind, track, value):
        with self.condition:
            self.generation += 1
            if self.active_cancel:
                self.active_cancel.set()
            self.pending.clear()
            if not self.stopping:
                self.pending.append((self.generation, kind, dict(track), value))
                self.condition.notify()
            return self.generation

    def cancel(self):
        with self.condition:
            self.generation += 1
            if self.active_cancel:
                self.active_cancel.set()
            self.pending.clear()

    def stop(self):
        with self.condition:
            self.stopping = True
            self.cancel()
            self.condition.notify()

    def run(self):
        while True:
            with self.condition:
                while not self.pending and not self.stopping:
                    self.condition.wait()
                if self.stopping:
                    return
                token, kind, track, value = self.pending.popleft()
                cancel = self.active_cancel = threading.Event()
            try:
                report = lambda percent, stage: self.progress.emit(token, percent, stage)
                if kind == "search":
                    entries, errors = self.downloader.browse(track, value, cancel, report)
                    if not cancel.is_set():
                        self.results.emit(token, entries, errors)
                else:
                    result = self.downloader.fetch(track, cancel, report, candidate=value)
                    if not cancel.is_set():
                        self.previewReady.emit(token, result)
            except CancelledAnalysis:
                pass
            except Exception as exc:
                if not cancel.is_set():
                    self.failed.emit(token, str(exc))
            finally:
                with self.condition:
                    if self.active_cancel is cancel:
                        self.active_cancel = None
