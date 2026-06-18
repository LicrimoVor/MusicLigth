import asyncio
import sys
import soundcard as sc
from PyQt5 import QtCore, QtWidgets, QtGui
import threading

from core.const import LAMP_POSITION
from libs.controller import TuyaLampController
from libs.init_lamp import init_lamp
from libs.lerp_color import lerp, lerp_color
from model import level as lvl
from ui.LampWidget import LampWidget


class AudioVisualizer(QtWidgets.QWidget):
    def __init__(self, tuya_controller: TuyaLampController = None):
        super().__init__()
        self.tuya_controller = tuya_controller
        self.setWindowTitle("Цветомузыка с настраиваемыми цветами и яркостью")
        self.resize(700, 800)

        self.mic_devices = sc.all_microphones(include_loopback=True)
        self.level_meter = lvl.LevelMeter()
        self.level_rms = lvl.RMSPeakLevel()
        self.stream = None
        self.selected_mic = None

        # Цвета (R,G,B)
        self.min_color = (0, 0, 255)  # синий по умолчанию
        self.max_color = (255, 0, 0)  # красный по умолчанию

        # Яркость в процентах (0-100)
        self.min_brightness = 30
        self.max_brightness = 100

        self.smooth_levels = {name: 0.0 for name in LAMP_POSITION}
        self.smoothing_factor = 0.3

        self.init_ui()

        self.timer = QtCore.QTimer()
        self.timer.setInterval(50)
        self.timer.timeout.connect(self.update_visualization)

    def init_ui(self):
        layout = QtWidgets.QVBoxLayout(self)

        layout.addWidget(QtWidgets.QLabel("Выберите аудио-устройство:"))
        self.device_combo = QtWidgets.QComboBox()
        for mic in self.mic_devices:
            self.device_combo.addItem(mic.name)
        layout.addWidget(self.device_combo)

        # Цвета
        colors_layout = QtWidgets.QHBoxLayout()
        self.min_color_btn = QtWidgets.QPushButton("Цвет мин (синий)")
        self.min_color_btn.clicked.connect(self.pick_min_color)
        colors_layout.addWidget(self.min_color_btn)

        self.max_color_btn = QtWidgets.QPushButton("Цвет макс (красный)")
        self.max_color_btn.clicked.connect(self.pick_max_color)
        colors_layout.addWidget(self.max_color_btn)

        layout.addLayout(colors_layout)

        # Яркость
        brightness_layout = QtWidgets.QHBoxLayout()
        self.min_brightness_spin = QtWidgets.QSpinBox()
        self.min_brightness_spin.setRange(0, 100)
        self.min_brightness_spin.setValue(self.min_brightness)
        self.min_brightness_spin.setSuffix(" %")
        self.min_brightness_spin.valueChanged.connect(self.min_brightness_changed)
        brightness_layout.addWidget(QtWidgets.QLabel("Мин яркость"))
        brightness_layout.addWidget(self.min_brightness_spin)

        self.max_brightness_spin = QtWidgets.QSpinBox()
        self.max_brightness_spin.setRange(0, 100)
        self.max_brightness_spin.setValue(self.max_brightness)
        self.max_brightness_spin.setSuffix(" %")
        self.max_brightness_spin.valueChanged.connect(self.max_brightness_changed)
        brightness_layout.addWidget(QtWidgets.QLabel("Макс яркость"))
        brightness_layout.addWidget(self.max_brightness_spin)

        layout.addLayout(brightness_layout)

        self.lamp_widget = LampWidget()
        layout.addWidget(self.lamp_widget, alignment=QtCore.Qt.AlignCenter)

        btn_layout = QtWidgets.QHBoxLayout()
        self.start_btn = QtWidgets.QPushButton("Старт")
        self.start_btn.clicked.connect(self.start_stream)
        btn_layout.addWidget(self.start_btn)

        self.stop_btn = QtWidgets.QPushButton("Стоп")
        self.stop_btn.clicked.connect(self.stop_stream)
        self.stop_btn.setEnabled(False)
        btn_layout.addWidget(self.stop_btn)

        layout.addLayout(btn_layout)

    def pick_min_color(self):
        color = QtWidgets.QColorDialog.getColor(
            QtGui.QColor(*self.min_color), self, "Выберите минимальный цвет"
        )
        if color.isValid():
            self.min_color = (color.red(), color.green(), color.blue())
            self.min_color_btn.setStyleSheet(f"background-color: rgb{self.min_color};")

    def pick_max_color(self):
        color = QtWidgets.QColorDialog.getColor(
            QtGui.QColor(*self.max_color), self, "Выберите максимальный цвет"
        )
        if color.isValid():
            self.max_color = (color.red(), color.green(), color.blue())
            self.max_color_btn.setStyleSheet(f"background-color: rgb{self.max_color};")

    def min_brightness_changed(self, val):
        self.min_brightness = val

    def max_brightness_changed(self, val):
        self.max_brightness = val

    def start_stream(self):
        if self.stream is not None:
            return
        idx = self.device_combo.currentIndex()
        if idx < 0:
            QtWidgets.QMessageBox.warning(self, "Ошибка", "Выберите устройство")
            return
        self.selected_mic = self.mic_devices[idx]
        try:
            self.stream = self.selected_mic.recorder(samplerate=44100)
            self.timer.start()
            self.start_btn.setEnabled(False)
            self.stop_btn.setEnabled(True)
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Ошибка", str(e))
            self.stream = None

    def stop_stream(self):
        if self.stream:
            try:
                self.stream.close()
            except Exception:
                pass
            self.stream = None
        self.timer.stop()
        self.lamp_widget.set_lamp_colors({name: (0, 0, 0) for name in LAMP_POSITION})
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)

    def update_visualization(self):
        try:
            data = self.selected_mic.record(numframes=2205, samplerate=44100)
            # level = rms_level(data)
            # level = self.level_meter.rms_level(data)
            level = self.level_rms(data)
            # level = lvl.spect_level(data)
        except Exception:
            level = 0.0

        new_levels = {}
        for name, pos in LAMP_POSITION.items():
            dist_to_center = ((pos[0] - 0.5) ** 2 + (pos[1] - 0.5) ** 2) ** 0.5
            brightness_factor = level * (0.5 + dist_to_center)
            brightness_factor = min(1.0, brightness_factor)
            new_levels[name] = brightness_factor

        for name in LAMP_POSITION:
            prev = self.smooth_levels[name]
            cur = new_levels[name]
            smooth = prev + self.smoothing_factor * (cur - prev)
            self.smooth_levels[name] = smooth

        final_colors = {}
        for name in LAMP_POSITION:
            level = self.smooth_levels[name]
            # Интерполяция цвета
            base_color = lerp_color(self.min_color, self.max_color, level)
            # Интерполяция яркости в 0..1
            brightness = lerp(self.min_brightness / 100, self.max_brightness / 100, level)
            R = int(base_color[0] * brightness)
            G = int(base_color[1] * brightness)
            B = int(base_color[2] * brightness)

            R = max(0, min(255, R))
            G = max(0, min(255, G))
            B = max(0, min(255, B))

            final_colors[name] = (R, G, B)

        self.lamp_widget.set_lamp_colors(final_colors)
        if self.tuya_controller:
            asyncio.run_coroutine_threadsafe(
                self.tuya_controller.set_colors(final_colors, brightness), async_loop
            )

    def closeEvent(self, event):
        self.stop_stream()
        event.accept()


async_loop = asyncio.new_event_loop()


def start_loop(loop):
    asyncio.set_event_loop(loop)
    loop.run_forever()


threading.Thread(target=start_loop, args=(async_loop,), daemon=True).start()

if __name__ == "__main__":
    devices = init_lamp()
    tuya_controller = TuyaLampController(devices)
    app = QtWidgets.QApplication(sys.argv)
    win = AudioVisualizer(tuya_controller)
    win.show()
    sys.exit(app.exec_())
