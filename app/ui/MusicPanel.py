import asyncio
from pathlib import Path
import random
import uuid

import numpy as np
from PyQt5 import QtCore, QtGui, QtWidgets

from music.analysis import ANALYSIS_VERSION
from music.jobs import MusicJobs
from music.matching import source_details
from music.player import MusicPlayer
from music.scheduler import LightShowScheduler
from music.storage import DEFAULT_MUSIC_DIR, MusicLibrary, SUPPORTED_EXTENSIONS
from music.timeline import PRESETS, show_settings, timeline_key
from ui.PlaylistTable import PlaylistTable
from ui.ImportDialog import ImportDialog


def duration_text(seconds):
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}:{minutes:02}:{seconds:02}" if hours else f"{minutes}:{seconds:02}"


class LibraryTable(QtWidgets.QTableWidget):
    filesDropped = QtCore.pyqtSignal(list)

    def __init__(self):
        super().__init__(0, 7)
        self.setAcceptDrops(True)
        self.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.setHorizontalHeaderLabels(["Название", "Исполнитель", "Альбом", "Длительность", "BPM", "Анализ", "Прогресс"])
        self.horizontalHeader().setSectionResizeMode(0, QtWidgets.QHeaderView.Stretch)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        self.dragEnterEvent(event)

    def dropEvent(self, event):
        paths = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
        self.filesDropped.emit(paths)
        event.acceptProposedAction()


