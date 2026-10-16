from PyQt5 import QtCore, QtGui, QtWidgets


class PlaylistTable(QtWidgets.QTableWidget):
    orderChanged = QtCore.pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(0, 5, parent)
        self.setHorizontalHeaderLabels(["Название", "Исполнитель", "Длительность", "Прослушивания", "Статус"])
        self.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.setDragDropMode(QtWidgets.QAbstractItemView.InternalMove)
        self.setDragDropOverwriteMode(False)
        self.horizontalHeader().setSectionResizeMode(0, QtWidgets.QHeaderView.Stretch)
        self.horizontalHeader().setSectionResizeMode(1, QtWidgets.QHeaderView.Stretch)
        for column in (2, 3, 4):
            self.horizontalHeader().setSectionResizeMode(column, QtWidgets.QHeaderView.ResizeToContents)
        self.rows_by_id = {}

    def track_ids(self):
        return [self.item(row, 0).data(QtCore.Qt.UserRole) for row in range(self.rowCount())]

    def selected_id(self):
        item = self.item(self.currentRow(), 0)
        return item.data(QtCore.Qt.UserRole) if item else None

    def fill(self, ids, tracks):
        selected = self.selected_id()
        scroll = self.verticalScrollBar().value()
        self.setUpdatesEnabled(False)
        self.rows_by_id = {}
        available = [identity for identity in ids if identity in tracks]
        self.setRowCount(len(available))
        for row, identity in enumerate(available):
            self.rows_by_id[identity] = row
            self.update_track(tracks[identity])
        if selected in self.rows_by_id:
            self.selectRow(self.rows_by_id[selected])
        self.verticalScrollBar().setValue(scroll)
        self.setUpdatesEnabled(True)

    def update_track(self, track):
        row = self.rows_by_id.get(track["id"])
        if row is None:
            return
        seconds = max(0, int(track.get("duration") or track.get("expected_duration") or 0))
        duration = f"{seconds // 60}:{seconds % 60:02}" if seconds else "—"
        status = track.get("status", "NotDownloaded")
        label, color, background = {
            "Ready": ("● Ready", "#7ee6ab", "#14382a"),
            "Error": ("! Error", "#ffaaaa", "#51252b"),
            "NotDownloaded": ("↓ Не скачан", "#c4cdd6", "#2b343e"),
            "Pending": ("◷ В очереди", "#f4d58b", "#443b24"),
            "Analyzing": (f"◷ Анализ {track.get('progress', 0)}%", "#9cd9ff", "#203b50"),
        }.get(status, (status, "#c4cdd6", "#2b343e"))
        values = [track["title"], track.get("artist") or "—", duration, str(track.get("play_count", 0)), label]
        for column, text in enumerate(values):
            item = self.item(row, column)
            if item is None:
                item = QtWidgets.QTableWidgetItem()
                self.setItem(row, column, item)
            item.setText(text)
            item.setData(QtCore.Qt.UserRole, track["id"])
            item.setToolTip(track.get("error") or text)
            if column >= 2:
                item.setTextAlignment(QtCore.Qt.AlignCenter)
            if column == 4:
                item.setForeground(QtGui.QColor(color))
                item.setBackground(QtGui.QColor(background))
                font = item.font()
                font.setBold(True)
                item.setFont(font)

    def move_row(self, source, target):
        if source == target or not (0 <= source < self.rowCount() and 0 <= target < self.rowCount()):
            return
        values = [self.takeItem(source, column) for column in range(self.columnCount())]
        self.removeRow(source)
        self.insertRow(target)
        for column, item in enumerate(values):
            self.setItem(target, column, item)
        self.rows_by_id = {identity: row for row, identity in enumerate(self.track_ids())}
        self.selectRow(target)
        self.orderChanged.emit()

    def dropEvent(self, event):
        if event.source() is not self:
            event.ignore()
            return
        target = self.indexAt(event.pos()).row()
        self.move_row(self.currentRow(), target if target >= 0 else self.rowCount() - 1)
        # The row was moved above. A MoveAction would let QAbstractItemView's
        # source-side cleanup delete it a second time after the drag finishes.
        event.setDropAction(QtCore.Qt.CopyAction)
        event.accept()
