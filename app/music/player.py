import random

from PyQt5 import QtCore, QtMultimedia
from music.audio_backend import PCMPlayerBackend


class PlaybackQueue:
    def __init__(self, rng=None):
        self.tracks = []
        self.order = []
        self.cursor = -1
        self.repeat = "off"
        self.shuffle = False
        self.rng = rng or random.Random()

    @property
    def current(self):
        return self.tracks[self.order[self.cursor]] if 0 <= self.cursor < len(self.order) else None

    def set_tracks(self, tracks, start=0):
        self.tracks = list(tracks)
        self.order = list(range(len(tracks)))
        self.cursor = 0 if tracks else -1
        if tracks:
            start = max(0, min(len(tracks) - 1, start))
            if self.shuffle:
                self.order.remove(start)
                self.rng.shuffle(self.order)
                self.order.insert(0, start)
            else:
                self.cursor = start

    def set_shuffle(self, enabled):
        current = self.order[self.cursor] if self.current else None
        self.shuffle = bool(enabled)
        if current is None:
            return
        if enabled:
            remaining = [index for index in self.order if index != current]
            self.rng.shuffle(remaining)
            self.order, self.cursor = [current] + remaining, 0
        else:
            self.order, self.cursor = list(range(len(self.tracks))), current

    def advance(self, direction=1, manual=True):
        if not self.tracks:
            return None
        if direction > 0 and not manual and self.repeat == "track":
            return self.current
        target = self.cursor + direction
        if target < 0:
            target = len(self.order) - 1 if self.repeat == "playlist" else 0
        if target >= len(self.order):
            if self.repeat != "playlist":
                return None
            target = 0
            if self.shuffle:
                self.rng.shuffle(self.order)
        self.cursor = target
        return self.current

    def ordered_tracks(self):
        return [self.tracks[index] for index in self.order]


