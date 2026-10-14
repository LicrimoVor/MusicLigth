from collections import deque
import math
import time

import numpy as np
from PyQt5 import QtCore, QtGui, QtWidgets

from libs.lerp_color import lerp
from model.audio import SPECTRUM_BINS
from model.color import clamp, color_to_hex


class ColorButton(QtWidgets.QPushButton):
    colorChanged = QtCore.pyqtSignal(tuple)

    def __init__(self, title, color):
        super().__init__(title)
        self._title = title
        self._color = tuple(color)
        self.clicked.connect(self.pick_color)
        self.refresh()

    @property
    def color(self):
        return self._color

    def set_color(self, color):
        self._color = tuple(color)
        self.refresh()
        self.colorChanged.emit(self._color)

    def refresh(self):
        self.setText(f"{self._title}  {color_to_hex(self._color).upper()}")
        self.setStyleSheet(
            "QPushButton {"
            f"background: {color_to_hex(self._color)};"
            "color: white;"
            "font-weight: 600;"
            "border: 1px solid rgba(255,255,255,0.22);"
            "padding: 8px 10px;"
            "}"
        )

    def pick_color(self):
        color = QtWidgets.QColorDialog.getColor(QtGui.QColor(*self._color), self, self._title)
        if color.isValid():
            self.set_color((color.red(), color.green(), color.blue()))


