import asyncio
import json

import numpy as np
import soundcard as sc
from PyQt5 import QtCore, QtGui, QtWidgets

from core.const import DEVICE_IP, LAMP_POSITION
from libs.controller import TuyaLampController
from libs.lerp_color import lerp, lerp_color
from model import level as lvl
from model.audio import FRAME_SIZE, SAMPLE_RATE, SPECTRUM_BINS, lamp_level, spectrum
from model.color import clamp, color_to_hex
from model.lamp_layout import build_lamp_positions
from model.settings import load_settings, save_settings
from ui.LampWidget import LampWidget
from ui.widgets import BeatGraphWidget, ColorButton, SpectrumWidget


class AudioVisualizer(QtWidgets.QWidget):
    diagnosticsReady = QtCore.pyqtSignal(list)

    def __init__(self, tuya_controller: TuyaLampController = None, async_loop=None):
        super().__init__()
        self.tuya_controller = tuya_controller
        self.async_loop = async_loop
        self.setWindowTitle("Music Light")
        self.resize(1180, 780)

        self.settings = load_settings()
        self.mic_devices = self.get_microphones()
        self.beat_detector = lvl.BeatOnsetDetector(samplerate=SAMPLE_RATE)
        self.selected_mic = None
        self.pending_lamp_update = None
        self.pending_mode_update = None
        self.last_audio_error = ""

        self.device_ids = [device.id for device in (tuya_controller.devices if tuya_controller else [])]
        if not self.device_ids:
            self.device_ids = list(DEVICE_IP)
        self.lamp_positions = build_lamp_positions(self.device_ids)

        saved_lamps = set(self.settings.get("selected_lamps") or [])
        default_lamps = [lamp_id for lamp_id in self.device_ids if lamp_id in LAMP_POSITION] or self.device_ids
        self.active_lamps = saved_lamps.intersection(self.device_ids) or set(default_lamps)
        self.smooth_levels = {lamp_id: 0.0 for lamp_id in self.lamp_positions}

        self.min_color = tuple(self.settings["min_color"])
        self.max_color = tuple(self.settings["max_color"])
        self.static_color = tuple(self.settings["static_color"])
        self.min_brightness = int(self.settings["min_brightness"])
        self.max_brightness = int(self.settings["max_brightness"])
        self.smoothing_factor = float(self.settings["smoothing"])
        self.beat_scale = float(self.settings["beat_scale"])
        self.beat_history = []
        self.mode = self.settings["mode"]

        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(50)
        self.timer.timeout.connect(self.update_visualization)

        self.diagnosticsReady.connect(self.render_diagnostics)
        self.init_ui()
        self.apply_loaded_settings()
        self.update_preview({}, np.zeros(SPECTRUM_BINS), 0.0, self.static_color)

    def get_microphones(self):
        try:
            return sc.all_microphones(include_loopback=True)
        except Exception as exc:
            self.last_audio_error = str(exc)
            return []

    def save_current_settings(self):
        data = {
            "mode": self.mode_combo.currentData(),
            "mic_name": self.device_combo.currentText(),
            "min_color": list(self.min_color),
            "max_color": list(self.max_color),
            "static_color": list(self.static_color),
            "min_brightness": self.min_brightness_spin.value(),
            "max_brightness": self.max_brightness_spin.value(),
            "smoothing": self.smoothing_slider.value() / 100,
            "beat_scale": self.beat_scale_slider.value() / 100,
            "selected_lamps": sorted(self.active_lamps),
        }
        settings_path = save_settings(data)
        self.settings_status.setText(f"Сохранено: {settings_path.name}")

    def init_ui(self):
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        header = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel("Music Light")
        title.setObjectName("title")
        self.connection_label = QtWidgets.QLabel(self.connection_summary())
        self.connection_label.setObjectName("muted")
        header.addWidget(title)
        header.addStretch(1)
        header.addWidget(self.connection_label)
        root.addLayout(header)

        self.tabs = QtWidgets.QTabWidget()
        self.tabs.addTab(self.build_control_tab(), "Управление")
        self.tabs.addTab(self.build_beat_tab(), "Бит")
        self.tabs.addTab(self.build_diagnostics_tab(), "Диагностика")
        root.addWidget(self.tabs, 1)

    def build_control_tab(self):
        page = QtWidgets.QWidget()
        layout = QtWidgets.QHBoxLayout(page)
        layout.setContentsMargins(0, 12, 0, 0)
        layout.setSpacing(14)

        controls = QtWidgets.QWidget()
        controls.setFixedWidth(360)
        controls_layout = QtWidgets.QVBoxLayout(controls)
        controls_layout.setContentsMargins(0, 0, 0, 0)
        controls_layout.setSpacing(12)
        controls_layout.addWidget(self.build_audio_group())
        controls_layout.addWidget(self.build_color_group())
        controls_layout.addWidget(self.build_lamp_group(), 1)
        controls_layout.addWidget(self.build_actions_group())

        visual = QtWidgets.QWidget()
        visual_layout = QtWidgets.QVBoxLayout(visual)
        visual_layout.setContentsMargins(0, 0, 0, 0)
        visual_layout.setSpacing(12)

        self.spectrum_widget = SpectrumWidget()
        self.lamp_widget = LampWidget(self.lamp_positions)
        self.lamp_widget.set_selected_lamps(self.active_lamps)
        self.lamp_widget.lampToggled.connect(self.set_lamp_enabled)

        status_line = QtWidgets.QHBoxLayout()
        self.current_color = QtWidgets.QLabel()
        self.current_color.setFixedSize(84, 34)
        self.current_color.setObjectName("colorPreview")
        self.level_label = QtWidgets.QLabel("Уровень: 0%")
        self.level_label.setObjectName("muted")
        status_line.addWidget(QtWidgets.QLabel("Текущий бит"))
        status_line.addWidget(self.current_color)
        status_line.addWidget(self.level_label)
        status_line.addStretch(1)

        visual_layout.addWidget(self.spectrum_widget)
        visual_layout.addWidget(self.lamp_widget, 1)
        visual_layout.addLayout(status_line)

        layout.addWidget(controls)
        layout.addWidget(visual, 1)
        return page

    def build_audio_group(self):
        group = QtWidgets.QGroupBox("Аудио и режим")
        layout = QtWidgets.QFormLayout(group)
        layout.setLabelAlignment(QtCore.Qt.AlignLeft)

        self.device_combo = QtWidgets.QComboBox()
        for mic in self.mic_devices:
            self.device_combo.addItem(mic.name)
        if not self.mic_devices:
            self.device_combo.addItem("Аудиоустройства не найдены")
            self.device_combo.setEnabled(False)
        layout.addRow("Источник", self.device_combo)

        self.mode_combo = QtWidgets.QComboBox()
        self.mode_combo.addItem("Бит: новый детектор", "rms")
        self.mode_combo.addItem("Спектр по лампам", "spectrum")
        self.mode_combo.addItem("Пульс одним цветом", "pulse")
        self.mode_combo.addItem("Статичный цвет", "static")
        self.mode_combo.currentIndexChanged.connect(self.on_mode_changed)
        layout.addRow("Режим", self.mode_combo)

        self.smoothing_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.smoothing_slider.setRange(5, 80)
        self.smoothing_slider.setValue(int(self.smoothing_factor * 100))
        self.smoothing_slider.valueChanged.connect(self.on_smoothing_changed)
        layout.addRow("Плавность", self.smoothing_slider)

        self.beat_scale_label = QtWidgets.QLabel(f"x{self.beat_scale:.2f}")
        self.beat_scale_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.beat_scale_slider.setRange(0, 200)
        self.beat_scale_slider.setValue(int(self.beat_scale * 100))
        self.beat_scale_slider.valueChanged.connect(self.on_beat_scale_changed)
        scale_row = QtWidgets.QHBoxLayout()
        scale_row.addWidget(self.beat_scale_slider, 1)
        scale_row.addWidget(self.beat_scale_label)
        layout.addRow("Масштаб бита", scale_row)
        return group

    def build_beat_tab(self):
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)
        layout.setContentsMargins(0, 12, 0, 0)
        layout.setSpacing(10)

        top = QtWidgets.QHBoxLayout()
        self.beat_percent_label = QtWidgets.QLabel("Бит: 0%")
        self.beat_percent_label.setObjectName("title")
        self.beat_scale_hint = QtWidgets.QLabel(f"Масштаб: x{self.beat_scale:.2f}")
        self.beat_scale_hint.setObjectName("muted")
        clear_btn = QtWidgets.QPushButton("Очистить график")
        clear_btn.clicked.connect(self.clear_beat_graph)
        top.addWidget(self.beat_percent_label)
        top.addWidget(self.beat_scale_hint)
        top.addStretch(1)
        top.addWidget(clear_btn)

        self.beat_graph_widget = BeatGraphWidget()
        layout.addLayout(top)
        layout.addWidget(self.beat_graph_widget, 1)
        return page

    def build_color_group(self):
        group = QtWidgets.QGroupBox("Цвет и яркость")
        layout = QtWidgets.QVBoxLayout(group)

        self.min_color_btn = ColorButton("Тихий", self.min_color)
        self.min_color_btn.colorChanged.connect(self.on_min_color_changed)
        self.max_color_btn = ColorButton("Бит", self.max_color)
        self.max_color_btn.colorChanged.connect(self.on_max_color_changed)
        self.static_color_btn = ColorButton("Статика", self.static_color)
        self.static_color_btn.colorChanged.connect(self.on_static_color_changed)
        layout.addWidget(self.min_color_btn)
        layout.addWidget(self.max_color_btn)
        layout.addWidget(self.static_color_btn)

        brightness = QtWidgets.QGridLayout()
        self.min_brightness_spin = QtWidgets.QSpinBox()
        self.min_brightness_spin.setRange(0, 100)
        self.min_brightness_spin.setSuffix(" %")
        self.min_brightness_spin.setValue(self.min_brightness)
        self.min_brightness_spin.valueChanged.connect(self.on_brightness_changed)

        self.max_brightness_spin = QtWidgets.QSpinBox()
        self.max_brightness_spin.setRange(0, 100)
        self.max_brightness_spin.setSuffix(" %")
        self.max_brightness_spin.setValue(self.max_brightness)
        self.max_brightness_spin.valueChanged.connect(self.on_brightness_changed)

        brightness.addWidget(QtWidgets.QLabel("Мин."), 0, 0)
        brightness.addWidget(self.min_brightness_spin, 0, 1)
        brightness.addWidget(QtWidgets.QLabel("Макс."), 0, 2)
        brightness.addWidget(self.max_brightness_spin, 0, 3)
        layout.addLayout(brightness)
        return group

    def build_lamp_group(self):
        group = QtWidgets.QGroupBox("Лампы")
        layout = QtWidgets.QVBoxLayout(group)

        row = QtWidgets.QHBoxLayout()
        select_all = QtWidgets.QPushButton("Все")
        select_all.clicked.connect(lambda: self.select_lamps(True))
        select_none = QtWidgets.QPushButton("Ни одной")
        select_none.clicked.connect(lambda: self.select_lamps(False))
        row.addWidget(select_all)
        row.addWidget(select_none)
        layout.addLayout(row)

        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QtWidgets.QWidget()
        self.lamp_list_layout = QtWidgets.QVBoxLayout(inner)
        self.lamp_list_layout.setContentsMargins(0, 0, 0, 0)
        self.lamp_checkboxes = {}
        for index, lamp_id in enumerate(self.device_ids, 1):
            checkbox = QtWidgets.QCheckBox(f"{index}. {lamp_id[-6:]}  {DEVICE_IP.get(lamp_id, '')}")
            checkbox.setToolTip(lamp_id)
            checkbox.setChecked(lamp_id in self.active_lamps)
            checkbox.toggled.connect(
                lambda checked, current_id=lamp_id: self.set_lamp_enabled(current_id, checked)
            )
            self.lamp_checkboxes[lamp_id] = checkbox
            self.lamp_list_layout.addWidget(checkbox)
        self.lamp_list_layout.addStretch(1)
        scroll.setWidget(inner)
        layout.addWidget(scroll, 1)
        return group

    def build_actions_group(self):
        group = QtWidgets.QGroupBox("Запуск")
        layout = QtWidgets.QVBoxLayout(group)

        buttons = QtWidgets.QHBoxLayout()
        self.start_btn = QtWidgets.QPushButton("Старт")
        self.start_btn.clicked.connect(self.start_stream)
        self.stop_btn = QtWidgets.QPushButton("Стоп")
        self.stop_btn.clicked.connect(lambda: self.stop_stream())
        self.stop_btn.setEnabled(False)
        buttons.addWidget(self.start_btn)
        buttons.addWidget(self.stop_btn)

        save_buttons = QtWidgets.QHBoxLayout()
        save_btn = QtWidgets.QPushButton("Сохранить")
        save_btn.clicked.connect(self.save_current_settings)
        apply_static_btn = QtWidgets.QPushButton("Применить цвет")
        apply_static_btn.clicked.connect(self.apply_static_color)
        turn_off_btn = QtWidgets.QPushButton("Выключить все")
        turn_off_btn.clicked.connect(self.turn_off_all_lamps)
        save_buttons.addWidget(save_btn)
        save_buttons.addWidget(apply_static_btn)
        save_buttons.addWidget(turn_off_btn)

        self.settings_status = QtWidgets.QLabel("")
        self.settings_status.setObjectName("muted")
        layout.addLayout(buttons)
        layout.addLayout(save_buttons)
        layout.addWidget(self.settings_status)
        return group

    def build_diagnostics_tab(self):
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)
        layout.setContentsMargins(0, 12, 0, 0)
        layout.setSpacing(10)

        top = QtWidgets.QHBoxLayout()
        self.diagnostics_status = QtWidgets.QLabel("Диагностика еще не запускалась")
        self.diagnostics_status.setObjectName("muted")
        refresh = QtWidgets.QPushButton("Обновить диагностику")
        refresh.clicked.connect(self.refresh_diagnostics)
        top.addWidget(self.diagnostics_status)
        top.addStretch(1)
        top.addWidget(refresh)
        self.refresh_diagnostics_btn = refresh
        layout.addLayout(top)

        self.diagnostics_table = QtWidgets.QTableWidget(0, 8)
        self.diagnostics_table.setHorizontalHeaderLabels(
            ["Вкл", "ID", "IP", "Версия", "Связь", "Задержка", "Ошибка", "Статус"]
        )
        self.diagnostics_table.verticalHeader().setVisible(False)
        self.diagnostics_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.diagnostics_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.diagnostics_table.horizontalHeader().setSectionResizeMode(1, QtWidgets.QHeaderView.Stretch)
        self.diagnostics_table.horizontalHeader().setSectionResizeMode(6, QtWidgets.QHeaderView.Stretch)
        self.diagnostics_table.horizontalHeader().setSectionResizeMode(7, QtWidgets.QHeaderView.Stretch)
        layout.addWidget(self.diagnostics_table, 1)

        self.render_diagnostics([])
        return page

    def apply_loaded_settings(self):
        mode_index = self.mode_combo.findData(self.mode)
        if mode_index >= 0:
            self.mode_combo.setCurrentIndex(mode_index)

        saved_mic = self.settings.get("mic_name", "")
        if saved_mic:
            mic_index = self.device_combo.findText(saved_mic)
            if mic_index >= 0:
                self.device_combo.setCurrentIndex(mic_index)

        self.sync_lamp_controls()

    def connection_summary(self):
        return f"Ламп: {len(self.device_ids)} · выбрано: {len(self.active_lamps)}"

    def sync_lamp_controls(self):
        for lamp_id, checkbox in self.lamp_checkboxes.items():
            checkbox.blockSignals(True)
            checkbox.setChecked(lamp_id in self.active_lamps)
            checkbox.blockSignals(False)
        self.lamp_widget.set_selected_lamps(self.active_lamps)
        self.connection_label.setText(self.connection_summary())

    def set_lamp_enabled(self, lamp_id, enabled):
        if enabled:
            self.active_lamps.add(lamp_id)
        else:
            self.active_lamps.discard(lamp_id)
        self.sync_lamp_controls()

    def select_lamps(self, enabled):
        self.active_lamps = set(self.device_ids) if enabled else set()
        self.sync_lamp_controls()

    def on_mode_changed(self):
        self.mode = self.mode_combo.currentData()
        if self.tuya_controller and self.active_lamps and self.async_loop and self.timer.isActive():
            self.pending_mode_update = asyncio.run_coroutine_threadsafe(
                self.tuya_controller.set_mode("music", self.active_lamps), self.async_loop
            )

    def on_smoothing_changed(self, value):
        self.smoothing_factor = value / 100

    def on_beat_scale_changed(self, value):
        self.beat_scale = value / 100
        label = f"x{self.beat_scale:.2f}"
        self.beat_scale_label.setText(label)
        self.beat_scale_hint.setText(f"Масштаб: {label}")

    def clear_beat_graph(self):
        self.beat_history = []
        self.beat_graph_widget.clear()
        self.beat_percent_label.setText("Бит: 0%")

    def on_min_color_changed(self, color):
        self.min_color = tuple(color)

    def on_max_color_changed(self, color):
        self.max_color = tuple(color)

    def on_static_color_changed(self, color):
        self.static_color = tuple(color)
        if self.mode_combo.currentData() == "static":
            self.apply_static_color()

    def on_brightness_changed(self):
        self.min_brightness = self.min_brightness_spin.value()
        self.max_brightness = self.max_brightness_spin.value()
        if self.min_brightness > self.max_brightness:
            self.max_brightness_spin.setValue(self.min_brightness)
            self.max_brightness = self.min_brightness

    def start_stream(self):
        if not self.mic_devices:
            QtWidgets.QMessageBox.warning(self, "Аудио", "Аудиоустройства не найдены")
            return

        idx = self.device_combo.currentIndex()
        if idx < 0:
            QtWidgets.QMessageBox.warning(self, "Аудио", "Выберите источник аудио")
            return

        self.selected_mic = self.mic_devices[idx]
        self.beat_detector = lvl.BeatOnsetDetector(samplerate=SAMPLE_RATE)
        self.timer.start()
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.settings_status.setText("Запущено")
        self.on_mode_changed()

    def stop_stream(self, send_to_lamps=True):
        self.timer.stop()
        self.selected_mic = None
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.settings_status.setText("Остановлено")

        off_colors = {lamp_id: (0, 0, 0) for lamp_id in self.active_lamps}
        self.update_beat_graph(0.0)
        self.update_preview(off_colors, np.zeros(SPECTRUM_BINS), 0.0, (0, 0, 0))
        if send_to_lamps:
            self.send_colors(off_colors, 0.0)

    def apply_static_color(self):
        brightness = self.max_brightness_spin.value() / 100
        color = tuple(int(channel * brightness) for channel in self.static_color)
        colors = {lamp_id: color for lamp_id in self.active_lamps}
        self.update_preview(colors, np.zeros(SPECTRUM_BINS), 1.0, color)
        self.send_colors(colors, brightness)

    def turn_off_all_lamps(self):
        colors = {lamp_id: (0, 0, 0) for lamp_id in self.device_ids}
        self.update_beat_graph(0.0)
        self.update_preview(colors, np.zeros(SPECTRUM_BINS), 0.0, (0, 0, 0))
        self.send_colors(colors, 0.0)
        self.settings_status.setText("Все лампы выключены")

    def update_visualization(self):
        mode = self.mode_combo.currentData()
        if mode == "static":
            self.apply_static_color()
            return

        data = self.read_audio()
        bars = spectrum(data) if data is not None else np.zeros(SPECTRUM_BINS)
        raw_rms_level = self.beat_detector(data) if data is not None else 0.0
        raw_spectrum_level = float(np.max(bars)) if len(bars) else 0.0
        bars = np.clip(bars * self.beat_scale, 0.0, 1.0)
        rms_level = clamp(raw_rms_level * self.beat_scale)
        spectrum_level = clamp(raw_spectrum_level * self.beat_scale)
        visual_level = clamp(spectrum_level if mode == "spectrum" else rms_level)

        final_colors = {}
        display_colors = {lamp_id: (0, 0, 0) for lamp_id in self.lamp_positions}
        global_brightness = lerp(self.min_brightness / 100, self.max_brightness / 100, visual_level)
        current_color = lerp_color(self.min_color, self.max_color, visual_level)

        for lamp_id, position in self.lamp_positions.items():
            if lamp_id not in self.active_lamps:
                self.smooth_levels[lamp_id] = 0.0
                continue

            level = lamp_level(mode, position, rms_level, bars)
            previous = self.smooth_levels.get(lamp_id, 0.0)
            smooth = previous + self.smoothing_factor * (level - previous)
            self.smooth_levels[lamp_id] = smooth

            base_color = lerp_color(self.min_color, self.max_color, smooth)
            brightness = lerp(self.min_brightness / 100, self.max_brightness / 100, smooth)
            color = (
                int(base_color[0] * brightness),
                int(base_color[1] * brightness),
                int(base_color[2] * brightness),
            )
            final_colors[lamp_id] = color
            display_colors[lamp_id] = color

        self.update_beat_graph(visual_level)
        self.update_preview(display_colors, bars, visual_level, current_color)
        self.send_colors(final_colors, global_brightness)

    def read_audio(self):
        try:
            return self.selected_mic.record(numframes=FRAME_SIZE, samplerate=SAMPLE_RATE)
        except Exception as exc:
            self.last_audio_error = str(exc)
            self.settings_status.setText(f"Аудио: {exc}")
            return None

    def update_preview(self, colors, bars, level, current_color):
        self.lamp_widget.set_lamp_colors(colors)
        self.spectrum_widget.set_data(bars, level, current_color)
        self.current_color.setStyleSheet(
            f"background: {color_to_hex(current_color)}; border: 1px solid #56616b; border-radius: 4px;"
        )
        self.level_label.setText(f"Уровень: {int(level * 100)}% · {color_to_hex(current_color).upper()}")

    def update_beat_graph(self, level):
        self.beat_history.append(clamp(level))
        self.beat_history = self.beat_history[-self.beat_graph_widget.max_points :]
        self.beat_graph_widget.set_history(self.beat_history)
        self.beat_percent_label.setText(f"Бит: {int(clamp(level) * 100)}%")

    def send_colors(self, colors, brightness):
        if not self.tuya_controller or not colors or not self.async_loop:
            return
        if self.pending_lamp_update and not self.pending_lamp_update.done():
            return
        self.pending_lamp_update = asyncio.run_coroutine_threadsafe(
            self.tuya_controller.set_colors(colors, brightness), self.async_loop
        )

    def refresh_diagnostics(self):
        if not self.tuya_controller or not self.async_loop:
            self.render_diagnostics([])
            return

        self.refresh_diagnostics_btn.setEnabled(False)
        self.diagnostics_status.setText("Проверяю лампы...")
        future = asyncio.run_coroutine_threadsafe(self.tuya_controller.diagnose_devices(), self.async_loop)

        def done(done_future):
            try:
                result = done_future.result()
            except Exception as exc:
                result = [
                    {
                        "id": "-",
                        "ip": "",
                        "version": "",
                        "ok": False,
                        "latency_ms": 0,
                        "status": None,
                        "error": str(exc),
                    }
                ]
            self.diagnosticsReady.emit(result)

        future.add_done_callback(done)

    def render_diagnostics(self, rows):
        if not hasattr(self, "diagnostics_table"):
            return

        known_rows = rows or [
            {
                "id": lamp_id,
                "ip": DEVICE_IP.get(lamp_id, ""),
                "version": "3.5",
                "ok": None,
                "latency_ms": "",
                "status": "",
                "error": "",
            }
            for lamp_id in self.device_ids
        ]

        self.diagnostics_table.setRowCount(len(known_rows))
        ok_count = 0
        for row_index, row in enumerate(known_rows):
            is_enabled = row["id"] in self.active_lamps
            ok = row.get("ok")
            if ok:
                ok_count += 1
            values = [
                "Да" if is_enabled else "Нет",
                row.get("id", ""),
                row.get("ip", ""),
                str(row.get("version", "")),
                "OK" if ok else ("-" if ok is None else "Ошибка"),
                f"{row.get('latency_ms', '')} мс" if row.get("latency_ms") != "" else "",
                row.get("error", ""),
                self.compact_status(row.get("status")),
            ]
            for column, value in enumerate(values):
                item = QtWidgets.QTableWidgetItem(str(value))
                if column == 4:
                    color = "#2f9e44" if ok else ("#6c757d" if ok is None else "#c92a2a")
                    item.setForeground(QtGui.QColor(color))
                self.diagnostics_table.setItem(row_index, column, item)

        if rows:
            self.diagnostics_status.setText(f"Связь: {ok_count}/{len(rows)} ламп")
        else:
            self.diagnostics_status.setText(
                "Показаны лампы из lamps.json. Нажмите обновление для проверки связи."
            )
        self.refresh_diagnostics_btn.setEnabled(True)

    def compact_status(self, status):
        if not status:
            return ""
        text = json.dumps(status, ensure_ascii=False)
        return text if len(text) <= 180 else text[:177] + "..."

    def closeEvent(self, event):
        self.save_current_settings()
        self.stop_stream(send_to_lamps=self.timer.isActive())
        event.accept()
