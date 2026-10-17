from PyQt5 import QtCore, QtWidgets

from ui.icons import icon
from ui.widgets import BeatGraphWidget
from ui.theme import colors


class DesktopBeatWidget(QtWidgets.QWidget):
    visibilityChanged = QtCore.pyqtSignal(bool)
    positionSaved = QtCore.pyqtSignal()

    def __init__(self, preferences=None):
        # An independent tool window stays visible when the main window hides.
        super().__init__(None, QtCore.Qt.Tool | QtCore.Qt.FramelessWindowHint | QtCore.Qt.WindowStaysOnTopHint)
        self.setWindowTitle("Music Light · Бит")
        self.setWindowIcon(icon("app"))
        self.setObjectName("desktopBeatWidget")
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground)
        self.resize(370, 250)
        self._drag_position = None
        self._shutting_down = False
        self.enabled = False
        self.source = "live"
        self.apply_theme()
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        card = QtWidgets.QFrame()
        card.setObjectName("beatCard")
        outer.addWidget(card)
        layout = QtWidgets.QVBoxLayout(card)
        layout.setContentsMargins(12, 10, 12, 12)
        layout.setSpacing(4)
        header = QtWidgets.QHBoxLayout()
        self.title = QtWidgets.QLabel("Бит · Реальное время")
        header.addWidget(self.title, 1)
        close = QtWidgets.QToolButton()
        close.setText("×")
        close.setFixedSize(24, 24)
        close.setToolTip("Скрыть виджет")
        close.clicked.connect(self.close)
        header.addWidget(close)
        layout.addLayout(header)
        row = QtWidgets.QHBoxLayout()
        self.level = QtWidgets.QLabel("0%")
        self.level.setObjectName("beatValue")
        self.tempo = QtWidgets.QLabel("")
        row.addWidget(self.level)
        row.addStretch(1)
        row.addWidget(self.tempo)
        layout.addLayout(row)
        self.graph = BeatGraphWidget(max_points=360, duration_seconds=6)
        self.graph.setMinimumHeight(150)
        self.graph.setMinimumWidth(260)
        layout.addWidget(self.graph, 1)
        geometry = QtWidgets.QApplication.primaryScreen().availableGeometry()
        preferences = preferences if isinstance(preferences, dict) else {}
        position = preferences.get("position")
        if not isinstance(position, (list, tuple)) or len(position) != 2 or not all(isinstance(value, int) for value in position):
            position = [geometry.right() - self.width() - 20, geometry.bottom() - self.height() - 20]
        point = QtCore.QPoint(*position)
        screen = QtWidgets.QApplication.screenAt(point)
        if screen:
            geometry = screen.availableGeometry()
        self.move(max(geometry.left(), min(point.x(), geometry.right() - self.width() + 1)),
                  max(geometry.top(), min(point.y(), geometry.bottom() - self.height() + 1)))

    def set_enabled(self, enabled):
        enabled = bool(enabled)
        if self.enabled == enabled:
            return
        self.enabled = enabled
        if enabled:
            self.graph.clear()
            self.show()
        else:
            self.hide()
        self.visibilityChanged.emit(enabled)

    def apply_theme(self):
        palette = colors()
        self.setStyleSheet(f"""
            QFrame#beatCard {{ background: {palette['window']}; border: 1px solid {palette['border']}; border-radius: 12px; }}
            QLabel {{ background: transparent; color: {palette['text']}; font-size: 12px; }}
            QLabel#beatValue {{ color: {palette['accent']}; font-size: 21px; font-weight: 600; }}
            QToolButton {{ color: {palette['text']}; background: {palette['button']}; border: 0; border-radius: 5px; }}
            QToolButton:hover {{ background: {palette['selected']}; }}
        """)
        self.update()

    def set_source(self, source):
        self.source = source
        self.title.setText("Бит · Музыка" if source == "player" else "Бит · Реальное время")
        self.tempo.clear()
        self.graph.clear()

    def add_sample(self, level, raw_level=None, onset=False, timestamp=None, tempo=0):
        if not self.enabled or not self.isVisible():
            return
        self.graph.add_level(level, raw_level, onset, timestamp)
        self.level.setText(f"{round(self.graph.valid_level(level) * 100)}%")
        self.tempo.setText(f"{tempo:.0f} BPM" if tempo > 0 else "")

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
        self.graph.paint_timer.stop()
        self.close()
        self.deleteLater()
