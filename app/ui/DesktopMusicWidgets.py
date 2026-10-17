from bisect import bisect_right

from PyQt5 import QtCore, QtWidgets

from music.lyrics import LyricsClient, parse_lrc
from ui.icons import icon
from ui.theme import colors
from ui.MusicPanel import duration_text


class DesktopPanel(QtWidgets.QWidget):
    visibilityChanged = QtCore.pyqtSignal(bool)
    positionSaved = QtCore.pyqtSignal()

    def __init__(self, title, preferences=None, size=(370, 210), offset=(30, 30)):
        super().__init__(None, QtCore.Qt.Tool | QtCore.Qt.FramelessWindowHint | QtCore.Qt.WindowStaysOnTopHint)
        self.setWindowTitle(f"Music Light · {title}")
        self.setWindowIcon(icon("app"))
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground)
        self.enabled = False
        self._shutting_down = False
        self._drag_position = None
        self.resize(*size)
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        card = QtWidgets.QFrame()
        card.setObjectName("desktopCard")
        outer.addWidget(card)
        self.content = QtWidgets.QVBoxLayout(card)
        self.content.setContentsMargins(12, 10, 12, 12)
        header = self.header = QtWidgets.QHBoxLayout()
        header.addWidget(QtWidgets.QLabel(title), 1)
        close = QtWidgets.QToolButton()
        close.setText("×")
        close.setFixedSize(24, 24)
        close.setToolTip("Скрыть виджет")
        close.clicked.connect(self.close)
        header.addWidget(close)
        self.content.addLayout(header)
        preferences = preferences if isinstance(preferences, dict) else {}
        position = preferences.get("position")
        geometry = QtWidgets.QApplication.primaryScreen().availableGeometry()
        if not isinstance(position, (list, tuple)) or len(position) != 2 or not all(isinstance(v, int) for v in position):
            position = [geometry.right() - self.width() - offset[0], geometry.top() + offset[1]]
        point = QtCore.QPoint(*position)
        screen = QtWidgets.QApplication.screenAt(point)
        if screen:
            geometry = screen.availableGeometry()
        self.move(max(geometry.left(), min(point.x(), geometry.right() - self.width() + 1)),
                  max(geometry.top(), min(point.y(), geometry.bottom() - self.height() + 1)))
        self.apply_theme()

    def apply_theme(self):
        p = colors()
        self.setStyleSheet(f"""
            QFrame#desktopCard {{ background: {p['window']}; border: 1px solid {p['border']}; border-radius: 12px; }}
            QLabel {{ background: transparent; color: {p['text']}; }}
            QLabel#muted {{ color: {p['muted']}; }}
            QLabel#widgetTitle {{ font-size: 18px; font-weight: 600; }}
            QLabel#currentLyrics {{ font-size: 21px; font-weight: 600; color: {p['accent']}; }}
            QToolButton {{ background: {p['button']}; border: 0; border-radius: 5px; }}
            QToolButton:hover {{ background: {p['selected']}; }}
        """)

    def set_enabled(self, enabled):
        enabled = bool(enabled)
        if self.enabled == enabled:
            return
        self.enabled = enabled
        self.setVisible(enabled)
        self.visibilityChanged.emit(enabled)

    def preferences(self):
        return {"enabled": self.enabled, "position": [self.x(), self.y()]}

    def mousePressEvent(self, event):
        if event.button() == QtCore.Qt.LeftButton:
            self._drag_position = event.globalPos() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if self._drag_position is not None and event.buttons() & QtCore.Qt.LeftButton:
            self.move(event.globalPos() - self._drag_position)
            event.accept()

    def mouseReleaseEvent(self, event):
        if self._drag_position is not None:
            self._drag_position = None
            self.positionSaved.emit()
            event.accept()

    def closeEvent(self, event):
        if self._shutting_down:
            event.accept()
        else:
            event.ignore()
            self.set_enabled(False)

    def shutdown(self):
        self._shutting_down = True
        self.close()
        self.deleteLater()


