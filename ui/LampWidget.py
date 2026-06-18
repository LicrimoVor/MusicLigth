from PyQt5 import QtCore, QtWidgets, QtGui

from core.const import LAMP_POSITION


class LampWidget(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        self.setMinimumSize(500, 500)
        self.setMaximumSize(500, 500)
        self.lamp_colors = {name: (0, 0, 0) for name in LAMP_POSITION}

    def set_lamp_colors(self, colors_dict):
        self.lamp_colors = colors_dict
        self.update()

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing)

        painter.fillRect(self.rect(), QtGui.QColor(20, 20, 20))

        w = self.width()
        h = self.height()

        radius = 25
        for name, (x, y) in LAMP_POSITION.items():
            R, G, B = self.lamp_colors.get(name, (0, 0, 0))
            color = QtGui.QColor(R, G, B)
            painter.setBrush(color)
            painter.setPen(QtCore.Qt.NoPen)

            cx = int(x * w)
            cy = int((1 - y) * h)

            painter.drawEllipse(cx - radius, cy - radius, radius * 2, radius * 2)
