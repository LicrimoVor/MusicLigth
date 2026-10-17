from PyQt5 import QtCore, QtMultimedia, QtWidgets

from music.audio_backend import PCMPlayerBackend
from music.matching import search_query, source_details
from music.providers import safe_choice
from music.recording_jobs import RecordingJobs
from ui.caption_buttons import CaptionButtons


def duration_text(seconds):
    seconds = max(0, round(seconds or 0))
    return f"{seconds // 60}:{seconds % 60:02}"


class RecordingDialog(QtWidgets.QDialog):
    recordingChosen = QtCore.pyqtSignal(str, dict)
    closedAndStopped = QtCore.pyqtSignal(object)

    def __init__(self, panel, track, *, backend=None):
        super().__init__(panel.window())
        self.caption_buttons = CaptionButtons(self)
        self.panel, self.track = panel, dict(track)
        self.setWindowTitle("Выбрать другую запись")
        self.resize(940, 650)
        self.worker = RecordingJobs(panel.library.root, panel.jobs.downloader.catalogs.credentials, self)
        self.backend = backend or PCMPlayerBackend(self)
        self.token = None
        self.preview_choice = None
        self.previewing = False
        self.preview_loaded = False
        self._preview_error = False
        self._main_resume = None
        self._changing_main = False
        self._closed = False
        self.access_dialog = None
        self.worker.results.connect(self._results)
        self.worker.previewReady.connect(self._preview_ready)
        self.worker.failed.connect(self._failed)
        self.worker.progress.connect(self._progress)
        self.worker.finished.connect(self._worker_finished)
        self.backend.positionChanged.connect(self._position)
        self.backend.durationChanged.connect(lambda duration: self.seek.setMaximum(duration))
        self.backend.error.connect(self._audio_error)
        self.backend.mediaStatusChanged.connect(self._media_status)
        panel.player.playbackChanged.connect(self._main_state_changed)
        panel.player.trackChanged.connect(lambda _track: self._forget_resume())
        layout = QtWidgets.QVBoxLayout(self)
        title = QtWidgets.QLabel(track["title"] + (" · " + track["artist"] if track.get("artist") else ""))
        title.setTextFormat(QtCore.Qt.PlainText)
        title.setWordWrap(True)
        layout.addWidget(title)
        row = QtWidgets.QHBoxLayout()
        self.query = QtWidgets.QLineEdit(search_query(track))
        self.query.setPlaceholderText("Исполнитель и название")
        self.query.returnPressed.connect(self.search)
        row.addWidget(self.query, 1)
        search = QtWidgets.QPushButton("Найти заново")
        search.clicked.connect(self.search)
        row.addWidget(search)
        access = QtWidgets.QPushButton("Источники")
        access.clicked.connect(self.show_access)
        row.addWidget(access)
        layout.addLayout(row)
        self.table = QtWidgets.QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Источник", "Исполнитель", "Название", "Длительность", "Совпадение"])
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(2, QtWidgets.QHeaderView.Stretch)
        self.table.setColumnWidth(0, 135)
        self.table.setColumnWidth(1, 160)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        self.table.cellDoubleClicked.connect(lambda _row, _column: self.preview())
        layout.addWidget(self.table, 1)
        self.provider_status = QtWidgets.QPlainTextEdit()
        self.provider_status.setReadOnly(True)
        self.provider_status.setMaximumHeight(90)
        self.provider_status.hide()
        layout.addWidget(self.provider_status)
        self.status = QtWidgets.QLabel("Поиск записей…")
        self.status.setWordWrap(True)
        self.status.setTextFormat(QtCore.Qt.PlainText)
        layout.addWidget(self.status)
        row = QtWidgets.QHBoxLayout()
        self.preview_button = QtWidgets.QPushButton("Прослушать")
        self.preview_button.setEnabled(False)
        self.preview_button.clicked.connect(self.preview)
        row.addWidget(self.preview_button)
        self.stop_button = QtWidgets.QPushButton("Стоп")
        self.stop_button.clicked.connect(self.stop_preview)
        row.addWidget(self.stop_button)
        self.seek = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.seek.sliderReleased.connect(lambda: self.backend.setPosition(self.seek.value()))
        row.addWidget(self.seek, 1)
        self.time = QtWidgets.QLabel("0:00")
        row.addWidget(self.time)
        row.addWidget(QtWidgets.QLabel("Громкость"))
        self.volume = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setValue(panel.volume_slider.value())
        self.volume.setMaximumWidth(100)
        self.volume.valueChanged.connect(self.backend.setVolume)
        self.backend.setVolume(self.volume.value())
        row.addWidget(self.volume)
        layout.addLayout(row)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Cancel)
        buttons.button(QtWidgets.QDialogButtonBox.Cancel).setText("Отмена")
        buttons.rejected.connect(self.reject)
        self.choose_button = buttons.addButton("Выбрать эту запись", QtWidgets.QDialogButtonBox.AcceptRole)
        self.choose_button.clicked.connect(self.choose)
        self.choose_button.setEnabled(False)
        layout.addWidget(buttons)
        self.worker.start()
        QtCore.QTimer.singleShot(0, self.search)

    def nativeEvent(self, event_type, message):
        if hasattr(self, "caption_buttons") and self.caption_buttons.native_event(message):
            return True, 0
        return super().nativeEvent(event_type, message)

    def current_choice(self):
        row = self.table.currentRow()
        return self.table.item(row, 0).data(QtCore.Qt.UserRole) if row >= 0 else None

    def search(self):
        if self._closed or not self.query.text().strip():
            return
        self.stop_preview()
        self.table.setRowCount(0)
        self.provider_status.hide()
        excluded = [self.track["resolved_url"]] if self.track.get("resolved_url") else []
        self.token = self.worker.request("search", self.track | {"excluded_urls": excluded}, self.query.text().strip())
        self.status.setText("Ищу записи во всех источниках…")

    def _results(self, token, entries, errors):
        if token != self.token or self._closed:
            return
        self.table.setRowCount(len(entries))
        for row, entry in enumerate(entries):
            score = entry.get("match_score")
            values = (entry.get("provider", ""), entry.get("artist") or entry.get("uploader", ""), entry["title"],
                      duration_text(entry.get("duration")), f"{min(100, round(score * 100))}%" if score is not None else "Проверить")
            for column, value in enumerate(values):
                item = QtWidgets.QTableWidgetItem(str(value))
                item.setToolTip(entry["webpage_url"] if column == 0 else str(value))
                if column == 0:
                    item.setData(QtCore.Qt.UserRole, entry)
                self.table.setItem(row, column, item)
        self.provider_status.setPlainText("\n".join(errors))
        self.provider_status.setVisible(bool(errors))
        self.status.setText(f"Найдено записей: {len(entries)}. Двойной щелчок — прослушать." if entries else "Записи не найдены. Уточните запрос или подключите источник.")
        if entries:
            self.table.selectRow(0)

    def _selection_changed(self):
        choice = self.current_choice()
        self.preview_button.setEnabled(bool(choice))
        self.choose_button.setEnabled(bool(choice))
        self.preview_button.setText("Прослушать")

    def preview(self):
        choice = self.current_choice()
        if not choice or self._closed:
            return
        if self.preview_loaded and self.preview_choice == choice:
            if self.previewing:
                self.backend.pause()
                self.previewing = False
                self.preview_button.setText("Прослушать")
            else:
                self.backend.play()
                self.previewing = True
                self.preview_button.setText("Пауза")
            return
        self.backend.stop()
        self.previewing = False
        self.preview_loaded = False
        self.preview_choice = dict(choice)
        self.token = self.worker.request("preview", self.track | {"excluded_urls": []}, safe_choice(choice))
        self.status.setText("Подготавливаю запись для прослушивания…")

    def _preview_ready(self, token, metadata):
        if token != self.token or self._closed:
            return
        if self.panel.player.want_play and self._main_resume is None:
            self._main_resume = self.panel.player.current_track
            self._changing_main = True
            try:
                self.panel.player.pause()
            finally:
                self._changing_main = False
        self.seek.setRange(0, round(metadata.get("source_duration", 0) * 1000))
        self._preview_error = False
        self.backend.setMedia(QtMultimedia.QMediaContent(QtCore.QUrl.fromLocalFile(metadata["path"])))
        self.backend.play()
        if self._preview_error:
            return
        self.preview_loaded = True
        self.previewing = True
        self.preview_button.setText("Пауза")
        self.status.setText(f"Прослушивание: {metadata.get('resolved_title', '')} · {source_details(metadata)}")

    def _progress(self, token, percent, stage):
        if token == self.token and not self._closed:
            self.status.setText(stage + (f" · {percent}%" if percent else ""))

    def _failed(self, token, message):
        if token == self.token and not self._closed:
            self.previewing = False
            self.preview_loaded = False
            self.preview_button.setText("Прослушать")
            self.status.setText(message)
            self._resume_main()

    def _position(self, position):
        if not self.seek.isSliderDown():
            self.seek.setValue(position)
        self.time.setText(duration_text(position / 1000))

    def _audio_error(self, error):
        if error != QtMultimedia.QMediaPlayer.NoError:
            self._preview_error = True
            self._failed(self.token, self.backend.errorString() or "Не удалось воспроизвести запись")

    def _media_status(self, status):
        if status == QtMultimedia.QMediaPlayer.EndOfMedia:
            self.stop_preview()

    def _forget_resume(self):
        self._main_resume = None

    def _main_state_changed(self, _state):
        if not self._changing_main:
            self._forget_resume()
            if self.panel.player.want_play and (self.previewing or self.preview_loaded):
                self.backend.stop()
                self.previewing = False
                self.preview_loaded = False
                self.preview_button.setText("Прослушать")

    def _resume_main(self):
        track, self._main_resume = self._main_resume, None
        if (track and self.panel.player.current_track == track and self.panel.player.state == "paused"
                and not self.panel.closing and not getattr(self.panel.window(), "_closing", False)):
            self.panel.player.play()

    def stop_preview(self):
        self.worker.cancel()
        self.token = None
        self.backend.stop()
        self.previewing = False
        self.preview_loaded = False
        self.preview_choice = None
        self.preview_button.setText("Прослушать")
        self._resume_main()

    def choose(self):
        choice = self.current_choice()
        if choice:
            self.stop_preview()
            self.recordingChosen.emit(self.track["id"], safe_choice(choice))
            self.accept()

    def show_access(self):
        if self.access_dialog:
            self.access_dialog.show()
            self.access_dialog.raise_()
            return
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle("Доступ к источникам")
        form = QtWidgets.QFormLayout(dialog)
        note = QtWidgets.QLabel("Токены хранятся в памяти до закрытия приложения.\nVK нужен доступ к API аудио; Яндекс может требовать подписку для полной записи.")
        note.setWordWrap(True)
        form.addRow(note)
        fields = {}
        for key, title in (("yandex", "Токен Яндекс Музыки"), ("vk", "Токен VK")):
            field = QtWidgets.QLineEdit()
            field.setEchoMode(QtWidgets.QLineEdit.Password)
            field.setText(self.worker.downloader.catalogs.credentials.get(key, ""))
            form.addRow(title, field)
            fields[key] = field
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Apply | QtWidgets.QDialogButtonBox.Close)
        buttons.button(QtWidgets.QDialogButtonBox.Apply).setText("Применить")
        buttons.button(QtWidgets.QDialogButtonBox.Close).setText("Закрыть")
        def apply():
            self.worker.downloader.catalogs.credentials.update({key: field.text().strip() for key, field in fields.items()})
            self.search()
        buttons.button(QtWidgets.QDialogButtonBox.Apply).clicked.connect(apply)
        buttons.rejected.connect(dialog.close)
        form.addRow(buttons)
        dialog.finished.connect(lambda _result: self._access_closed(dialog))
        self.access_dialog = dialog
        dialog.show()

    def _access_closed(self, dialog):
        self.access_dialog = None
        dialog.deleteLater()

    def done(self, result):
        if self._closed:
            return
        self._closed = True
        self.stop_preview()
        self.worker.stop()
        if self.access_dialog:
            self.access_dialog.close()
        if getattr(self.backend, "file", None):
            self.backend.file.close()
        super().done(result)
        if not self.worker.isRunning():
            self.closedAndStopped.emit(self)

    def _worker_finished(self):
        if self._closed:
            self.closedAndStopped.emit(self)
