import sys

from PyQt5 import QtWidgets

from libs.controller import TuyaLampController
from libs.init_lamp import init_lamp
from model.runtime import create_async_loop
from ui.AudioVisualizer import AudioVisualizer
from ui.widgets import app_stylesheet


def main():
    async_loop = create_async_loop()
    devices = init_lamp()
    tuya_controller = TuyaLampController(devices)

    app = QtWidgets.QApplication(sys.argv)
    app.setStyleSheet(app_stylesheet())

    window = AudioVisualizer(tuya_controller, async_loop=async_loop)
    window.show()
    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