class MusicPlayer(QtCore.QObject):
    trackRequested = QtCore.pyqtSignal(str)
    preparationRequested = QtCore.pyqtSignal(str)
    trackChanged = QtCore.pyqtSignal(str)
    queueChanged = QtCore.pyqtSignal(list)
    playbackChanged = QtCore.pyqtSignal(str)
    positionChanged = QtCore.pyqtSignal(float)
    durationChanged = QtCore.pyqtSignal(float)
    seekRequested = QtCore.pyqtSignal(float)
    sceneReset = QtCore.pyqtSignal()
    error = QtCore.pyqtSignal(str)
    repeatChanged = QtCore.pyqtSignal(int)
    listenCompleted = QtCore.pyqtSignal(str)

    def __init__(self, track_lookup, parent=None, backend=None):
        super().__init__(parent)
        self.lookup = track_lookup
        self.backend = backend or PCMPlayerBackend(self)
        self.backend.setNotifyInterval(20)
        self.queue = PlaybackQueue()
        self.state = "stopped"
        self.want_play = False
        self.waiting = None
        self.prepared = None
        self.failed_tracks = set()
        self.repeat_remaining = 0
        self.backend.positionChanged.connect(lambda value: self.positionChanged.emit(value / 1000))
        self.backend.durationChanged.connect(lambda value: self.durationChanged.emit(value / 1000))
        self.backend.stateChanged.connect(self._backend_state)
        self.backend.mediaStatusChanged.connect(self._media_status)
        self.backend.error.connect(self._backend_error)

    @property
    def current_track(self):
        return self.queue.current

    def position_seconds(self):
        # This is the media service's playback position, never a wall-clock estimate.
        return self.backend.position() / 1000

    def _state(self, state):
        self.state = state
        self.playbackChanged.emit(state)

    def set_queue(self, tracks, start=0):
        self.clear_repeat()
        self.want_play = False
        self.backend.stop()
        self.queue.set_tracks(tracks, start)
        self.failed_tracks.clear()
        self.prepared = None
        self.waiting = None
        self.sceneReset.emit()
        self.queueChanged.emit(self.queue.ordered_tracks())
        self._state("stopped")

    def replace_queue(self, tracks):
        current, was_playing = self.current_track, self.want_play
        tracks = list(tracks)
        if current in tracks:
            self.queue.set_tracks(tracks, tracks.index(current))
            self.queueChanged.emit(self.queue.ordered_tracks())
        else:
            self.set_queue(tracks)
            if was_playing and tracks:
                self.play()

    def play(self):
        if not self.current_track:
            return
        self.want_play = True
        if self.prepared == self.current_track:
            self.backend.play()
            return
        self._request_current()

    def _request_current(self):
        track_id = self.current_track
        if not track_id:
            self.stop()
            return
        track = self.lookup(track_id)
        if not track or track.get("status") == "Error":
            self.failed_tracks.add(track_id)
            if len(self.failed_tracks) >= len(set(self.queue.tracks)):
                self.error.emit("В очереди нет доступных треков")
                self.stop()
            else:
                self.next()
            return
        self.prepared = None
        self.waiting = track_id
        self.sceneReset.emit()
        self.trackChanged.emit(track_id)
        self._state("waiting" if self.want_play else "paused")
        if track["status"] == "Ready":
            self.trackRequested.emit(track_id)
        else:
            self.preparationRequested.emit(track_id)

    def track_updated(self, track_id):
        if self.waiting == track_id:
            self._request_current()

    def start_ready(self, track_id, playback_path):
        if track_id != self.current_track or self.waiting != track_id:
            return
        self.prepared, self.waiting = track_id, None
        self.backend.setMedia(QtMultimedia.QMediaContent(QtCore.QUrl.fromLocalFile(playback_path)))
        if self.prepared != track_id or self.current_track != track_id:
            return
        self.backend.setPosition(0)
        self.durationChanged.emit(float(self.lookup(track_id).get("duration", 0)))
        if self.want_play:
            self.backend.play()
        else:
            self._state("paused")

    def pause(self):
        self.want_play = False
        self.backend.pause()
        self._state("paused")

    def stop(self):
        self.want_play = False
        self.waiting = None
        self.backend.stop()
        self.backend.setPosition(0)
        self._state("stopped")
        self.sceneReset.emit()

    def next(self, manual=True):
        if manual:
            self.clear_repeat()
        self.backend.stop()
        self.prepared = None
        if self.queue.advance(1, manual) is None:
            self.stop()
            return
        self.queueChanged.emit(self.queue.ordered_tracks())
        self._request_current()

    def previous(self, force=False):
        self.clear_repeat()
        if not force and self.position_seconds() > 3:
            self.seek(0)
            return
        self.backend.stop()
        self.prepared = None
        self.queue.advance(-1)
        self._request_current()

    def seek(self, seconds):
        track = self.lookup(self.current_track) if self.current_track else None
        if not track or self.prepared != self.current_track:
            return
        seconds = max(0, min(float(track["duration"]), seconds))
        self.seekRequested.emit(seconds)
        self.backend.setPosition(round(seconds * 1000))

    def _backend_state(self, state):
        if state == QtMultimedia.QMediaPlayer.PlayingState:
            self._state("playing")
        elif state == QtMultimedia.QMediaPlayer.PausedState:
            self._state("paused")

    def _media_status(self, status):
        if status == QtMultimedia.QMediaPlayer.EndOfMedia and self.want_play:
            self.listenCompleted.emit(self.current_track)
            if self.repeat_remaining:
                self.repeat_remaining -= 1
                self.repeatChanged.emit(self.repeat_remaining)
                self.backend.stop()
                self.prepared = None
                self._request_current()
                return
            self.next(manual=False)

    def add_repeat(self):
        self.repeat_remaining += 1
        self.repeatChanged.emit(self.repeat_remaining)

    def clear_repeat(self):
        self.repeat_remaining = 0
        self.repeatChanged.emit(0)

    def _backend_error(self, error):
        if error == QtMultimedia.QMediaPlayer.NoError:
            return
        message = self.backend.errorString() or "Ошибка аудиоплеера"
        self.prepared = None
        self.error.emit(message)
        if self.current_track:
            self.failed_tracks.add(self.current_track)
        if self.want_play and len(self.failed_tracks) < len(set(self.queue.tracks)):
            self.next()
        else:
            self.stop()
