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

    def set_data(self, bars, level, current_color):
        self.bars = np.asarray(bars, dtype=np.float32)
        self.level = float(level)
        self.current_color = tuple(current_color)
        self.update()

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        rect = self.rect()
        painter.fillRect(rect, QtGui.QColor("#0d1115"))

        baseline = rect.bottom() - 22
        top = rect.top() + 22
        height = max(1, baseline - top)
        count = max(1, len(self.bars))
        gap = 3
        bar_width = max(4, (rect.width() - gap * (count + 1)) / count)

        accent = QtGui.QColor(*self.current_color)
        dim = QtGui.QColor("#26313a")

        for index, value in enumerate(self.bars):
            value = clamp(float(value))
            x = rect.left() + gap + index * (bar_width + gap)
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
        painter.end()


class BeatGraphWidget(QtWidgets.QWidget):
    def __init__(self, max_points=240):
        super().__init__()
        self.setMinimumHeight(320)
        self.max_points = max_points
        self.history = []

    def set_history(self, history):
        self.history = list(history)[-self.max_points :]
        self.update()

    def add_level(self, level):
        self.history.append(clamp(float(level)))
        if len(self.history) > self.max_points:
            self.history = self.history[-self.max_points :]
        self.update()

    def clear(self):
        self.history = []
        self.update()

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        rect = self.rect()
        painter.fillRect(rect, QtGui.QColor("#0d1115"))

        plot = rect.adjusted(48, 28, -18, -38)
        painter.setPen(QtGui.QPen(QtGui.QColor("#26313a"), 1))
        painter.drawRoundedRect(plot, 6, 6)

        font = painter.font()
        font.setPointSize(8)
        painter.setFont(font)

        for percent in range(0, 101, 25):
            y = plot.bottom() - plot.height() * percent / 100
            painter.setPen(QtGui.QPen(QtGui.QColor("#202a32"), 1))
            painter.drawLine(plot.left(), int(y), plot.right(), int(y))
            painter.setPen(QtGui.QColor("#9aa6b2"))
            painter.drawText(8, int(y) + 4, f"{percent}%")

        painter.setPen(QtGui.QColor("#c8d2dc"))
        painter.drawText(rect.adjusted(12, 8, -12, -8), QtCore.Qt.AlignTop, "График бита")

        if len(self.history) >= 2:
            path = QtGui.QPainterPath()
            points = self.history[-self.max_points :]
            step = plot.width() / max(1, self.max_points - 1)
            start_x = plot.right() - step * (len(points) - 1)
            first_y = plot.bottom() - clamp(points[0]) * plot.height()
            path.moveTo(start_x, first_y)

            for index, level in enumerate(points[1:], 1):
                x = start_x + step * index
                y = plot.bottom() - clamp(level) * plot.height()
                path.lineTo(x, y)

            fill_path = QtGui.QPainterPath(path)
            fill_path.lineTo(start_x + step * (len(points) - 1), plot.bottom())
            fill_path.lineTo(start_x, plot.bottom())
            fill_path.closeSubpath()

            fill = QtGui.QLinearGradient(plot.topLeft(), plot.bottomLeft())
            fill.setColorAt(0.0, QtGui.QColor(64, 192, 255, 90))
            fill.setColorAt(1.0, QtGui.QColor(64, 192, 255, 8))
            painter.setBrush(QtGui.QBrush(fill))
            painter.setPen(QtCore.Qt.NoPen)
            painter.drawPath(fill_path)

            painter.setPen(QtGui.QPen(QtGui.QColor("#40c0ff"), 2))
            painter.drawPath(path)

            current_percent = int(clamp(points[-1]) * 100)
            painter.setPen(QtGui.QColor("#edf2f7"))
            painter.drawText(
                plot.adjusted(0, 0, -8, -8),
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
    QComboBox, QSpinBox {
        background: #0f1419;
        border: 1px solid #35414b;
        border-radius: 5px;
        padding: 6px;
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
