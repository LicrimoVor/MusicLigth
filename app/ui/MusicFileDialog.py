from PyQt5 import QtWidgets


class MusicFileDialog(QtWidgets.QFileDialog):
    """Qt's non-native browser, accepting both files and directories in one selection."""

    def accept(self):
        paths = self.selectedFiles()
        if paths:
            # QFileDialog.accept enters a selected directory instead of selecting it.
            # Discovery and file validation belong to the existing background worker.
            self.filesSelected.emit(paths)
            QtWidgets.QDialog.accept(self)
