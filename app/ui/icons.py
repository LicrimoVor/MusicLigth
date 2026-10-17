from functools import lru_cache
from pathlib import Path

from PyQt5 import QtCore, QtGui, QtSvg
from ui.theme import colors


ASSETS = Path(__file__).resolve().parent / "assets"


def icon(name, *, muted=False, active=False):
    values = colors()
    tint = values["accent"] if active else values["muted"] if muted else values["text"]
    return _icon(name, tint, values["accent"])


@lru_cache(maxsize=None)
def _icon(name, tint, accent):
    if name == "app":
        return QtGui.QIcon(str(ASSETS / "app.ico"))
    svg = (ASSETS / f"{name}.svg").read_text(encoding="utf-8")
    svg = svg.replace("#edf2f7", tint).replace("#78f0b6", accent)
    renderer = QtSvg.QSvgRenderer(QtCore.QByteArray(svg.encode("utf-8")))
    result = QtGui.QIcon()
    for size in (20, 24, 32, 48, 64):
        pixmap = QtGui.QPixmap(size, size)
        pixmap.fill(QtCore.Qt.transparent)
        painter = QtGui.QPainter(pixmap)
        renderer.render(painter)
        painter.end()
        result.addPixmap(pixmap)
    return result