class DesktopMusicWidget(DesktopPanel):
    def __init__(self, panel, preferences=None):
        self.panel = panel
        super().__init__("Музыка", preferences)
        self.title = QtWidgets.QLabel()
        self.title.setObjectName("widgetTitle")
        self.title.setTextFormat(QtCore.Qt.PlainText)
        self.title.setWordWrap(True)
        self.artist = QtWidgets.QLabel()
        self.artist.setObjectName("muted")
        self.artist.setTextFormat(QtCore.Qt.PlainText)
        self.artist.setWordWrap(True)
        self.content.addWidget(self.title)
        self.content.addWidget(self.artist)
        row = QtWidgets.QHBoxLayout()
        self.previous = QtWidgets.QToolButton()
        self.play_pause = QtWidgets.QToolButton()
        self.next = QtWidgets.QToolButton()
        for button, title, callback in ((self.previous, "Предыдущая", lambda: panel.player.previous(force=True)),
                                        (self.play_pause, "Воспроизвести / пауза", panel.toggle_playback),
                                        (self.next, "Следующая", panel.player.next)):
            button.setToolTip(title)
            button.setAccessibleName(title)
            button.setFixedSize(36, 32)
            button.clicked.connect(callback)
            row.addWidget(button)
        row.addStretch()
        self.time = QtWidgets.QLabel()
        row.addWidget(self.time)
        self.content.addLayout(row)
        self.seek = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.seek.sliderReleased.connect(lambda: panel.player.seek(self.seek.value() / 1000))
        self.content.addWidget(self.seek)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("Громкость"))
        self.volume = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setValue(panel.volume_slider.value())
        self.volume.valueChanged.connect(panel.volume_slider.setValue)
        panel.volume_slider.valueChanged.connect(self.volume.setValue)
        self.volume.sliderReleased.connect(panel.save_preferences)
        row.addWidget(self.volume)
        self.content.addLayout(row)
        panel.player.trackChanged.connect(self.refresh)
        panel.player.queueChanged.connect(self.refresh)
        panel.player.playbackChanged.connect(self.refresh)
        panel.player.positionChanged.connect(self.set_position)
        panel.player.durationChanged.connect(self.set_duration)
        self.refresh()

    def refresh(self, *_args):
        track = self.panel.library.track(self.panel.player.current_track)
        self.title.setText(track['title'] if track else "Ничего не воспроизводится")
        self.artist.setText(track.get("artist", "") if track else "Выберите музыку в приложении")
        for button in (self.previous, self.play_pause, self.next):
            button.setEnabled(bool(track))
        self.set_duration(track.get("duration", 0) if track else 0)
        self.set_position(self.panel.player.position_seconds())
        self._icons()

    def _icons(self):
        for button, source in ((self.previous, self.panel.previous_button), (self.next, self.panel.next_button),
                               (self.play_pause, self.panel.play_pause_button)):
            button.setIcon(source.icon())
        self.play_pause.setToolTip("Пауза" if self.panel.player.want_play else "Воспроизвести")

    def apply_theme(self):
        super().apply_theme()
        if hasattr(self, "play_pause"):
            self._icons()

    def set_duration(self, duration):
        self.seek.setRange(0, max(0, round(duration * 1000)))

    def set_position(self, position):
        if not self.seek.isSliderDown():
            self.seek.setValue(round(position * 1000))
        self.time.setText(f"{duration_text(position)} / {duration_text(self.seek.maximum() / 1000)}")


