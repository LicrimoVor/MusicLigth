from bisect import bisect_right
from pathlib import Path

from PyQt5 import QtCore, QtWidgets

from music.lyrics import LyricsClient, MAX_LYRICS_BYTES, parse_lrc


class LyricsDialog(QtWidgets.QDialog):
    seekRequested = QtCore.pyqtSignal(float)
    loaded = QtCore.pyqtSignal()

    def __init__(self, root, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Текст песни")
        self.resize(620, 640)
        self.track = None
        self.times = []
        self.file_dialog = None
        self.client = LyricsClient(root, self)
        self.client.ready.connect(self._ready)
        self.client.failed.connect(self._failed)
        layout = QtWidgets.QVBoxLayout(self)
        self.title = QtWidgets.QLabel()
        self.title.setTextFormat(QtCore.Qt.PlainText)
        self.title.setWordWrap(True)
        self.title.setStyleSheet("font-size: 19px; font-weight: 600;")
        layout.addWidget(self.title)
        self.status = QtWidgets.QLabel()
        self.status.setTextFormat(QtCore.Qt.PlainText)
        self.status.setWordWrap(True)
        self.status.setObjectName("muted")
        layout.addWidget(self.status)
        self.stack = QtWidgets.QStackedWidget()
        self.lines = QtWidgets.QListWidget()
        self.lines.setWordWrap(True)
        self.lines.setStyleSheet("QListWidget { font-size: 18px; outline: 0; } QListWidget::item { padding: 12px; }")
        self.lines.itemDoubleClicked.connect(self._seek)
        self.plain = QtWidgets.QTextBrowser()
        self.plain.setStyleSheet("font-size: 18px;")
        self.stack.addWidget(self.lines)
        self.stack.addWidget(self.plain)
        layout.addWidget(self.stack, 1)
        row = QtWidgets.QHBoxLayout()
        self.follow = QtWidgets.QToolButton()
        self.follow.setText("Автопрокрутка")
        self.follow.setCheckable(True)
        self.follow.setChecked(True)
        row.addWidget(self.follow)
        row.addStretch(1)
        local = QtWidgets.QPushButton("Открыть .lrc / .txt")
        local.clicked.connect(self.choose_file)
        row.addWidget(local)
        retry = QtWidgets.QPushButton("Найти заново")
        retry.clicked.connect(lambda: self.set_track(self.track, refresh=True) if self.track else None)
        row.addWidget(retry)
        layout.addLayout(row)

    def set_track(self, track, refresh=False):
        self.track = dict(track)
        self.title.setText(" · ".join(filter(None, (track["title"], track.get("artist")))))
        self.times = []
        self.lines.clear()
        self.plain.clear()
        self.status.setText("Ищу текст песни…")
        self.client.request(track, refresh)

    def _ready(self, identity, document):
        if not self.track or identity != self.track["id"]:
            return
        self.times = []
        self.lines.clear()
        if document["status"] == "instrumental":
            self.stack.setCurrentWidget(self.plain)
            self.plain.setPlainText("Инструментальная композиция — текста нет.")
        elif document["status"] == "not_found":
            self.stack.setCurrentWidget(self.plain)
            self.plain.setPlainText("Текст не найден. Можно открыть локальный файл .lrc или .txt.")
        else:
            timed = parse_lrc(document.get("synced") or "")
            self.times = [seconds for seconds, _words in timed]
            if timed:
                self.lines.addItems([words or "♪" for _seconds, words in timed])
                self.stack.setCurrentWidget(self.lines)
            else:
                self.plain.setPlainText(document.get("plain") or document.get("synced") or "")
                self.stack.setCurrentWidget(self.plain)
        self.follow.setEnabled(bool(self.times))
        source = document.get("source") or "Локальный файл"
        if document.get("matched_artist"):
            source += " · " + document["matched_artist"]
        mode = "синхронизированные строки" if self.times else "обычный текст"
        cached = " · сохранён локально" if document.get("cached") else ""
        self.status.setText(f"{source} · {mode}{cached}")
        self.loaded.emit()

    def _failed(self, identity, message):
        if self.track and identity == self.track["id"]:
            self.status.setText(message)

    def set_position(self, seconds):
        if not self.isVisible() or not self.times:
            return
        row = bisect_right(self.times, seconds) - 1
        if row != self.lines.currentRow():
            self.lines.setCurrentRow(row)
            if row >= 0 and self.follow.isChecked():
                self.lines.scrollToItem(self.lines.item(row), QtWidgets.QAbstractItemView.PositionAtCenter)

    def _seek(self, item):
        row = self.lines.row(item)
        if 0 <= row < len(self.times):
            self.seekRequested.emit(self.times[row])

    def choose_file(self):
        if self.file_dialog:
            self.file_dialog.raise_()
            return
        dialog = QtWidgets.QFileDialog(self, "Открыть текст песни")
        dialog.setOption(QtWidgets.QFileDialog.DontUseNativeDialog, True)
        dialog.setOption(QtWidgets.QFileDialog.DontUseCustomDirectoryIcons, True)
        dialog.setFileMode(QtWidgets.QFileDialog.ExistingFile)
        dialog.setNameFilter("Текст песни (*.lrc *.txt)")
        dialog.fileSelected.connect(self.load_file)
        dialog.finished.connect(self._file_closed)
        self.file_dialog = dialog
        dialog.open()

    def _file_closed(self, _result):
        dialog, self.file_dialog = self.file_dialog, None
        if dialog:
            dialog.deleteLater()

    def load_file(self, path):
        if not self.track:
            return
        try:
            path = Path(path)
            if path.stat().st_size > MAX_LYRICS_BYTES:
                raise ValueError("Файл текста слишком большой")
            raw = path.read_bytes()
            try:
                text = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                text = raw.decode("cp1251")
            if not text.strip():
                raise ValueError("Файл текста пуст")
            self.client.cancel()
            document = {"status": "found", "source": "Локальный файл", "plain": text,
                        "synced": text if parse_lrc(text) else "", "cached": True}
            self.client.cache.save(self.track, document)
            self._ready(self.track["id"], document)
        except (OSError, ValueError) as exc:
            self.status.setText(str(exc))

    def done(self, result):
        self.client.cancel()
        if self.file_dialog:
            self.file_dialog.reject()
        super().done(result)
