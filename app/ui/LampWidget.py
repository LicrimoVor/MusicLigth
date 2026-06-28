from PyQt5 import QtCore, QtGui, QtWidgets

from core.const import LAMP_POSITION


class LampWidget(QtWidgets.QWidget):
    lampToggled = QtCore.pyqtSignal(str, bool)

    def __init__(self, lamp_positions=None):
        super().__init__()
        self.setMinimumSize(440, 440)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding)
        self.lamp_positions = dict(lamp_positions or LAMP_POSITION)
        self.lamp_colors = {name: (0, 0, 0) for name in self.lamp_positions}
        self.selected_lamps = set(self.lamp_positions)
        self._hit_radius = 26

    def set_lamp_positions(self, positions):
        self.lamp_positions = dict(positions)
        self.lamp_colors = {name: self.lamp_colors.get(name, (0, 0, 0)) for name in self.lamp_positions}
        self.selected_lamps = {name for name in self.selected_lamps if name in self.lamp_positions}
        self.update()

    def set_selected_lamps(self, lamp_ids):
        self.selected_lamps = set(lamp_ids)
        self.update()

    def set_lamp_colors(self, colors_dict):
        self.lamp_colors.update(colors_dict)
        self.update()

    def _lamp_center(self, lamp_id):
        x, y = self.lamp_positions[lamp_id]
        margin = 42
        w = max(1, self.width() - margin * 2)
        h = max(1, self.height() - margin * 2)
        return QtCore.QPointF(margin + x * w, margin + (1 - y) * h)

    def _lamp_at(self, pos):
        for lamp_id in reversed(list(self.lamp_positions)):
            center = self._lamp_center(lamp_id)
            dx = center.x() - pos.x()
            dy = center.y() - pos.y()
            if (dx * dx + dy * dy) ** 0.5 <= self._hit_radius + 8:
                return lamp_id
        return None

    def mousePressEvent(self, event):
        lamp_id = self._lamp_at(event.pos())
        if not lamp_id:
            return

        enabled = lamp_id not in self.selected_lamps
        if enabled:
            self.selected_lamps.add(lamp_id)
        else:
            self.selected_lamps.discard(lamp_id)

        self.lampToggled.emit(lamp_id, enabled)
        self.update()

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing)

        rect = self.rect()
        painter.fillRect(rect, QtGui.QColor("#101418"))

        margin = 42
        field = rect.adjusted(margin, margin, -margin, -margin)
        painter.setPen(QtGui.QPen(QtGui.QColor("#28323a"), 1))
        painter.drawRoundedRect(field, 8, 8)

        painter.setPen(QtGui.QPen(QtGui.QColor("#1e272e"), 1))
        for i in range(1, 4):
            x = field.left() + field.width() * i / 4
            y = field.top() + field.height() * i / 4
            painter.drawLine(int(x), field.top(), int(x), field.bottom())
            painter.drawLine(field.left(), int(y), field.right(), int(y))

        font = painter.font()
        font.setPointSize(8)
        painter.setFont(font)

        for index, lamp_id in enumerate(self.lamp_positions):
            center = self._lamp_center(lamp_id)
            r, g, b = self.lamp_colors.get(lamp_id, (0, 0, 0))
            selected = lamp_id in self.selected_lamps

            halo = QtGui.QRadialGradient(center, 48)
            halo.setColorAt(0.0, QtGui.QColor(r, g, b, 120 if selected else 40))
            halo.setColorAt(1.0, QtGui.QColor(0, 0, 0, 0))
            painter.setBrush(QtGui.QBrush(halo))
            painter.setPen(QtCore.Qt.NoPen)
            painter.drawEllipse(center, 48, 48)

            painter.setBrush(QtGui.QColor(r, g, b) if selected else QtGui.QColor("#2c333a"))
            painter.setPen(QtGui.QPen(QtGui.QColor("#dfe7ef" if selected else "#65717c"), 2))
            painter.drawEllipse(center, self._hit_radius, self._hit_radius)

            label = str(index + 1)
            label_rect = QtCore.QRectF(center.x() - 14, center.y() - 9, 28, 18)
            painter.setPen(QtGui.QColor("#ffffff" if selected else "#9aa6b2"))
            painter.drawText(label_rect, QtCore.Qt.AlignCenter, label)

        painter.end()
