from PyQt5 import QtCore, QtGui, QtWidgets

from ui.icons import icon


class SystemTray(QtCore.QObject):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.panel = window.music_panel
        self.color_dialog = None
        self.tray = QtWidgets.QSystemTrayIcon(icon("app"), window)
        self.menu = QtWidgets.QMenu(window)
        self.tray.setContextMenu(self.menu)
        self.tray.activated.connect(self._activated)
        self.menu.aboutToShow.connect(self.sync)
        self.title = self.menu.addAction("Music Light")
        self.title.setEnabled(False)
        source_menu = self.menu.addMenu("Источник света")
        group = QtWidgets.QActionGroup(source_menu)
        self.sources = {}
        for key, title in (("live", "Реальное время"), ("player", "Музыка")):
            action = source_menu.addAction(icon("live" if key == "live" else "music"), title)
            action.setCheckable(True)
            group.addAction(action)
            action.triggered.connect(lambda _checked=False, source=key: window.select_light_source(source))
            self.sources[key] = action

        self.live_menu = self.menu.addMenu("Режим света")
        self.live_modes = self._combo_actions(self.live_menu, window.mode_combo)
        self.colors_menu = self.menu.addMenu("Цвета")
        for title, button in (("Тихо", window.quiet_color_btn), ("Средне", window.medium_color_btn),
                              ("Громко", window.loud_color_btn), ("Статика", window.static_color_btn)):
            self.colors_menu.addAction(title, lambda _checked=False, current=button: self.choose_color(current))
        self.live_toggle = self.menu.addAction("Запустить захват", self.toggle_live)
        self.preset_menu = self.menu.addMenu("Режим светового шоу")
        self.presets = self._combo_actions(self.preset_menu, self.panel.preset_combo)
        self.music_colors_menu = self.menu.addMenu("Цвета музыки")
        self.music_color_modes = self._combo_actions(self.music_colors_menu, self.panel.music_color_mode)
        self.music_colors_menu.addSeparator()
        for title, button in zip(("Низкие", "Средние", "Высокие"), self.panel.music_color_buttons):
            self.music_colors_menu.addAction(title, lambda _checked=False, current=button: self.choose_color(current))
        self.music_previous = self.menu.addAction(self.panel.previous_button.icon(), "Назад (F9)",
                                                  lambda: self.panel.player.previous(force=True))
        self.music_pause = self.menu.addAction(self.panel.play_pause_button.icon(), "Пауза", self.panel.toggle_playback)
        self.music_next = self.menu.addAction(self.panel.next_button.icon(), "Вперёд (F10)", self.panel.player.next)
        self.volume_action = QtWidgets.QWidgetAction(self.menu)
        volume_widget = QtWidgets.QWidget()
        row = QtWidgets.QHBoxLayout(volume_widget)
        row.setContentsMargins(12, 6, 12, 6)
        row.addWidget(QtWidgets.QLabel("Громкость"))
        self.volume = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setFixedWidth(110)
        self.volume.setAccessibleName("Громкость в трее")
        self.volume.setValue(self.panel.volume_slider.value())
        self.volume_value = QtWidgets.QLabel(f"{self.volume.value()}%")
        self.volume_value.setMinimumWidth(35)
        row.addWidget(self.volume)
        row.addWidget(self.volume_value)
        self.volume_action.setDefaultWidget(volume_widget)
        self.menu.addAction(self.volume_action)
        self.volume.valueChanged.connect(self.panel.volume_slider.setValue)
        self.panel.volume_slider.valueChanged.connect(self.volume.setValue)
        self.volume.valueChanged.connect(lambda value: self.volume_value.setText(f"{value}%"))
        self.volume.sliderReleased.connect(self.panel.save_preferences)
        self.menu.addSeparator()
        self.widgets_menu = self.menu.addMenu("Виджеты")
        self.widgets_menu.addActions(list(window.widget_actions.values()))
        self.widget_action = window.widget_actions["beat"]
        self.exit_action = self.menu.addAction("Выход", window.request_exit)
        window.lightSourceChanged.connect(self.sync)
        window.mode_combo.currentIndexChanged.connect(self.sync)
        self.panel.preset_combo.currentIndexChanged.connect(self.sync)
        self.panel.music_color_mode.currentIndexChanged.connect(self.sync)
        self.panel.player.playbackChanged.connect(self.sync)
        self.panel.player.trackChanged.connect(self.sync)
        self.sync()
        if QtWidgets.QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.show()

    @staticmethod
    def _combo_actions(menu, combo):
        group = QtWidgets.QActionGroup(menu)
        actions = []
        for index in range(combo.count()):
            action = menu.addAction(combo.itemText(index))
            action.setCheckable(True)
            group.addAction(action)
            action.triggered.connect(lambda _checked=False, row=index: combo.setCurrentIndex(row))
            actions.append(action)
        return actions

    def sync(self, *_args):
        music = self.window.light_source == "player"
        for key, action in self.sources.items():
            action.setChecked(key == self.window.light_source)
        for row, action in enumerate(self.live_modes):
            action.setChecked(row == self.window.mode_combo.currentIndex())
        for row, action in enumerate(self.presets):
            action.setChecked(row == self.panel.preset_combo.currentIndex())
        for row, action in enumerate(self.music_color_modes):
            action.setChecked(row == self.panel.music_color_mode.currentIndex())
        self.live_menu.menuAction().setVisible(not music)
        self.colors_menu.menuAction().setVisible(not music)
        self.live_toggle.setVisible(not music)
        self.live_toggle.setText("Остановить захват" if self.window.timer.isActive() else "Запустить захват")
        for action in (self.preset_menu.menuAction(), self.music_colors_menu.menuAction(), self.music_previous, self.music_pause,
                       self.music_next, self.volume_action):
            action.setVisible(music)
        self.music_pause.setText("Пауза" if self.panel.player.want_play else "Воспроизвести")
        self.music_pause.setIcon(self.panel.play_pause_button.icon())
        self.music_previous.setIcon(self.panel.previous_button.icon())
        self.music_next.setIcon(self.panel.next_button.icon())
        self.music_previous.setEnabled(bool(self.panel.player.current_track))
        self.music_next.setEnabled(bool(self.panel.player.current_track))
        track = self.panel.library.track(self.panel.player.current_track)
        title = track["title"] if music and track else "Музыка" if music else "Реальное время"
        self.title.setText(title)
        self.tray.setToolTip(("Music Light · " + title)[:127])

    def toggle_live(self):
        if self.window.timer.isActive():
            self.window.stop_stream()
        else:
            self.window.start_stream()
        self.sync()

    def choose_color(self, button):
        if self.color_dialog is not None:
            self.color_dialog.raise_()
            self.color_dialog.activateWindow()
            return
        dialog = QtWidgets.QColorDialog(QtGui.QColor(*button.color), self.window)
        dialog.setWindowTitle(button.text())
        dialog.setOption(QtWidgets.QColorDialog.DontUseNativeDialog, True)
        dialog.colorSelected.connect(lambda color: button.set_color((color.red(), color.green(), color.blue())))
        dialog.finished.connect(self._color_closed)
        self.color_dialog = dialog
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _color_closed(self, _result):
        dialog, self.color_dialog = self.color_dialog, None
        if dialog:
            dialog.deleteLater()

    def _activated(self, reason):
        if reason == QtWidgets.QSystemTrayIcon.Trigger:
            self.menu.popup(QtGui.QCursor.pos())
        elif reason == QtWidgets.QSystemTrayIcon.DoubleClick:
            self.menu.hide()
            self.show_window()

    def show_window(self):
        self.window.showNormal()
        self.window.raise_()
        self.window.activateWindow()

    def close(self):
        self.tray.hide()
        self.menu.close()
        if self.color_dialog:
            self.color_dialog.reject()
