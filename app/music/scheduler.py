from collections import deque

import numpy as np
from PyQt5 import QtCore

from music.analysis import AnalysisData, SPECTRUM_START
from music.timeline import LightTimeline


class LightShowScheduler(QtCore.QObject):
    colorsReady = QtCore.pyqtSignal(dict)
    previewReady = QtCore.pyqtSignal(object, float, object)
    beatReady = QtCore.pyqtSignal(float, float, bool, float)
    sceneInvalidated = QtCore.pyqtSignal()

    def __init__(self, player, lamp_ids, settings, parent=None):
        super().__init__(parent)
        self.player, self.lamp_ids = player, list(lamp_ids)
        self.settings = settings
        self.timeline = None
        self.analysis = None
        self.active = False
        self.last_position = None
        self.last_visual_position = None
        self.preview_colors = {}
        self.pending_seek = None
        self.samples = deque(maxlen=300)
        self.timer = QtCore.QTimer(self)
        self.timer.setTimerType(QtCore.Qt.PreciseTimer)
        self.timer.setInterval(max(1, round(1000 / settings["update_hz"])))
        self.timer.timeout.connect(self.tick)
        # Visualisation follows the audio clock at 50 Hz, independent of Tuya's
        # much lower command limit. Both timers read the same prepared assets.
        self.visual_timer = QtCore.QTimer(self)
        self.visual_timer.setTimerType(QtCore.Qt.PreciseTimer)
        self.visual_timer.setInterval(20)
        self.visual_timer.timeout.connect(self.update_visuals)
        player.playbackChanged.connect(self.playback_changed)
        player.seekRequested.connect(self.seek_requested)
        player.positionChanged.connect(self.position_changed)
        player.sceneReset.connect(self.invalidate)

    def update_settings(self, settings):
        self.settings = settings
        self.timer.setInterval(max(1, round(1000 / settings["update_hz"])))
        self.invalidate()
        if self.active and self.timeline:
            self.tick(force=True)

    def set_timeline(self, path, analysis_path):
        timeline = LightTimeline(path)
        try:
            analysis = AnalysisData(analysis_path)
        except Exception:
            timeline.close()
            raise
        self.close_assets()
        self.timeline, self.analysis = timeline, analysis
        self.invalidate()
        if not self.player.waiting:
            self.playback_changed(self.player.state)

    def set_active(self, active):
        self.active = bool(active)
        self.invalidate()
        self.playback_changed(self.player.state)

    def invalidate(self):
        self.last_position = None
        self.last_visual_position = None
        self.sceneInvalidated.emit()

    def seek_requested(self, position):
        self.pending_seek = position
        self.invalidate()

    def position_changed(self, position):
        if self.pending_seek is not None and abs(position - self.pending_seek) < 0.1:
            self.pending_seek = None
            self.tick(force=True)
            self.update_visuals(force=True)

    def playback_changed(self, state):
        self.timer.stop()
        self.visual_timer.stop()
        if not self.active:
            return
        if state == "playing" and self.timeline:
            self.timer.start()
            self.visual_timer.start()
            self.tick(force=True)
            self.update_visuals(force=True)
        elif state in ("paused", "stopped", "waiting"):
            self.invalidate()
            if state == "stopped":
                self.preview_colors = {lamp: (0, 0, 0) for lamp in self.lamp_ids}
                self.colorsReady.emit(self.preview_colors)
            elif state == "paused" and self.player.prepared == self.player.current_track and not self.player.waiting:
                self.tick(force=True)
                self.update_visuals(force=True)

    def tick(self, force=False):
        if not self.active or not self.timeline or self.pending_seek is not None:
            return
        if not force and self.player.state != "playing":
            return
        position = self.player.position_seconds()
        if not force and position == self.last_position:
            return
        if self.last_position is not None and position < self.last_position - 0.1:
            self.invalidate()
        self.last_position = position
        selected = set(self.settings["lamp_ids"])
        colors = {}
        rendered = {}
        for lamp in self.lamp_ids:
            offset = (self.settings["latency_ms"] + self.settings["lamp_offsets"].get(lamp, 0)) / 1000
            when = max(0, position + offset)
            colors[lamp] = self.timeline.state_at(when, lamp) if lamp in selected else (0, 0, 0)
            rendered[lamp] = when
        self.colorsReady.emit(colors)
        self.preview_colors = colors
        self.samples.append({"audio_position": position, "rendered_positions": rendered})

    def update_visuals(self, force=False):
        if (not self.active or self.analysis is None or self.player.waiting
                or self.pending_seek is not None or (not force and self.player.state != "playing")):
            return
        position = self.player.position_seconds()
        previous = self.last_visual_position
        if not force and previous == position:
            return
        self.last_visual_position = position
        frame = self.analysis.frame_at(position)
        energy, raw = float(frame[7]), float(frame[5])
        self.previewReady.emit(np.asarray(frame[SPECTRUM_START:], dtype=np.float32), energy, self.preview_colors)
        beats = getattr(self.analysis, "beats", ())
        index = int(np.searchsorted(beats, position))
        nearby = beats[max(0, index - 1):index + 1]
        distance = min((abs(float(beat) - position) for beat in nearby), default=float("inf"))
        response = max(raw * 0.5, (0.35 + energy * 0.65) * float(np.exp(-0.5 * (distance / 0.065) ** 2)))
        markers = beats if len(beats) else getattr(self.analysis, "onsets", ())
        start = previous if previous is not None and previous <= position else position - 0.02
        onset = int(np.searchsorted(markers, position, side="right")) > int(np.searchsorted(markers, start, side="right"))
        self.beatReady.emit(response, raw, onset, position)

    def close_assets(self):
        if self.timeline:
            self.timeline.close()
            self.timeline = None
        if self.analysis:
            self.analysis.close()
            self.analysis = None

    def shutdown(self):
        self.timer.stop()
        self.visual_timer.stop()
        self.active = False
        self.close_assets()