class SpectrumWidget(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        self.setMinimumHeight(170)
        self.bars = np.zeros(SPECTRUM_BINS, dtype=np.float32)
        self.level = 0.0
        self.current_color = (0, 0, 0)
        self.last_update = None

    def set_data(self, bars, level, current_color):
        values = np.clip(np.nan_to_num(np.asarray(bars, dtype=np.float32)), 0.0, 1.0)
        now = time.monotonic()
        dt = now - self.last_update if self.last_update is not None else 0.033
        self.last_update = now
        if values.shape != self.bars.shape:
            self.bars = np.zeros_like(values)
        alpha = 1.0 - np.exp(-dt / np.where(values > self.bars, 0.025, 0.14))
        self.bars += alpha * (values - self.bars)
        self.level = float(level)
        self.current_color = tuple(current_color)
        self.update()

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        rect = self.rect()
        painter.fillRect(rect, QtGui.QColor("#0d1115"))

        baseline = rect.bottom() - 26
        top = rect.top() + 32
        height = max(1, baseline - top)
        count = max(1, len(self.bars))
        available = max(1, rect.width() - 24)
        slot_width = available / count
        gap = min(3.0, slot_width * 0.25)
        bar_width = max(0.1, slot_width - gap)

        accent = QtGui.QColor(*self.current_color)
        dim = QtGui.QColor("#26313a")

        for index, value in enumerate(self.bars):
            value = clamp(float(value))
            x = rect.left() + 12 + index * slot_width + gap / 2
            bar_height = max(3, value * height)
            bar_rect = QtCore.QRectF(x, baseline - bar_height, bar_width, bar_height)
            color = QtGui.QColor(
                int(lerp(dim.red(), accent.red(), value)),
                int(lerp(dim.green(), accent.green(), value)),
                int(lerp(dim.blue(), accent.blue(), value)),
            )
            painter.setBrush(color)
            painter.setPen(QtCore.Qt.NoPen)
            painter.drawRoundedRect(bar_rect, 3, 3)

        painter.setPen(QtGui.QPen(QtGui.QColor("#34404a"), 1))
        painter.drawLine(rect.left() + 8, baseline, rect.right() - 8, baseline)

        painter.setPen(QtGui.QColor("#c8d2dc"))
        painter.drawText(rect.adjusted(12, 8, -12, -8), QtCore.Qt.AlignTop, "Спектр аудио")
        painter.setPen(QtGui.QColor("#9aa6b2"))
        labels = QtCore.QRectF(12, baseline + 5, available, 18)
        for text, alignment in (("35 Гц", QtCore.Qt.AlignLeft), ("750 Гц", QtCore.Qt.AlignHCenter),
                                ("16 кГц", QtCore.Qt.AlignRight)):
            painter.drawText(labels, alignment | QtCore.Qt.AlignVCenter, text)
        painter.end()


class BeatGraphWidget(QtWidgets.QWidget):
    def __init__(self, max_points=600, duration_seconds=8.0):
        super().__init__()
        self.setMinimumHeight(220)
        self.setMinimumWidth(260)
        self.max_points = max(2, int(max_points))
        self.duration_seconds = max(1.0, float(duration_seconds))
        self.history = deque(maxlen=self.max_points)
        self._background = None
        self._background_key = None

    @staticmethod
    def valid_level(value):
        value = float(value)
        return clamp(value) if math.isfinite(value) else 0.0

    def set_history(self, history):
        values = list(history)[-self.max_points:]
        now = time.monotonic()
        step = self.duration_seconds / (self.max_points - 1)
        self.history.clear()
        for index, value in enumerate(values):
            level = self.valid_level(value)
            self.history.append((now - step * (len(values) - index - 1), level, level, False))
        self.update()

    def add_level(self, level, raw_level=None, onset=False, timestamp=None):
        timestamp = time.monotonic() if timestamp is None else float(timestamp)
        level = self.valid_level(level)
        raw_level = level if raw_level is None else self.valid_level(raw_level)
        if self.history and timestamp < self.history[-1][0]:
            self.history.clear()
        self.history.append((timestamp, level, raw_level, bool(onset)))
        cutoff = timestamp - self.duration_seconds
        while len(self.history) > 1 and self.history[1][0] < cutoff:
            self.history.popleft()
        self.update()

    def clear(self):
        self.history.clear()
        self.update()

    def resizeEvent(self, event):
        self._background = None
        self._background_key = None
        super().resizeEvent(event)

    def plot_rect(self):
        return QtCore.QRectF(self.rect()).adjusted(48, 46, -20, -34)

    def background_pixmap(self):
        ratio = self.devicePixelRatioF()
        key = (self.width(), self.height(), ratio)
        if self._background is not None and self._background_key == key:
            return self._background

        pixmap = QtGui.QPixmap(int(self.width() * ratio), int(self.height() * ratio))
        pixmap.setDevicePixelRatio(ratio)
        pixmap.fill(QtGui.QColor("#0d1115"))
        painter = QtGui.QPainter(pixmap)
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        rect = self.rect()
        plot = self.plot_rect()

        painter.setPen(QtGui.QPen(QtGui.QColor("#26313a"), 1))
        painter.drawRoundedRect(plot, 6, 6)

        font = painter.font()
        font.setPointSize(8)
        painter.setFont(font)

        for percent in range(0, 101, 25):
            y = plot.bottom() - plot.height() * percent / 100
            painter.setPen(QtGui.QPen(QtGui.QColor("#202a32"), 1))
            painter.drawLine(QtCore.QPointF(plot.left(), y), QtCore.QPointF(plot.right(), y))
            painter.setPen(QtGui.QColor("#9aa6b2"))
            label = QtCore.QRectF(0, y - 9, 40, 18)
            painter.drawText(label, QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter, f"{percent}%")

        for index in range(5):
            x = plot.left() + plot.width() * index / 4
            painter.setPen(QtGui.QPen(QtGui.QColor("#202a32"), 1))
            painter.drawLine(QtCore.QPointF(x, plot.top()), QtCore.QPointF(x, plot.bottom()))
            label = "сейчас" if index == 4 else f"−{self.duration_seconds * (1 - index / 4):g} с"
            painter.setPen(QtGui.QColor("#9aa6b2"))
            painter.drawText(QtCore.QRectF(x - 24, plot.bottom() + 8, 48, 18), QtCore.Qt.AlignCenter, label)

        painter.setPen(QtGui.QColor("#c8d2dc"))
        painter.drawText(rect.adjusted(12, 8, -12, -8), QtCore.Qt.AlignTop,
                         f"Бит · последние {self.duration_seconds:g} секунд")
        painter.setPen(QtGui.QColor("#40c0ff"))
        painter.drawText(48, 36, "Детектор")
        painter.setPen(QtGui.QColor("#62e5ab"))
        painter.drawText(132, 36, "Отклик")
        painter.setPen(QtGui.QColor("#ffcf66"))
        painter.drawText(202, 36, "Удар")
        painter.end()

        self._background = pixmap
        self._background_key = key
        return pixmap

    def curve_path(self, plot, samples, value_index):
        path = QtGui.QPainterPath()
        if not samples:
            return path
        start = samples[-1][0] - self.duration_seconds
        previous = None
        for sample in samples:
            x = plot.left() + (sample[0] - start) / self.duration_seconds * plot.width()
            y = plot.bottom() - sample[value_index] * plot.height()
            if previous is None or sample[0] - previous > 0.2:
                path.moveTo(x, y)
            else:
                path.lineTo(x, y)
            previous = sample[0]
        return path

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        painter.drawPixmap(0, 0, self.background_pixmap())
        plot = self.plot_rect()

        if self.history:
            samples = list(self.history)
            path = self.curve_path(plot, samples, 1)
            fill_path = QtGui.QPainterPath()
            # Close each continuous segment separately to leave recording gaps blank.
            segment = []
            start = samples[-1][0] - self.duration_seconds
            for sample in samples + [None]:
                if segment and (sample is None or sample[0] - segment[-1][0] > 0.2):
                    fill_segment = QtGui.QPainterPath()
                    first_x = plot.left() + (segment[0][0] - start) / self.duration_seconds * plot.width()
                    fill_segment.moveTo(first_x, plot.bottom())
                    for point in segment:
                        x = plot.left() + (point[0] - start) / self.duration_seconds * plot.width()
                        fill_segment.lineTo(x, plot.bottom() - point[1] * plot.height())
                    fill_segment.lineTo(x, plot.bottom())
                    fill_segment.closeSubpath()
                    fill_path.addPath(fill_segment)
                    segment = []
                if sample is not None:
                    segment.append(sample)

            painter.save()
            painter.setClipRect(plot)

            fill = QtGui.QLinearGradient(plot.topLeft(), plot.bottomLeft())
            fill.setColorAt(0.0, QtGui.QColor(98, 229, 171, 70))
            fill.setColorAt(1.0, QtGui.QColor(98, 229, 171, 4))
            painter.setBrush(QtGui.QBrush(fill))
            painter.setPen(QtCore.Qt.NoPen)
            painter.drawPath(fill_path)

            painter.setBrush(QtCore.Qt.NoBrush)
            painter.setPen(QtGui.QPen(QtGui.QColor("#40c0ff"), 1.5, QtCore.Qt.DashLine))
            painter.drawPath(self.curve_path(plot, samples, 2))
            painter.setPen(QtGui.QPen(QtGui.QColor("#62e5ab"), 2, QtCore.Qt.SolidLine,
                                     QtCore.Qt.RoundCap, QtCore.Qt.RoundJoin))
            painter.drawPath(path)
            for timestamp, level, raw_level, onset in samples:
                if onset:
                    x = plot.left() + (timestamp - start) / self.duration_seconds * plot.width()
                    painter.setPen(QtGui.QPen(QtGui.QColor(255, 207, 102, 110), 1, QtCore.Qt.DotLine))
                    painter.drawLine(QtCore.QPointF(x, plot.top()), QtCore.QPointF(x, plot.bottom()))
                    painter.setBrush(QtGui.QColor("#ffcf66"))
                    painter.setPen(QtCore.Qt.NoPen)
                    painter.drawEllipse(QtCore.QPointF(x, plot.bottom() - raw_level * plot.height()), 3, 3)
            painter.setBrush(QtGui.QColor("#62e5ab"))
            painter.setPen(QtCore.Qt.NoPen)
            painter.drawEllipse(QtCore.QPointF(plot.right(), plot.bottom() - samples[-1][1] * plot.height()), 3, 3)
            painter.restore()

            current_percent = int(samples[-1][1] * 100)
            painter.setPen(QtGui.QColor("#edf2f7"))
            painter.drawText(
                QtCore.QRectF(self.rect()).adjusted(0, 6, -20, -self.height() + 30),
                QtCore.Qt.AlignRight | QtCore.Qt.AlignTop,
                f"{current_percent}%",
            )
        else:
            painter.setPen(QtGui.QColor("#6f7c87"))
            painter.drawText(plot, QtCore.Qt.AlignCenter, "Нет данных")
        painter.end()


def app_stylesheet():
    return """
    QWidget {
        background: #151a1f;
        color: #edf2f7;
        font-size: 13px;
    }
    QLabel#title {
        font-size: 24px;
        font-weight: 700;
    }
    QLabel#muted {
        color: #9aa6b2;
    }
    QGroupBox {
        border: 1px solid #303941;
        border-radius: 8px;
        margin-top: 12px;
        padding: 12px;
        font-weight: 600;
    }
    QGroupBox::title {
        subcontrol-origin: margin;
        left: 10px;
        padding: 0 4px;
        color: #c8d2dc;
    }
    QPushButton {
        background: #26313a;
        border: 1px solid #3a4650;
        border-radius: 6px;
        padding: 8px 10px;
    }
    QPushButton:hover {
        background: #31404b;
    }
    QPushButton:disabled {
        color: #69747e;
        background: #20262c;
    }
    QComboBox, QSpinBox, QLineEdit {
        background: #0f1419;
        border: 1px solid #35414b;
        border-radius: 5px;
        padding: 6px;
    }
    QListWidget {
        background: #0f1419;
        border: 1px solid #303941;
        border-radius: 6px;
        padding: 4px;
    }
    QListWidget::item {
        padding: 7px;
        border-radius: 4px;
    }
    QListWidget::item:selected {
        background: #34404a;
    }
    QTabWidget::pane {
        border: 1px solid #303941;
        border-radius: 8px;
    }
    QTabBar::tab {
        background: #20262c;
        padding: 9px 16px;
        border-top-left-radius: 6px;
        border-top-right-radius: 6px;
        margin-right: 2px;
    }
    QTabBar::tab:selected {
        background: #34404a;
    }
    QTableWidget, QScrollArea {
        background: #0f1419;
        border: 1px solid #303941;
        border-radius: 6px;
    }
    QHeaderView::section {
        background: #20262c;
        color: #d7dee6;
        border: 0;
        padding: 7px;
    }
    QCheckBox {
        padding: 4px;
    }
    """