class DesktopLyricsWidget(DesktopPanel):
    settingsChanged = QtCore.pyqtSignal()

    def __init__(self, panel, preferences=None):
        preferences = preferences if isinstance(preferences, dict) else {}
        def setting(key, default, low, high):
            value = preferences.get(key, default)
            return max(low, min(high, value)) if isinstance(value, int) else default
        self.lines_before = setting("lines_before", 1, 0, 8)
        self.lines_after = setting("lines_after", 1, 0, 8)
        self.font_size = setting("font_size", 21, 12, 48)
        size = preferences.get("size", [420, 300])
        if not isinstance(size, (list, tuple)) or len(size) != 2 or not all(isinstance(v, int) for v in size):
            size = [420, 300]
        size = (max(260, min(1600, size[0])), max(160, min(1200, size[1])))
        super().__init__("Текст", preferences, size=size, offset=(420, 30))
        self.panel = panel
        self.settings_dialog = None
        settings_button = QtWidgets.QToolButton()
        settings_button.setText("⚙")
        settings_button.setToolTip("Настройки виджета текста")
        settings_button.setAccessibleName("Настройки виджета текста")
        settings_button.setFixedSize(24, 24)
        settings_button.clicked.connect(self.show_settings)
        self.header.insertWidget(1, settings_button)
        self.track = None
        self.times, self.words = [], []
        self.client = LyricsClient(panel.library.root, self)
        self.client.ready.connect(self._ready)
        self.client.failed.connect(self._failed)
        self.title = QtWidgets.QLabel("Выберите музыку в приложении")
        self.title.setWordWrap(True)
        self.title.setTextFormat(QtCore.Qt.PlainText)
        self.content.addWidget(self.title)
        self.stack = QtWidgets.QStackedWidget()
        synced = QtWidgets.QWidget()
        self.lines_layout = QtWidgets.QVBoxLayout(synced)
        self.line_labels = []
        self._build_lines()
        self.plain = QtWidgets.QTextBrowser()
        self.plain.setStyleSheet(f"font-size: {self.font_size}px;")
        self.stack.addWidget(self.plain)
        self.stack.addWidget(synced)
        self.content.addWidget(self.stack, 1)
        panel.player.trackChanged.connect(self.refresh)
        panel.player.queueChanged.connect(lambda _queue: self.refresh() if not panel.player.current_track else None)
        panel.player.positionChanged.connect(self.set_position)

    def _build_lines(self):
        for label in self.line_labels:
            self.lines_layout.removeWidget(label)
            label.deleteLater()
        self.line_labels = []
        for offset in range(-self.lines_before, self.lines_after + 1):
            label = QtWidgets.QLabel()
            label.setWordWrap(True)
            label.setTextFormat(QtCore.Qt.PlainText)
            label.setAlignment(QtCore.Qt.AlignCenter)
            label.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Ignored)
            label.setObjectName("currentLyrics" if offset == 0 else "muted")
            self.lines_layout.addWidget(label, 1)
            self.line_labels.append(label)
        self.current_line = self.line_labels[self.lines_before]
        self.previous_line = self.line_labels[self.lines_before - 1] if self.lines_before else None
        self.next_line = self.line_labels[self.lines_before + 1] if self.lines_after else None
        self._apply_fonts()

    def _apply_fonts(self):
        for label in self.line_labels:
            size = self.font_size if label is self.current_line else max(12, self.font_size - 5)
            label.setStyleSheet(f"font-size: {size}px;")
        if hasattr(self, "plain"):
            self.plain.setStyleSheet(f"font-size: {self.font_size}px;")

    def preferences(self):
        return super().preferences() | {"size": [self.width(), self.height()], "font_size": self.font_size,
            "lines_before": self.lines_before, "lines_after": self.lines_after}

    def show_settings(self):
        if self.settings_dialog:
            self.settings_dialog.show()
            self.settings_dialog.raise_()
            return
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle("Виджет текста")
        form = QtWidgets.QFormLayout(dialog)
        self.settings_controls = {}
        for key, title, value, low, high in (
            ("width", "Ширина", self.width(), 260, 1600),
            ("height", "Высота", self.height(), 160, 1200),
            ("font_size", "Размер текста", self.font_size, 12, 48),
            ("lines_before", "Строк до текущей", self.lines_before, 0, 8),
            ("lines_after", "Строк после текущей", self.lines_after, 0, 8)):
            spin = QtWidgets.QSpinBox()
            spin.setRange(low, high)
            spin.setValue(value)
            if key in ("width", "height", "font_size"):
                spin.setSuffix(" px")
            form.addRow(title, spin)
            self.settings_controls[key] = spin
            spin.valueChanged.connect(self._settings_changed)
        close = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Close)
        close.button(QtWidgets.QDialogButtonBox.Close).setText("Закрыть")
        close.rejected.connect(dialog.close)
        form.addRow(close)
        dialog.finished.connect(self._settings_closed)
        self.settings_dialog = dialog
        dialog.show()

    def _settings_changed(self, *_args):
        values = {key: spin.value() for key, spin in self.settings_controls.items()}
        rebuild = (self.lines_before, self.lines_after) != (values["lines_before"], values["lines_after"])
        self.lines_before, self.lines_after = values["lines_before"], values["lines_after"]
        self.font_size = values["font_size"]
        if rebuild:
            self._build_lines()
        self._apply_fonts()
        self.resize(values["width"], values["height"])
        self.set_position(self.panel.player.position_seconds())

    def _settings_closed(self, *_args):
        dialog, self.settings_dialog = self.settings_dialog, None
        if dialog:
            self.settingsChanged.emit()
            dialog.deleteLater()

    def set_enabled(self, enabled):
        changed = self.enabled != bool(enabled)
        super().set_enabled(enabled)
        if changed:
            if enabled:
                self.refresh()
            else:
                self.client.cancel()
                if self.settings_dialog:
                    self.settings_dialog.close()

    def refresh(self, *_args):
        if not self.enabled:
            return
        self.client.cancel()
        self.track = self.panel.library.track(self.panel.player.current_track)
        self.times, self.words = [], []
        self.stack.setCurrentWidget(self.plain)
        if not self.track:
            self.title.setText("Текст")
            self.plain.setPlainText("Выберите музыку в приложении")
            return
        self.title.setText(" · ".join(filter(None, (self.track["title"], self.track.get("artist")))))
        self.plain.setPlainText("Ищу текст песни…")
        self.client.request(self.track)

    def _ready(self, identity, document):
        if not self.enabled or not self.track or self.track["id"] != identity:
            return
        timed = parse_lrc(document.get("synced") or "")
        self.times = [seconds for seconds, _words in timed]
        self.words = [words for _seconds, words in timed]
        if timed:
            self.stack.setCurrentIndex(1)
            self.set_position(self.panel.player.position_seconds())
        else:
            text = document.get("plain") or document.get("synced")
            if document["status"] == "instrumental":
                text = "Инструментальная композиция — текста нет."
            self.plain.setPlainText(text or "Текст не найден. Можно добавить .lrc / .txt через кнопку «Т» в приложении.")
            self.stack.setCurrentWidget(self.plain)

    def _failed(self, identity, message):
        if self.enabled and self.track and self.track["id"] == identity:
            self.plain.setPlainText(message)

    def set_position(self, seconds):
        if not self.enabled or not self.times:
            return
        row = bisect_right(self.times, seconds) - 1
        for offset, label in enumerate(self.line_labels):
            index = row + offset - self.lines_before
            text = self.words[index] if 0 <= index < len(self.words) else "♪" if label is self.current_line else ""
            if label.text() != text:
                label.setText(text)

    def shutdown(self):
        self.client.cancel()
        if self.settings_dialog:
            self.settings_dialog.close()
        super().shutdown()
