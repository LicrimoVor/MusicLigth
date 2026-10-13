import threading
import time
import sys

from PyQt5 import QtCore

from model.audio import FRAME_SIZE, SAMPLE_RATE, audio_features, spectrum_from_magnitude
from model.level.beat_onset import BeatOnsetDetector


class AudioCaptureWorker(QtCore.QThread):
    failed = QtCore.pyqtSignal(str)

    def __init__(self, microphone, parent=None):
        super().__init__(parent)
        self.microphone = microphone
        self._lock = threading.Lock()
        self._latest = None
        self._peak = 0.0
        self._onset = False

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
        uninitialize_com = False
        try:
            if sys.platform == "win32":
                # WASAPI COM objects must be initialised on the capture thread.
                import ctypes
                result = ctypes.windll.ole32.CoInitializeEx(None, 0)
                if result < 0 and result != -2147417850:  # RPC_E_CHANGED_MODE
                    raise OSError(f"COM initialisation failed: {result}")
                uninitialize_com = result >= 0
            with self.microphone.recorder(samplerate=SAMPLE_RATE, blocksize=FRAME_SIZE) as recorder:
                while not self.isInterruptionRequested():
                    data = recorder.record(numframes=FRAME_SIZE)
                    if self.isInterruptionRequested():
                        break
                    magnitude, rms = audio_features(data)
                    bars = spectrum_from_magnitude(magnitude, len(data))
                    level = detector(data, magnitude=magnitude, rms=rms)
                    with self._lock:
                        self._latest = bars, time.monotonic()
                        self._peak = max(self._peak, level)
                        self._onset = self._onset or detector.last_onset
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            if uninitialize_com:
                ctypes.windll.ole32.CoUninitialize()