class MusicPanel(QtWidgets.QWidget):
    sourceRequested = QtCore.pyqtSignal(str)
    colorsReady = QtCore.pyqtSignal(dict)
    previewReady = QtCore.pyqtSignal(object, float, object)
    beatReady = QtCore.pyqtSignal(float, float, bool, float)
    invalidateRequested = QtCore.pyqtSignal()
    rateChanged = QtCore.pyqtSignal(int)
    latencyMeasured = QtCore.pyqtSignal(list)

    def __init__(self, lamp_ids, controller=None, async_loop=None, root=None, backend=None, parent=None):
        super().__init__(parent)
        self.lamp_ids, self.controller, self.async_loop = list(lamp_ids), controller, async_loop
        self.library = MusicLibrary(root or DEFAULT_MUSIC_DIR)
        self.preferences = self.library.settings()
        self.settings = show_settings(self.preferences.get("show", {}), self.lamp_ids)
        self.groups = dict(self.preferences.get("groups") or {})
        self.active = False
        self.closing = False
        self.building = False
        self.timeline_token = None
        self.timeline_track = None
        self.timeline_hash = None
        self.selected_playlist = self.preferences.get("playlist_id")
        self.active_playlist = None
        self.measure_future = None
        self.import_dialog = None
        self.text_import_dialog = None
        self.replacing = set()
        self.player = MusicPlayer(self.library.track, self, backend)
        self.player.queue.repeat = "off"
        self.player.queue.shuffle = bool(self.preferences.get("shuffle", False))
        self.scheduler = LightShowScheduler(self.player, self.lamp_ids, self.settings, self)
        self.scheduler.colorsReady.connect(self.colorsReady)
        self.scheduler.sceneInvalidated.connect(self.invalidateRequested)
        self.scheduler.previewReady.connect(self._preview)
        self.scheduler.beatReady.connect(self.beatReady)
        self.playlist_data_root = DEFAULT_MUSIC_DIR.parent if root is None else self.library.root
        self.jobs = MusicJobs(self.library.root, self, playlist_root=self.playlist_data_root / "playlists")
        self.jobs.progress.connect(self._progress)
        self.jobs.analysed.connect(self._analysed)
        self.jobs.failed.connect(self._failed)
        self.jobs.cancelled.connect(self._cancelled)
        self.jobs.timelineReady.connect(self._timeline_ready)
        self.jobs.filesFound.connect(self._files_found)
        self.jobs.filesImported.connect(self._files_found)
        self.jobs.playlistFound.connect(self._playlist_found)
        self.jobs.catalogFound.connect(self._catalog_found)
        self.jobs.downloaded.connect(self._downloaded)
        self.jobs.textImported.connect(self._text_imported)
        self.jobs.importProgress.connect(self._import_progress)
        self.jobs.replacementReady.connect(self._replacement_ready)
        self.jobs.replacementFailed.connect(self._replacement_failed)
        self.jobs.replacementProgress.connect(self._replacement_progress)
        self.settings_timer = QtCore.QTimer(self)
        self.settings_timer.setSingleShot(True)
        self.settings_timer.setInterval(180)
        self.settings_timer.timeout.connect(self.apply_show_settings)
        self._build_ui()
        self.player.trackRequested.connect(self.prepare_track)
        self.player.preparationRequested.connect(self.prepare_track)
        self.player.trackChanged.connect(self._track_changed)
        self.player.queueChanged.connect(self._queue_changed)
        self.player.playbackChanged.connect(self._playback_changed)
        self.player.positionChanged.connect(self._position_changed)
        self.player.durationChanged.connect(self._duration_changed)
        self.player.error.connect(self.status.setText)
        self.player.repeatChanged.connect(self._repeat_changed)
        self.player.listenCompleted.connect(self._listen_completed)
        self.latencyMeasured.connect(self._latency_measured)
        self.jobs.queueChanged.connect(self._jobs_changed)
        self.refresh_library()
        self.refresh_playlists()
        self.jobs.start()
        self.playlist_directories = [self.playlist_data_root]
        if root is None:
            self.playlist_directories.append(Path(__file__).resolve().parents[2] / "data")
        self.jobs.load_playlists(self.playlist_directories)
        # Hash/version validation runs in the managed worker, not on the GUI thread.
        for track in self.library.tracks():
            if not track.get("remote") and (track.get("status") != "Error" or not track.get("error")):
                self.enqueue_analysis(track)
        if self.library.load_error:
            self.status.setText(self.library.load_error)

    def _button(self, label, callback):
        button = QtWidgets.QPushButton(label)
        button.clicked.connect(callback)
        return button

    @QtCore.pyqtSlot(int)
    def _jobs_changed(self, count):
        if not self.closing:
            self.queue_status.setText(f"Задач в очереди: {count}")

    def _build_ui(self):
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        # The main window places these surfaces in its header and top-level tabs.
        self.library_page = self._build_library()
        self.playlists_page = self._build_playlists()
        self.player_header = self._build_player_header()
        self.settings_page = self._build_settings()
        for page in (self.library_page, self.playlists_page, self.player_header, self.settings_page):
            page.setParent(self)
            page.hide()
        bottom = QtWidgets.QHBoxLayout()
        self.status = QtWidgets.QLabel("Добавьте музыку: анализ и световой таймлайн создаются автоматически")
        self.status.setWordWrap(True)
        self.queue_status = QtWidgets.QLabel("Задач в очереди: 0")
        bottom.addWidget(self.status, 1)
        bottom.addWidget(self.queue_status)
        layout.addLayout(bottom)

    def _build_library(self):
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)
        row = QtWidgets.QHBoxLayout()
        for label, callback in (("Добавить файлы", self.choose_files), ("Добавить папку", self.choose_directory),
                                ("Удалить", self.remove_selected), ("Отменить анализ", self.cancel_selected),
                                ("Повторить анализ", self.retry_selected), ("Play", self.play_library_selection)):
            row.addWidget(self._button(label, callback))
        layout.addLayout(row)
        self.library_table = LibraryTable()
        self._connect_track_menu(self.library_table)
        self.library_table.filesDropped.connect(self.import_paths)
        self.library_table.cellDoubleClicked.connect(lambda _row, _column: self.play_library_selection())
        layout.addWidget(self.library_table)
        row = QtWidgets.QHBoxLayout()
        self.add_playlist_combo = QtWidgets.QComboBox()
        row.addWidget(QtWidgets.QLabel("Добавить выделенные треки:"))
        row.addWidget(self.add_playlist_combo, 1)
        row.addWidget(self._button("В плейлист", self.add_to_playlist))
        row.addWidget(self._button("Другая запись", self.replace_library_recording))
        layout.addLayout(row)
        return page

    def _build_playlists(self):
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(self._button("Создать", self.create_playlist))
        row.addWidget(self._button("Удалить плейлист", self.delete_playlist))
        row.addWidget(self._button("Play playlist", self.play_playlist))
        row.addWidget(self._button("Микс", lambda: self.play_playlist(mix=True)))
        row.addWidget(self._button("Импорт", self.open_import))
        row.addWidget(self._button("Добавить файлы", lambda: self.choose_files(self.selected_playlist)))
        row.addStretch(1)
        layout.addLayout(row)
        self.playlist_lists = {}
        self.empty_playlist_list = PlaylistTable(self)
        self.empty_playlist_list.hide()
        self.playlist_tracks = self.empty_playlist_list
        self.playlist_tabs = QtWidgets.QTabWidget()
        self.playlist_tabs.setMovable(True)
        self.playlist_tabs.currentChanged.connect(self._playlist_tab_changed)
        layout.addWidget(self.playlist_tabs)
        hint = QtWidgets.QLabel("Двойной щелчок: найти → скачать → проанализировать → играть. Скачанные треки доступны без сети.")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        return page

    def _build_player_header(self):
        page = QtWidgets.QFrame()
        page.setObjectName("playerHeader")
        page.setFrameShape(QtWidgets.QFrame.StyledPanel)
        layout = QtWidgets.QVBoxLayout(page)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(6)
        header = QtWidgets.QHBoxLayout()
        self.cover = QtWidgets.QLabel("♫")
        self.cover.setAlignment(QtCore.Qt.AlignCenter)
        self.cover.setFixedSize(52, 52)
        header.addWidget(self.cover)
        info = QtWidgets.QVBoxLayout()
        info.setSpacing(1)
        self.now_title = QtWidgets.QLabel("Ничего не воспроизводится")
        self.now_title.setStyleSheet("font-weight: 600;")
        self.now_title.setMinimumWidth(130)
        self.now_title.setMaximumHeight(36)
        self.now_title.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred)
        self.now_title.setTextFormat(QtCore.Qt.PlainText)
        self.now_title.setWordWrap(True)
        self.now_artist = QtWidgets.QLabel("")
        self.now_artist.setTextFormat(QtCore.Qt.PlainText)
        self.now_artist.setWordWrap(True)
        self.now_artist.setMaximumHeight(32)
        self.now_artist.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred)
        self.now_details = QtWidgets.QLabel("BPM: — · энергия: 0%")
        self.now_details.setObjectName("muted")
        self.now_details.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred)
        self.now_state = QtWidgets.QLabel("Остановлено")
        info.addWidget(self.now_title)
        info.addWidget(self.now_artist)
        info.addWidget(self.now_details)
        header.addLayout(info, 1)
        self.previous_button = self._transport_button("Предыдущая песня", QtWidgets.QStyle.SP_MediaSkipBackward, self.player.previous)
        self.play_pause_button = self._transport_button("Воспроизвести", QtWidgets.QStyle.SP_MediaPlay, self.toggle_playback)
        self.next_button = self._transport_button("Следующая песня", QtWidgets.QStyle.SP_MediaSkipForward, self.player.next)
        self.stop_button = self._transport_button("Остановить", QtWidgets.QStyle.SP_MediaStop, self.player.stop)
        for button in (self.previous_button, self.play_pause_button, self.next_button, self.stop_button):
            header.addWidget(button)
        volume_label = QtWidgets.QLabel("Громкость")
        header.addWidget(volume_label)
        self.volume_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(int(self.preferences.get("volume", 75)))
        self.volume_slider.setFixedWidth(100)
        self.volume_slider.setAccessibleName("Громкость музыки")
        self.volume_slider.setToolTip("Громкость музыки")
        self.player.backend.setVolume(self.volume_slider.value())
        self.volume_slider.valueChanged.connect(self.player.backend.setVolume)
        self.volume_slider.sliderReleased.connect(self.save_preferences)
        header.addWidget(self.volume_slider)
        self.repeat_button = QtWidgets.QToolButton()
        self.repeat_button.setIcon(self._transport_icon(QtWidgets.QStyle.SP_BrowserReload))
        self.repeat_button.setToolButtonStyle(QtCore.Qt.ToolButtonTextBesideIcon)
        self.repeat_button.setCheckable(True)
        self.repeat_button.clicked.connect(self.player.add_repeat)
        self.repeat_button.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.repeat_button.customContextMenuRequested.connect(lambda _point: self.player.clear_repeat())
        self._repeat_changed(0)
        self.shuffle_check = QtWidgets.QCheckBox("Микс")
        self.shuffle_check.setChecked(self.player.queue.shuffle)
        self.shuffle_check.toggled.connect(self._shuffle_changed)
        header.addWidget(self.repeat_button)
        header.addWidget(self.shuffle_check)
        self.queue_button = QtWidgets.QToolButton()
        self.queue_button.setText("Очередь")
        self.queue_button.setCheckable(True)
        header.addWidget(self.queue_button)
        layout.addLayout(header)
        self.seek_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.seek_slider.setRange(0, 0)
        self.seek_slider.setAccessibleName("Позиция в песне")
        self.seek_slider.sliderReleased.connect(lambda: self.player.seek(self.seek_slider.value() / 1000))
        self.time_label = QtWidgets.QLabel("0:00 / 0:00")
        row = QtWidgets.QHBoxLayout()
        row.addWidget(self.now_state)
        row.addWidget(self.seek_slider, 1)
        row.addWidget(self.time_label)
        layout.addLayout(row)
        self.playback_queue = QtWidgets.QListWidget()
        self.playback_queue.setMaximumHeight(135)
        self.playback_queue.setMinimumHeight(80)
        self.playback_queue.hide()
        self.queue_button.toggled.connect(self.playback_queue.setVisible)
        self.playback_queue.itemDoubleClicked.connect(self._jump_queue)
        layout.addWidget(self.playback_queue)
        return page

    def _transport_button(self, title, icon, callback):
        button = QtWidgets.QToolButton()
        button.setIcon(self._transport_icon(icon))
        button.setToolTip(title)
        button.setAccessibleName(title)
        button.setFixedSize(34, 34)
        button.clicked.connect(lambda _checked=False: callback())
        return button

    @staticmethod
    def _transport_icon(kind):
        # Platform media icons can be black on the application's dark theme.
        pixmap = QtGui.QPixmap(20, 20)
        pixmap.fill(QtCore.Qt.transparent)
        painter = QtGui.QPainter(pixmap)
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        painter.setPen(QtCore.Qt.NoPen)
        painter.setBrush(QtGui.QColor("#edf2f7"))
        if kind == QtWidgets.QStyle.SP_MediaPlay:
            points = [(6, 3), (17, 10), (6, 17)]
        elif kind == QtWidgets.QStyle.SP_MediaPause:
            painter.drawRect(5, 3, 4, 14)
            painter.drawRect(12, 3, 4, 14)
            points = []
        elif kind == QtWidgets.QStyle.SP_MediaStop:
            painter.drawRoundedRect(QtCore.QRectF(4, 4, 12, 12), 1, 1)
            points = []
        elif kind == QtWidgets.QStyle.SP_BrowserReload:
            painter.setPen(QtGui.QPen(QtGui.QColor("#edf2f7"), 2))
            painter.drawArc(QtCore.QRectF(3, 4, 14, 13), 45 * 16, 280 * 16)
            painter.setPen(QtCore.Qt.NoPen)
            points = [(17, 2), (17, 9), (11, 5)]
        else:
            if kind == QtWidgets.QStyle.SP_MediaSkipForward:
                painter.translate(20, 0)
                painter.scale(-1, 1)
            painter.drawRect(3, 3, 3, 14)
            points = [(16, 3), (6, 10), (16, 17)]
        if points:
            painter.drawPolygon(QtGui.QPolygonF([QtCore.QPointF(x, y) for x, y in points]))
        painter.end()
        return QtGui.QIcon(pixmap)

    def toggle_playback(self):
        if self.player.want_play:
            self.player.pause()
        else:
            self.play()

    def _build_settings(self):
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)
        form = QtWidgets.QFormLayout()
        self.preset_combo = QtWidgets.QComboBox()
        self.preset_combo.addItems(PRESETS)
        self.preset_combo.setCurrentText(self.settings["preset"])
        self.intensity_spin = QtWidgets.QDoubleSpinBox()
        self.intensity_spin.setRange(0, 1.5)
        self.intensity_spin.setSingleStep(0.1)
        self.intensity_spin.setValue(self.settings["intensity"])
        self.brightness_spin = QtWidgets.QSpinBox()
        self.brightness_spin.setRange(0, 100)
        self.brightness_spin.setSuffix(" %")
        self.brightness_spin.setValue(round(self.settings["max_brightness"] * 100))
        self.update_spin = QtWidgets.QSpinBox()
        self.update_spin.setRange(1, 30)
        self.update_spin.setSuffix(" /с")
        self.update_spin.setValue(self.settings["update_hz"])
        self.latency_spin = QtWidgets.QSpinBox()
        self.latency_spin.setRange(-2000, 2000)
        self.latency_spin.setSuffix(" мс")
        self.latency_spin.setValue(self.settings["latency_ms"])
        for label, widget in (("Световой пресет", self.preset_combo), ("Интенсивность", self.intensity_spin),
                              ("Максимальная яркость", self.brightness_spin),
                              ("Обновления ламп", self.update_spin),
                              ("Компенсация задержки", self.latency_spin)):
            form.addRow(label, widget)
        layout.addLayout(form)
        row = QtWidgets.QHBoxLayout()
        self.group_combo = QtWidgets.QComboBox()
        row.addWidget(QtWidgets.QLabel("Группа ламп"))
        row.addWidget(self.group_combo, 1)
        layout.addLayout(row)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(self._button("Группа из отмеченных", self.create_group))
        self.measure_button = self._button("Измерить задержку", self.measure_latency)
        self.measure_button.setEnabled(bool(self.controller and self.async_loop))
        row.addWidget(self.measure_button)
        layout.addLayout(row)
        self.lamp_table = QtWidgets.QTableWidget(len(self.lamp_ids), 5)
        self.lamp_table.setHorizontalHeaderLabels(["Вкл", "Лампа", "Группа", "RTT, мс", "Компенсация, мс"])
        for column in (0, 2, 3):
            self.lamp_table.horizontalHeader().setSectionResizeMode(column, QtWidgets.QHeaderView.ResizeToContents)
        self.lamp_table.horizontalHeader().setSectionResizeMode(1, QtWidgets.QHeaderView.Stretch)
        self.lamp_table.horizontalHeader().setSectionResizeMode(4, QtWidgets.QHeaderView.ResizeToContents)
        self.offset_spins = {}
        for row, lamp in enumerate(self.lamp_ids):
            enabled = QtWidgets.QTableWidgetItem()
            enabled.setFlags(QtCore.Qt.ItemIsEnabled | QtCore.Qt.ItemIsUserCheckable)
            enabled.setCheckState(QtCore.Qt.Checked if lamp in self.settings["lamp_ids"] else QtCore.Qt.Unchecked)
            self.lamp_table.setItem(row, 0, enabled)
            item = QtWidgets.QTableWidgetItem(lamp)
            item.setFlags(QtCore.Qt.ItemIsEnabled | QtCore.Qt.ItemIsSelectable)
            self.lamp_table.setItem(row, 1, item)
            self.lamp_table.setItem(row, 2, QtWidgets.QTableWidgetItem(self.groups.get(lamp, "Все")))
            measured = QtWidgets.QTableWidgetItem("—")
            measured.setFlags(QtCore.Qt.ItemIsEnabled | QtCore.Qt.ItemIsSelectable)
            self.lamp_table.setItem(row, 3, measured)
            offset = QtWidgets.QSpinBox()
            offset.setRange(-2000, 2000)
            offset.setValue(self.settings["lamp_offsets"].get(lamp, 0))
            offset.valueChanged.connect(self._settings_changed)
            self.offset_spins[lamp] = offset
            self.lamp_table.setCellWidget(row, 4, offset)
        layout.addWidget(self.lamp_table, 1)
        hint = QtWidgets.QLabel("Положительная компенсация отправляет свет заранее. Измерение оценивает половину сетевого RTT; реакцию лампы можно уточнить вручную.")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self._refresh_groups()
        self.group_combo.currentIndexChanged.connect(self._group_selected)
        self.lamp_table.itemChanged.connect(self._lamp_item_changed)
        self.preset_combo.currentIndexChanged.connect(self._settings_changed)
        for widget in (self.intensity_spin, self.brightness_spin, self.update_spin, self.latency_spin):
            widget.valueChanged.connect(self._settings_changed)
        return page

    def selected_track_ids(self):
        return [self.library_table.item(index.row(), 0).data(QtCore.Qt.UserRole)
                for index in self.library_table.selectionModel().selectedRows()]

    def refresh_library(self):
        selected = set(self.selected_track_ids()) if self.library_table.rowCount() else set()
        tracks = self.library.tracks()
        self.library_table.setRowCount(len(tracks))
        for row, track in enumerate(tracks):
            bpm = f"{track.get('bpm', 0):.1f}" if track.get("bpm") else "—"
            values = [track["title"], track.get("artist") or "—", track.get("album") or "—",
                      duration_text(track.get("duration", 0)), bpm, track["status"]]
            for column, value in enumerate(values):
                item = QtWidgets.QTableWidgetItem(str(value))
                item.setToolTip(track.get("error") or track["path"])
                if column == 0:
                    item.setData(QtCore.Qt.UserRole, track["id"])
                self.library_table.setItem(row, column, item)
            if track["status"] == "NotDownloaded":
                self.library_table.removeCellWidget(row, 6)
                self.library_table.setItem(row, 6, QtWidgets.QTableWidgetItem("—"))
            else:
                bar = QtWidgets.QProgressBar()
                bar.setRange(0, 100)
                bar.setValue(int(track.get("progress", 0)))
                self.library_table.setCellWidget(row, 6, bar)
            if track["id"] in selected:
                self.library_table.selectionModel().select(self.library_table.model().index(row, 0),
                    QtCore.QItemSelectionModel.Select | QtCore.QItemSelectionModel.Rows)

    def refresh_playlists(self):
        self.building = True
        playlists = self.library.playlists()
        for combo in (self.add_playlist_combo,):
            old = combo.currentData()
            combo.clear()
            for playlist in playlists:
                combo.addItem(playlist["name"], playlist["id"])
            target = old or self.selected_playlist
            index = combo.findData(target)
            if index >= 0:
                combo.setCurrentIndex(index)
        known = {playlist["id"] for playlist in playlists}
        for playlist_id in list(self.playlist_lists):
            if playlist_id not in known:
                widget = self.playlist_lists.pop(playlist_id)
                self.playlist_tabs.removeTab(self.playlist_tabs.indexOf(widget))
                widget.deleteLater()
        for playlist in playlists:
            widget = self.playlist_lists.get(playlist["id"])
            if widget is None:
                widget = PlaylistTable()
                widget.setProperty("playlist_id", playlist["id"])
                self._connect_track_menu(widget)
                widget.orderChanged.connect(self._playlist_order_changed)
                widget.cellDoubleClicked.connect(lambda _row, _column: self.play_playlist())
                self.playlist_lists[playlist["id"]] = widget
                self.playlist_tabs.addTab(widget, playlist["name"])
            self.playlist_tabs.setTabText(self.playlist_tabs.indexOf(widget), playlist["name"])
        if self.selected_playlist in self.playlist_lists:
            self.playlist_tabs.setCurrentWidget(self.playlist_lists[self.selected_playlist])
        self.building = False
        self._playlist_selected()

    def _playlist_tab_changed(self, index):
        if self.building or index < 0:
            return
        self._playlist_selected()

    def _catalog_found(self, document):
        self._playlist_found(document, from_catalog=True)

    def _playlist_found(self, document, from_catalog=False):
        if self.closing:
            return
        playlist = self.library.import_html_playlist(document, from_catalog=from_catalog)
        if playlist is None:
            return
        self.refresh_library()
        self.refresh_playlists()
        self.status.setText(f"Плейлист {playlist['name']}: {len(playlist['tracks'])} треков. Загрузка начнётся при воспроизведении.")

    def _playlist_selected(self):
        if self.building:
            return
        widget = self.playlist_tabs.currentWidget()
        self.selected_playlist = widget.property("playlist_id") if widget else None
        playlist = next((item for item in self.library.playlists() if item["id"] == self.selected_playlist), None)
        self.building = True
        self.playlist_tracks = self.playlist_lists.get(self.selected_playlist, self.empty_playlist_list)
        tab = self.playlist_tabs.indexOf(self.playlist_tracks)
        if tab >= 0:
            self.playlist_tabs.setCurrentIndex(tab)
        if playlist:
            tracks = {track["id"]: track for track in self.library.tracks()}
            self.playlist_tracks.fill(playlist["tracks"], tracks)
        self.building = False

    def choose_files(self, playlist_id=None):
        extensions = " ".join("*" + extension for extension in sorted(SUPPORTED_EXTENSIONS))
        self._open_import_dialog(QtWidgets.QFileDialog.ExistingFiles, f"Аудио ({extensions})", playlist_id or None)

    def choose_playlist_import(self):
        self._open_import_dialog(QtWidgets.QFileDialog.ExistingFiles,
                                 "Плейлисты (*.txt *.html *.htm *.json)", import_playlist=True)

    def open_import(self):
        if self.text_import_dialog is not None:
            self.text_import_dialog.raise_()
            return
        dialog = ImportDialog(self)
        self.text_import_dialog = dialog
        dialog.importRequested.connect(self.import_text)
        dialog.filesRequested.connect(self._import_from_file)
        dialog.finished.connect(self._text_import_closed)
        dialog.open()

    def _import_from_file(self):
        self.text_import_dialog.accept()
        self.choose_playlist_import()

    def _text_import_closed(self, _result):
        dialog, self.text_import_dialog = self.text_import_dialog, None
        if dialog:
            dialog.deleteLater()

    def import_text(self, text):
        if not self.closing and text.strip():
            self.status.setText("Импорт музыки в фоне…")
            self.jobs.import_text(text, self.selected_playlist or "")

    @QtCore.pyqtSlot(str)
    def _import_progress(self, message):
        if not self.closing:
            self.status.setText(message)

    def _text_imported(self, playlist_id, result):
        if self.closing:
            return
        entries = result["entries"]
        added = 0
        if entries:
            if not any(item["id"] == playlist_id for item in self.library.playlists()):
                playlist_id = self.library.create_playlist("Импорт")["id"]
                self.selected_playlist = playlist_id
            added = self.library.add_remote_entries(playlist_id, entries)
            self.refresh_library()
            self.refresh_playlists()
            if playlist_id == self.active_playlist:
                playlist = next(item for item in self.library.playlists() if item["id"] == playlist_id)
                self.player.replace_queue(playlist["tracks"])
        errors = result["errors"]
        self.status.setText(f"Импорт: {len(result['documents'])} плейлистов, добавлено {added} песен, ошибок: {len(errors)}.")
        self.status.setToolTip("\n".join(errors))
        if errors:
            dialog = QtWidgets.QMessageBox(QtWidgets.QMessageBox.Warning, "Результат импорта",
                "Некоторые строки не импортированы. Остальные добавлены.", parent=self)
            dialog.setDetailedText("\n".join(errors))
            dialog.setAttribute(QtCore.Qt.WA_DeleteOnClose)
            self.import_result_dialog = dialog
            dialog.open()

    def choose_directory(self):
        self._open_import_dialog(QtWidgets.QFileDialog.Directory)

    def _open_import_dialog(self, mode, name_filter=None, playlist_id=None, import_playlist=False):
        if self.closing:
            return
        if self.import_dialog is not None:
            self.import_dialog.raise_()
            self.import_dialog.activateWindow()
            return
        dialog = QtWidgets.QFileDialog(self, "Папка с музыкой" if mode == QtWidgets.QFileDialog.Directory else "Добавить музыку")
        # The Windows shell dialog can hang while enumerating shell extensions
        # and network locations. Qt's file model enumerates directories in a worker.
        dialog.setOption(QtWidgets.QFileDialog.DontUseNativeDialog, True)
        dialog.setOption(QtWidgets.QFileDialog.DontUseCustomDirectoryIcons, True)
        dialog.setFileMode(mode)
        if mode == QtWidgets.QFileDialog.Directory:
            dialog.setOption(QtWidgets.QFileDialog.ShowDirsOnly, True)
        if name_filter:
            dialog.setNameFilter(name_filter)
        if import_playlist:
            dialog.setWindowTitle("Импорт плейлиста из HTML / JSON")
            dialog.filesSelected.connect(self.jobs.import_playlists)
        else:
            dialog.filesSelected.connect(lambda paths: self.import_paths(paths, playlist_id))
        dialog.finished.connect(self._import_dialog_finished)
        self.import_dialog = dialog
        dialog.open()

    def _import_dialog_finished(self, _result):
        dialog, self.import_dialog = self.import_dialog, None
        if dialog is not None:
            dialog.deleteLater()

    def import_paths(self, paths, playlist_id=None):
        if self.closing or not paths:
            return
        self.status.setText("Добавление музыки: проверка файлов в фоне…")
        self.jobs.import_paths(paths, playlist_id)

    def _files_found(self, paths, playlist_id=None):
        if self.closing:
            return
        added = self.library.add_files(paths, validated=True)
        if playlist_id:
            playlist = next((item for item in self.library.playlists() if item["id"] == playlist_id), None)
            if playlist:
                by_path = {track["path"]: track["id"] for track in self.library.tracks() if track["path"]}
                track_ids = list(dict.fromkeys(playlist["tracks"] + [by_path[path] for path in paths if path in by_path]))
                self.library.set_playlist_tracks(playlist_id, track_ids)
                if playlist_id == self.active_playlist:
                    self.player.replace_queue(track_ids)
                self.refresh_playlists()
        for track in added:
            self.enqueue_analysis(track)
        self.refresh_library()
        self.status.setText(f"Добавлено: {len(added)}. Анализ выполняется в фоне.")

    def enqueue_analysis(self, track, priority=False):
        if track.get("status") != "Pending" or track.get("progress") or track.get("error"):
            self.library.update_track(track["id"], status="Pending", progress=0, error="")
        self.jobs.analyse(track, dict(self.settings), priority=priority)

    def _downloaded(self, track_id, fields):
        if not self.closing and self.library.track(track_id):
            self.library.update_track(track_id, **fields)

    def _progress(self, track_id, percent, stage):
        if self.closing or not self.library.track(track_id):
            return
        track = self.library.track(track_id)
        if track["status"] != "Ready":
            # Progress need not fsync the whole library on every decoded block.
            for item in self.library.data["tracks"]:
                if item["id"] == track_id:
                    item.update(status="Analyzing", progress=percent)
                    for table in self.playlist_lists.values():
                        table.update_track(item)
            for row in range(self.library_table.rowCount()):
                if self.library_table.item(row, 0).data(QtCore.Qt.UserRole) == track_id:
                    self.library_table.item(row, 5).setText("Analyzing")
                    bar = self.library_table.cellWidget(row, 6)
                    if bar is None:
                        bar = QtWidgets.QProgressBar()
                        self.library_table.setCellWidget(row, 6, bar)
                    bar.setValue(percent)
        self.status.setText(f"{track['title']}: {stage} · {percent}%")

    def _analysed(self, track_id, metadata):
        if self.closing or not self.library.track(track_id):
            return
        fields = {key: metadata.get(key) for key in ("title", "artist", "album", "duration", "bpm",
                  "tempo_confidence", "content_id", "cache_path", "playback_path", "cover_path", "timeline_path")}
        for key in ("path", "resolved_url", "resolved_title", "resolved_source", "source_codec", "source_bitrate_kbps",
                    "source_sample_rate", "source_duration", "search_query", "selection_version", "search_by_name",
                    "download_revision", "excluded_urls"):
            if key in metadata:
                fields[key] = metadata[key]
        custom_title = self.library.track(track_id).get("custom_title")
        if custom_title:
            fields["title"] = custom_title
        self.library.update_track(track_id, **fields, analysis_version=ANALYSIS_VERSION,
                                  status="Ready", progress=100, error="")
        self.refresh_library()
        self._playlist_selected()
        self.player.track_updated(track_id)
        self.status.setText(f"{fields['title']}: Ready")

    def _failed(self, track_id, message):
        if self.closing:
            return
        track = self.library.track(track_id)
        if track and track["status"] != "Ready":
            self.library.update_track(track_id, status="Error", error=message, progress=0)
            self.refresh_library()
            self._playlist_selected()
            self.player.track_updated(track_id)
        self.status.setText(f"Ошибка: {message}")

    def _cancelled(self, track_id):
        if self.library.track(track_id):
            self.library.update_track(track_id, status="Pending", progress=0, error="Анализ отменён")
            if not self.closing:
                self.refresh_library()

    def remove_selected(self):
        self._remove_tracks(self.selected_track_ids())

    def _remove_tracks(self, track_ids):
        for track_id in track_ids:
            self.jobs.cancel_track(track_id)
        if self.player.current_track in track_ids:
            self.player.stop()
            self.player.set_queue([])
        self.library.remove_tracks(track_ids)
        self.player.replace_queue([track for track in self.player.queue.tracks if track not in track_ids])
        self.refresh_library()
        self.refresh_playlists()

    def cancel_selected(self):
        self._cancel_tracks(self.selected_track_ids())

    def _cancel_tracks(self, track_ids):
        for track_id in track_ids:
            self.jobs.cancel_track(track_id)

    def retry_selected(self):
        self._analyse_tracks(self.selected_track_ids())

    def _analyse_tracks(self, track_ids):
        for track_id in track_ids:
            track = self.library.track(track_id)
            if track:
                self.enqueue_analysis(track)
        self.refresh_library()
        self._playlist_selected()

    def _connect_track_menu(self, table):
        table.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        table.customContextMenuRequested.connect(lambda position: self._show_track_menu(table, position))

    def _show_track_menu(self, table, position):
        menu = self._track_context_menu(table, position)
        if menu is not None:
            menu.aboutToHide.connect(menu.deleteLater)
            menu.popup(table.viewport().mapToGlobal(position))

    def _track_context_menu(self, table, position):
        index = table.indexAt(position)
        if not index.isValid():
            return None
        playlist_id = table.property("playlist_id")
        if playlist_id:
            self.playlist_tabs.setCurrentWidget(table)
        if not table.selectionModel().isRowSelected(index.row(), QtCore.QModelIndex()):
            table.selectRow(index.row())
        table.selectionModel().setCurrentIndex(index, QtCore.QItemSelectionModel.NoUpdate)
        identities = [table.item(item.row(), 0).data(QtCore.Qt.UserRole)
                      for item in table.selectionModel().selectedRows()]
        menu = QtWidgets.QMenu(table)
        menu.addAction("Воспроизвести", self.play_playlist if playlist_id else self.play_library_selection)
        rename = menu.addAction("Переименовать", lambda: self.rename_track(identities[0]))
        rename.setEnabled(len(identities) == 1)
        menu.addAction("Проанализировать", lambda: self._analyse_tracks(identities))
        menu.addAction("Отменить анализ", lambda: self._cancel_tracks(identities))
        replacement = menu.addAction("Другая запись", lambda: self._replace_recordings(identities))
        replacement.setEnabled(any((track := self.library.track(identity)) and track.get("remote")
                                  and track.get("path") and track["status"] in ("Ready", "Error")
                                  and identity not in self.replacing for identity in identities))
        if playlist_id:
            menu.addSeparator()
            up = menu.addAction("Выше", lambda: self.move_playlist_track(-1))
            down = menu.addAction("Ниже", lambda: self.move_playlist_track(1))
            up.setEnabled(len(identities) == 1 and index.row() > 0)
            down.setEnabled(len(identities) == 1 and index.row() < table.rowCount() - 1)
        menu.addSeparator()
        if playlist_id:
            menu.addAction("Удалить из плейлиста", self.remove_playlist_track)
        else:
            menu.addAction("Удалить из библиотеки", lambda: self._remove_tracks(identities))
        return menu

    def rename_track(self, track_id):
        track = self.library.track(track_id)
        if not track:
            return
        title, accepted = QtWidgets.QInputDialog.getText(self, "Переименовать трек", "Название", text=track["title"])
        if not accepted or not title.strip():
            return
        self.library.rename_track(track_id, title)
        self.refresh_library()
        self._playlist_selected()
        if self.player.current_track == track_id:
            self.now_title.setText(title.strip())
            self.now_title.setToolTip(title.strip())
        self._queue_changed(self.player.queue.ordered_tracks())

    def replace_library_recording(self):
        self._replace_recordings(self.selected_track_ids())

    def replace_playlist_recording(self):
        table = self.playlist_tracks
        identities = [table.item(index.row(), 0).data(QtCore.Qt.UserRole)
                      for index in table.selectionModel().selectedRows()]
        self._replace_recordings(identities)

    def _replace_recordings(self, identities):
        queued = 0
        for identity in dict.fromkeys(identities):
            track = self.library.track(identity)
            if (not track or not track.get("remote") or not track.get("path")
                    or identity in self.replacing or track["status"] not in ("Ready", "Error")):
                continue
            self.replacing.add(identity)
            self.jobs.replace_recording(track, dict(self.settings))
            queued += 1
        self.status.setText(f"Поиск других записей в фоне: {queued}." if queued else
                            "Выберите уже скачанную из сети песню для поиска другой записи.")

    def _replacement_progress(self, identity, percent, stage):
        if self.closing or identity not in self.replacing:
            return
        track = self.library.track(identity)
        if track:
            self.status.setText(f"{track['title']}: {stage} · {percent}%")
            for table in self.playlist_lists.values():
                table.update_track(track | {"status": "Replacing"})

    def _replacement_failed(self, identity, message):
        self.replacing.discard(identity)
        if self.closing:
            return
        track = self.library.track(identity)
        if track:
            for table in self.playlist_lists.values():
                table.update_track(track)
        self.status.setText("Другая запись: " + message)

    def _replacement_ready(self, identity, metadata):
        self.replacing.discard(identity)
        if self.closing or not self.library.track(identity):
            return
        restart = (self.player.current_track == identity and self.player.prepared == identity
                   and self.player.state in ("playing", "paused"))
        self._analysed(identity, metadata)
        if restart:
            self.player.backend.stop()
            self.player.prepared = None
            self.player._request_current()
        self.status.setText(f"Новая запись: {metadata['title']} · {source_details(metadata)}")

    def create_playlist(self):
        name, accepted = QtWidgets.QInputDialog.getText(self, "Создать плейлист", "Название")
        if accepted and name.strip():
            playlist = self.library.create_playlist(name)
            self.building = True
            self.add_playlist_combo.setCurrentIndex(-1)
            self.building = False
            self.selected_playlist = playlist["id"]
            self.refresh_playlists()

    def add_search_track(self):
        if not self.selected_playlist:
            self.create_playlist()
        if not self.selected_playlist:
            return
        text, accepted = QtWidgets.QInputDialog.getText(self, "Добавить музыку", "Исполнитель — название (или только название):")
        if accepted and text.strip():
            artist, title = "", text.strip()
            for separator in (" — ", " – ", " - "):
                if separator in title:
                    artist, title = title.split(separator, 1)
                    break
            if not title.strip():
                self.status.setText("Введите название трека")
                return
            self.library.add_search_track(self.selected_playlist, title, artist)
            self.refresh_library()
            self.refresh_playlists()

    def delete_playlist(self):
        if self.selected_playlist:
            if self.selected_playlist == self.active_playlist:
                self.active_playlist = None
            self.library.remove_playlist(self.selected_playlist)
            self.selected_playlist = None
            self.refresh_playlists()

    def add_to_playlist(self):
        track_ids = self.selected_track_ids()
        playlist_id = self.add_playlist_combo.currentData()
        if not playlist_id:
            self.create_playlist()
            playlist_id = self.selected_playlist
        playlist = next((item for item in self.library.playlists() if item["id"] == playlist_id), None)
        if playlist:
            self.library.set_playlist_tracks(playlist_id, playlist["tracks"] + track_ids)
            if playlist_id == self.active_playlist:
                self.player.replace_queue(playlist["tracks"] + track_ids)
            self.refresh_playlists()

    def _playlist_order_changed(self, *args):
        if not self.building and self.selected_playlist:
            tracks = self.playlist_tracks.track_ids()
            self.library.set_playlist_tracks(self.selected_playlist, tracks)
            if self.selected_playlist == self.active_playlist:
                self.player.replace_queue(tracks)

    def move_playlist_track(self, direction):
        row = self.playlist_tracks.currentRow()
        target = row + direction
        self.playlist_tracks.move_row(row, target)

    def remove_playlist_track(self):
        rows = sorted((index.row() for index in self.playlist_tracks.selectionModel().selectedRows()), reverse=True)
        if rows:
            for row in rows:
                self.playlist_tracks.removeRow(row)
            self._playlist_order_changed()
            self._playlist_selected()

    def play_library_selection(self):
        selected = self.selected_track_ids()
        if not selected:
            return
        tracks = [item["id"] for item in self.library.tracks()] if len(selected) == 1 else selected
        self.active_playlist = None
        self.player.set_queue(tracks, tracks.index(selected[0]))
        self.play()

    def play_playlist(self, mix=False):
        playlist = next((item for item in self.library.playlists() if item["id"] == self.selected_playlist), None)
        if playlist and playlist["tracks"]:
            self.active_playlist = playlist["id"]
            start = random.randrange(len(playlist["tracks"])) if mix else max(0, self.playlist_tracks.currentRow())
            track = self.library.track(playlist["tracks"][start])
            if track and track.get("remote") and track["status"] == "Error":
                self.library.update_track(track["id"], status="NotDownloaded", error="", progress=0)
            if mix:
                self.shuffle_check.setChecked(True)
            self.player.set_queue(playlist["tracks"], start)
            self.play()

    def play(self):
        if not self.player.current_track:
            playlist = next((item for item in self.library.playlists() if item["id"] == self.selected_playlist), None)
            tracks = playlist["tracks"] if playlist else [item["id"] for item in self.library.tracks()]
            if not tracks:
                self.status.setText("Добавьте музыку или выберите непустой плейлист")
                return
            self.active_playlist = playlist["id"] if playlist else None
            self.player.set_queue(tracks)
        self.sourceRequested.emit("player")
        self.player.play()

    def prepare_track(self, track_id):
        track = self.library.track(track_id)
        if not track:
            return
        if track["status"] != "Ready":
            self.enqueue_analysis(track, priority=True)
            self.status.setText(f"{track['title']}: ожидание загрузки / анализа")
            return
        self.timeline_token = uuid.uuid4().hex
        self.timeline_track = track_id
        self.jobs.timeline(track_id, track["cache_path"], dict(self.settings), self.timeline_token)
        self.status.setText(f"{track['title']}: подготовка светового пресета {self.settings['preset']}")

    def _timeline_ready(self, track_id, token, path):
        if self.closing or token != self.timeline_token or track_id != self.player.current_track:
            return
        track = self.library.track(track_id)
        if not track:
            return
        try:
            self.scheduler.set_timeline(path, track["cache_path"])
            self.timeline_hash = timeline_key(track["content_id"], self.settings)
            self.player.start_ready(track_id, track["playback_path"])
            self.status.setText(f"{track['title']} · {self.settings['preset']}")
        except (OSError, ValueError, KeyError) as exc:
            self.status.setText(f"Таймлайн: {exc}")

    def _track_changed(self, track_id):
        track = self.library.track(track_id)
        if not track:
            return
        self.now_title.setText(track["title"])
        self.now_title.setToolTip(track["title"])
        self.now_artist.setText(" · ".join(filter(None, (track.get("artist"), track.get("album")))))
        self.now_artist.setToolTip(self.now_artist.text())
        self.seek_slider.setValue(0)
        self._duration_changed(track.get("duration", 0))
        picture = QtGui.QPixmap()
        if track.get("cover_path"):
            reader = QtGui.QImageReader(track["cover_path"])
            size = reader.size()
            if size.isValid():
                reader.setScaledSize(size.scaled(self.cover.size(), QtCore.Qt.KeepAspectRatio))
                picture = QtGui.QPixmap.fromImage(reader.read())
        self.cover.clear()
        if not picture.isNull():
            self.cover.setPixmap(picture.scaled(self.cover.size(), QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation))
        else:
            self.cover.setText("♫")
        self._queue_changed(self.player.queue.ordered_tracks())

    def _queue_changed(self, tracks):
        self.playback_queue.clear()
        for track_id in tracks:
            track = self.library.track(track_id)
            self.playback_queue.addItem(f"{track['title']} · {track['status']}" if track else "Трек удалён")
        self.playback_queue.setCurrentRow(self.player.queue.cursor)

    def _jump_queue(self, item):
        self.player.backend.stop()
        self.player.prepared = None
        self.player.queue.cursor = self.playback_queue.row(item)
        self.player.want_play = True
        self.sourceRequested.emit("player")
        self.player._request_current()

    def _playback_changed(self, state):
        names = {"playing": "Воспроизведение", "paused": "Пауза", "stopped": "Остановлено", "waiting": "Подготовка музыки…"}
        self.now_state.setText(names.get(state, state))
        playing = self.player.want_play
        icon = QtWidgets.QStyle.SP_MediaPause if playing else QtWidgets.QStyle.SP_MediaPlay
        title = "Пауза" if playing else "Воспроизвести"
        self.play_pause_button.setIcon(self._transport_icon(icon))
        self.play_pause_button.setToolTip(title)
        self.play_pause_button.setAccessibleName(title)
        self._queue_changed(self.player.queue.ordered_tracks())

    def _position_changed(self, position):
        if not self.seek_slider.isSliderDown():
            self.seek_slider.setValue(round(position * 1000))
        self.time_label.setText(f"{duration_text(position)} / {duration_text(self.seek_slider.maximum() / 1000)}")

    def _duration_changed(self, duration):
        self.seek_slider.setMaximum(max(0, round(duration * 1000)))

    def _preview(self, bars, energy, colors):
        track = self.library.track(self.player.current_track) if self.player.current_track else None
        bpm = f"{track.get('bpm', 0):.1f}" if track and track.get("bpm") else "—"
        confidence = f"{track.get('tempo_confidence', 0):.0%}" if track else "—"
        self.now_details.setText(f"BPM: {bpm} · уверенность: {confidence} · энергия: {energy:.0%} · {self.settings['preset']}")
        details = source_details(track) if track else ""
        if details:
            self.now_details.setText(self.now_details.text() + " · " + details)
        self.now_details.setToolTip(self.now_details.text() + ("\n" + track.get("resolved_url", "") if track else ""))
        self.previewReady.emit(bars, energy, colors)

    def _repeat_changed(self, remaining):
        self.repeat_button.setText(str(remaining))
        self.repeat_button.setChecked(remaining > 0)
        self.repeat_button.setToolTip(f"Повторить эту песню ещё {remaining} раз. Нажатие: +1; правая кнопка: сброс.")
        self.repeat_button.setAccessibleName(f"Повторы песни: {remaining}")

    def _listen_completed(self, track_id):
        if not self.closing:
            track = self.library.record_listen(track_id)
            if track:
                for table in self.playlist_lists.values():
                    table.update_track(track)

    def _shuffle_changed(self, enabled):
        self.player.queue.set_shuffle(enabled)
        self._queue_changed(self.player.queue.ordered_tracks())
        self.save_preferences()

    def _settings_changed(self, *args):
        if not self.building:
            self.settings_timer.start()

    def apply_show_settings(self):
        if self.closing:
            return
        selected = [lamp for row, lamp in enumerate(self.lamp_ids)
                    if self.lamp_table.item(row, 0).checkState() == QtCore.Qt.Checked]
        settings = show_settings({"preset": self.preset_combo.currentText(), "intensity": self.intensity_spin.value(),
            "max_brightness": self.brightness_spin.value() / 100, "update_hz": self.update_spin.value(),
            "latency_ms": self.latency_spin.value(), "lamp_ids": selected,
            "lamp_offsets": {lamp: spin.value() for lamp, spin in self.offset_spins.items()}}, self.lamp_ids)
        changed = settings != self.settings
        self.settings = settings
        self.scheduler.update_settings(settings)
        if self.active:
            self.rateChanged.emit(settings["update_hz"])
        self.save_preferences()
        current = self.library.track(self.player.current_track) if self.player.current_track else None
        if changed and current and current["status"] == "Ready":
            new_hash = timeline_key(current["content_id"], settings)
            if new_hash != self.timeline_hash:
                self.prepare_track(current["id"])

    def _lamp_item_changed(self, item):
        if self.building:
            return
        if item.column() == 2:
            lamp = self.lamp_ids[item.row()]
            self.groups[lamp] = item.text().strip() or "Все"
            self._refresh_groups()
        self._settings_changed()

    def _refresh_groups(self):
        self.group_combo.blockSignals(True)
        self.group_combo.clear()
        self.group_combo.addItem("Все лампы", None)
        for group in sorted({value for value in self.groups.values() if value and value != "Все"}):
            self.group_combo.addItem(group, group)
        self.group_combo.blockSignals(False)

    def _group_selected(self):
        group = self.group_combo.currentData()
        self.building = True
        for row, lamp in enumerate(self.lamp_ids):
            enabled = group is None or self.groups.get(lamp) == group
            self.lamp_table.item(row, 0).setCheckState(QtCore.Qt.Checked if enabled else QtCore.Qt.Unchecked)
        self.building = False
        self._settings_changed()

    def create_group(self):
        name, accepted = QtWidgets.QInputDialog.getText(self, "Группа ламп", "Название")
        if accepted and name.strip():
            self.building = True
            for row, lamp in enumerate(self.lamp_ids):
                if self.lamp_table.item(row, 0).checkState() == QtCore.Qt.Checked:
                    self.groups[lamp] = name.strip()
                    self.lamp_table.item(row, 2).setText(name.strip())
            self.building = False
            self._refresh_groups()
            self._settings_changed()

    def set_lamp_enabled(self, lamp, enabled):
        if lamp in self.lamp_ids:
            row = self.lamp_ids.index(lamp)
            self.lamp_table.item(row, 0).setCheckState(QtCore.Qt.Checked if enabled else QtCore.Qt.Unchecked)
            self.apply_show_settings()

    def set_source_active(self, active):
        self.active = bool(active)
        self.scheduler.set_active(active)
        if not active and (self.player.state == "playing" or self.player.want_play):
            self.player.pause()
        if active:
            self.rateChanged.emit(self.settings["update_hz"])

    def save_preferences(self):
        self.library.update_settings({"show": self.settings, "groups": self.groups, "playlist_id": self.selected_playlist,
            "repeat": self.player.queue.repeat, "shuffle": self.player.queue.shuffle, "volume": self.volume_slider.value()})

    def measure_latency(self):
        if not self.controller or not self.async_loop or (self.measure_future and not self.measure_future.done()):
            return
        self.measure_button.setEnabled(False)
        self.status.setText("Измерение сетевого RTT ламп…")

        async def measure():
            measurements = {}
            for _ in range(3):
                for row in await self.controller.diagnose_devices():
                    measurements.setdefault(row["id"], []).append(row)
            rows = []
            for lamp, samples in measurements.items():
                successful = [row.get("network_latency_ms", row["latency_ms"]) for row in samples if row["ok"]]
                rows.append({"id": lamp, "rtt": float(np.median(successful)) if successful else None,
                             "error": samples[-1].get("error", "")})
            return rows

        self.measure_future = asyncio.run_coroutine_threadsafe(measure(), self.async_loop)

        def done(future):
            try:
                rows = future.result()
            except Exception as exc:
                rows = [{"id": "", "rtt": None, "error": str(exc)}]
            self.latencyMeasured.emit(rows)
        self.measure_future.add_done_callback(done)

    def _latency_measured(self, rows):
        if self.closing:
            return
        self.building = True
        for result in rows:
            if result["id"] in self.lamp_ids:
                row = self.lamp_ids.index(result["id"])
                if result["rtt"] is not None:
                    self.lamp_table.item(row, 3).setText(f"{result['rtt']:.0f}")
                    self.offset_spins[result["id"]].setValue(round(result["rtt"] / 2))
                else:
                    self.lamp_table.item(row, 3).setText("Недоступна")
                    self.lamp_table.item(row, 3).setToolTip(result["error"])
        self.building = False
        self.apply_show_settings()
        self.measure_button.setEnabled(True)
        self.status.setText("RTT измерен. Компенсация равна RTT/2; её можно уточнить вручную.")

    def shutdown(self):
        if not self.closing:
            self.save_preferences()
            self.closing = True
            self.settings_timer.stop()
            self.player.stop()
            self.scheduler.shutdown()
            self.jobs.stop()
        return not self.jobs.isRunning() and (not self.measure_future or self.measure_future.done())
