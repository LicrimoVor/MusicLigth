from PyQt5 import QtCore, QtWidgets


class ImportDialog(QtWidgets.QDialog):
    importRequested = QtCore.pyqtSignal(str)
    filesRequested = QtCore.pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Импорт музыки")
        self.resize(660, 410)
        layout = QtWidgets.QVBoxLayout(self)
        label = QtWidgets.QLabel("VK · YouTube · Яндекс Музыка · Название песни")
        layout.addWidget(label)
        hint = QtWidgets.QLabel("Вставьте ссылки или названия — каждая строка распознаётся отдельно. Плейлист по ссылке получит свою вкладку; отдельные песни попадут в текущий плейлист. Можно вставить HTML списка VK.")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.text = QtWidgets.QPlainTextEdit()
        self.text.setPlaceholderText("https://vk.com/music/playlist/...\nhttps://www.youtube.com/playlist?list=...\nhttps://music.yandex.ru/users/.../playlists/...\nИсполнитель — Название песни")
        layout.addWidget(self.text)
        row = QtWidgets.QHBoxLayout()
        files = QtWidgets.QPushButton("Из файла HTML / TXT / JSON…")
        files.clicked.connect(self.filesRequested)
        row.addWidget(files)
        row.addStretch()
        cancel = QtWidgets.QPushButton("Отмена")
        cancel.clicked.connect(self.reject)
        row.addWidget(cancel)
        self.submit = QtWidgets.QPushButton("Импортировать")
        self.submit.setEnabled(False)
        self.text.textChanged.connect(lambda: self.submit.setEnabled(bool(self.text.toPlainText().strip())))
        self.submit.clicked.connect(self._submit)
        row.addWidget(self.submit)
        layout.addLayout(row)

    def _submit(self):
        self.importRequested.emit(self.text.toPlainText())
        self.accept()
