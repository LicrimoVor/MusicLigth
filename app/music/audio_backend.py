from pathlib import Path
import struct

from PyQt5 import QtCore, QtMultimedia
import soundfile as sf


def pcm_data_offset(path):
    """Locate PCM data in the WAV/RF64 files produced by the local analyser."""
    with Path(path).open("rb") as source:
        header = source.read(12)
        if len(header) != 12 or header[8:] != b"WAVE" or header[:4] not in (b"RIFF", b"RF64"):
            raise ValueError("Ожидался локальный PCM WAV")
        while chunk := source.read(8):
            if len(chunk) < 8:
                break
            name, size = struct.unpack("<4sI", chunk)
            if name == b"data":
                return source.tell()
            source.seek(size + size % 2, 1)
    raise ValueError("В WAV отсутствует PCM-блок")


class PCMPlayerBackend(QtCore.QObject):
    """Qt PCM output; playback time comes from processedUSecs(), not a stopwatch.

    SoundFile/soxr prepares a 48 kHz stereo WAV in the analysis worker. QFile is
    consumed by Qt's audio service, independently of analysis and lamp I/O.
    """

    positionChanged = QtCore.pyqtSignal(int)
    durationChanged = QtCore.pyqtSignal(int)
    stateChanged = QtCore.pyqtSignal(int)
    mediaStatusChanged = QtCore.pyqtSignal(int)
    error = QtCore.pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.audio = None
        self.file = None
        self.duration = 0
        self.data_offset = 0
        self.frame_bytes = 4
        self.samplerate = 48000
        self.base_position = 0
        self.volume = 0.75
        self.playing = False
        self.paused = False
        self.finished = False
        self._error = ""
        self._error_code = QtMultimedia.QMediaPlayer.NoError
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(20)
        self.timer.timeout.connect(self._notify)

    def isAvailable(self):
        return not QtMultimedia.QAudioDeviceInfo.defaultOutputDevice().isNull()

    def setNotifyInterval(self, value):
        self.timer.setInterval(max(10, int(value)))

    def setVolume(self, value):
        self.volume = max(0, min(100, value)) / 100
        if self.audio:
            self.audio.setVolume(self.volume)

    def setMedia(self, media):
        self.stop()
        if self.file:
            self.file.close()
            self.file.deleteLater()
            self.file = None
        if self.audio:
            self.audio.deleteLater()
            self.audio = None
        self._error, self._error_code = "", QtMultimedia.QMediaPlayer.NoError
        path = media.canonicalUrl().toLocalFile() if not media.isNull() else ""
        if not path:
            self.duration = 0
            return
        try:
            info = sf.info(path)
            self.samplerate, self.frame_bytes = info.samplerate, info.channels * 2
            self.duration = round(info.frames / info.samplerate * 1000)
            self.data_offset = pcm_data_offset(path)
            format = QtMultimedia.QAudioFormat()
            format.setCodec("audio/pcm")
            format.setSampleRate(info.samplerate)
            format.setChannelCount(info.channels)
            format.setSampleSize(16)
            format.setSampleType(QtMultimedia.QAudioFormat.SignedInt)
            format.setByteOrder(QtMultimedia.QAudioFormat.LittleEndian)
            device = QtMultimedia.QAudioDeviceInfo.defaultOutputDevice()
            if device.isNull() or not device.isFormatSupported(format):
                raise ValueError(f"Устройство вывода не поддерживает PCM {info.samplerate} Гц / {info.channels} канала")
            self.file = QtCore.QFile(path, self)
            if not self.file.open(QtCore.QIODevice.ReadOnly):
                raise OSError(self.file.errorString())
            self.file.seek(self.data_offset)
            self.audio = QtMultimedia.QAudioOutput(device, format, self)
            self.audio.setBufferSize(round(info.samplerate * self.frame_bytes * 0.08))
            self.audio.setVolume(self.volume)
            self.audio.stateChanged.connect(self._audio_state)
            self.durationChanged.emit(self.duration)
            self.mediaStatusChanged.emit(QtMultimedia.QMediaPlayer.LoadedMedia)
        except (OSError, ValueError, RuntimeError) as exc:
            self._fail(str(exc))

    def position(self):
        processed = self.audio.processedUSecs() / 1000 if self.audio and (self.playing or self.paused) else 0
        return max(0, min(self.duration, round(self.base_position + processed)))

    def setPosition(self, milliseconds):
        if not self.audio or not self.file:
            return
        was_playing, was_paused = self.playing, self.paused
        self.playing = False
        self.audio.stop()
        self.base_position = max(0, min(self.duration, int(milliseconds)))
        frame = round(self.base_position / 1000 * self.samplerate)
        self.file.seek(self.data_offset + frame * self.frame_bytes)
        self.finished = False
        if was_playing:
            self.play()
        elif was_paused:
            # The next play starts a fresh audio clock at this seek position.
            self.paused = False
        self.positionChanged.emit(self.base_position)

    def play(self):
        if not self.audio or not self.file:
            self._fail(self._error or "Аудиофайл не подготовлен")
            return
        if self.paused:
            self.audio.resume()
        else:
            self.audio.start(self.file)
        if self.audio.error() not in (QtMultimedia.QAudio.NoError, QtMultimedia.QAudio.UnderrunError):
            self._fail("Не удалось открыть аудиоустройство")
            return
        self.playing, self.paused, self.finished = True, False, False
        self.stateChanged.emit(QtMultimedia.QMediaPlayer.PlayingState)
        self.timer.start()

    def pause(self):
        if self.audio and self.playing:
            self.audio.suspend()
        self.playing, self.paused = False, True
        self.timer.stop()
        self.stateChanged.emit(QtMultimedia.QMediaPlayer.PausedState)

    def stop(self):
        self.playing, self.paused = False, False
        self.timer.stop()
        if self.audio:
            self.audio.stop()
        self.base_position = 0
        if self.file:
            self.file.seek(self.data_offset)
        self.stateChanged.emit(QtMultimedia.QMediaPlayer.StoppedState)

    def _notify(self):
        if not self.playing or not self.audio:
            return
        position = self.position()
        self.positionChanged.emit(position)
        if self.audio.state() == QtMultimedia.QAudio.IdleState and position >= self.duration - 30 and not self.finished:
            self.finished = True
            self.playing = False
            self.timer.stop()
            self.mediaStatusChanged.emit(QtMultimedia.QMediaPlayer.EndOfMedia)

    def _audio_state(self, state):
        if state == QtMultimedia.QAudio.StoppedState and self.playing:
            if self.audio.error() != QtMultimedia.QAudio.NoError:
                self._fail("Аудиоустройство отключено или недоступно")

    def _fail(self, message):
        self.playing = False
        self.timer.stop()
        self._error, self._error_code = message, QtMultimedia.QMediaPlayer.ResourceError
        self.error.emit(self._error_code)

    def errorString(self):
        return self._error

    def error_code(self):
        return self._error_code
