import threading
import time
import sys
import warnings

import numpy as np
import soundcard as sc

from PyQt5 import QtCore

from model.audio import (ANALYSIS_HOP, CAPTURE_BUFFER_SIZE, FRAME_SIZE, SAMPLE_RATE,
                         audio_features, mono_audio, spectrum_from_magnitude)
from model.level.beat_onset import BeatOnsetDetector


class LiveAudioBuffer:
    """Keep a short analysis window while consuming variable backend packets."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.samples = np.zeros(0, dtype=np.float32)
        self.pending = 0

    def push(self, data):
        samples = mono_audio(data)
        self.pending += len(samples)
        self.samples = np.concatenate((self.samples, samples))[-FRAME_SIZE:]
        if len(self.samples) < FRAME_SIZE or self.pending < ANALYSIS_HOP:
            return None
        hop, self.pending = self.pending, 0
        return self.samples, hop


def record_available(recorder):
    """Read without filling a requested block and report actual WASAPI gaps."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.filterwarnings("always", message="^data discontinuity in recording$",
                                category=getattr(sc, "SoundcardRuntimeWarning", RuntimeWarning))
        data = recorder.record(numframes=None)
    discontinuity = False
    for warning in caught:
        if str(warning.message) == "data discontinuity in recording":
            discontinuity = True
        else:
            warnings.warn_explicit(warning.message, warning.category, warning.filename, warning.lineno)
    return data, discontinuity


class AudioCaptureWorker(QtCore.QThread):
    failed = QtCore.pyqtSignal(str)
    discontinuityDetected = QtCore.pyqtSignal(int)

    def __init__(self, microphone, parent=None):
        super().__init__(parent)
        self.microphone = microphone
        self._lock = threading.Lock()
        self._latest = None
        self._peak = 0.0
        self._onset = False
        self.discontinuities = 0
        self._last_gap_notice = 0.0

    def take_frame(self):
        """Consume only the newest frame, preserving attacks between UI ticks."""
        with self._lock:
            if self._latest is None:
                return None
            bars, timestamp = self._latest
            frame = bars, self._peak, self._onset, timestamp
            self._latest = None
            self._peak = 0.0
            self._onset = False
            return frame

    def run(self):
        detector = BeatOnsetDetector(samplerate=SAMPLE_RATE)
        analysis = LiveAudioBuffer()
        uninitialize_com = False
        try:
            if sys.platform == "win32":
                # WASAPI COM objects must be initialised on the capture thread.
                import ctypes
                result = ctypes.windll.ole32.CoInitializeEx(None, 0)
                if result < 0 and result != -2147417850:  # RPC_E_CHANGED_MODE
                    raise OSError(f"COM initialisation failed: {result}")
                uninitialize_com = result >= 0
            # Buffer capacity absorbs scheduler stalls; record(None) still reads
            # small backend packets immediately, without waiting for 4096 samples.
            with self.microphone.recorder(samplerate=SAMPLE_RATE, blocksize=CAPTURE_BUFFER_SIZE) as recorder:
                while not self.isInterruptionRequested():
                    data, discontinuity = record_available(recorder)
                    if self.isInterruptionRequested():
                        break
                    # Discard stale analysis after a gap or a large queued packet.
                    if discontinuity or len(data) > FRAME_SIZE * 2:
                        detector.reset()
                        analysis.reset()
                        with self._lock:
                            self._latest, self._peak, self._onset = None, 0.0, False
                        if discontinuity:
                            self.discontinuities += 1
                            now = time.monotonic()
                            if now - self._last_gap_notice >= 1.0:
                                self._last_gap_notice = now
                                self.discontinuityDetected.emit(self.discontinuities)
                    frame = analysis.push(data[-FRAME_SIZE:] if len(data) > FRAME_SIZE * 2 else data)
                    if frame is None:
                        continue
                    window, hop = frame
                    magnitude, rms = audio_features(window)
                    bars = spectrum_from_magnitude(magnitude, len(window))
                    level = detector(window, magnitude=magnitude, rms=rms, hop_size=hop)
                    with self._lock:
                        self._latest = bars, time.monotonic()
                        self._peak = max(self._peak, level)
                        self._onset = self._onset or detector.last_onset
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            if uninitialize_com:
                ctypes.windll.ole32.CoUninitialize()
