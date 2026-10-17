import sys
import ctypes

from PyQt5 import QtWidgets

from libs.controller import TuyaLampController
from libs.init_lamp import init_lamp
from model.runtime import create_async_loop
from ui.AudioVisualizer import AudioVisualizer
from ui.widgets import app_stylesheet
from ui.icons import icon


def main():
    if sys.platform == "win32":
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("MusicLight.Desktop")
    async_loop = create_async_loop()
    devices = init_lamp()
    tuya_controller = TuyaLampController(devices)

    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName("Music Light")
    app.setWindowIcon(icon("app"))
    app.setStyleSheet(app_stylesheet())

    window = AudioVisualizer(tuya_controller, async_loop=async_loop)
    window.exitReady.connect(app.quit)
    window.show()
    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
