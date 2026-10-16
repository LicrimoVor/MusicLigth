"""Windows RegisterHotKey integration; no keyboard polling or hook thread."""
import sys

from PyQt5 import QtCore, QtGui, QtWidgets


class _NativeFilter(QtCore.QAbstractNativeEventFilter):
    def __init__(self, owner):
        super().__init__()
        self.owner = owner

    def nativeEventFilter(self, event_type, message):
        if self.owner.active and event_type in (b"windows_generic_MSG", b"windows_dispatcher_MSG"):
            import ctypes
            from ctypes import wintypes
            event = wintypes.MSG.from_address(int(message))
            action = self.owner.registered.get(int(event.wParam))
            if event.message == 0x0312 and action:
                self.owner.activated.emit(action)
                return True, 0
        return False, 0


class GlobalMediaKeys(QtCore.QObject):
    activated = QtCore.pyqtSignal(str)

    def __init__(self, window):
        super().__init__(window)
        self.registered = {}
        self.active = True
        self.error = ""
        self.api = None
        self.filter = None
        self.shortcuts = []
        app = QtWidgets.QApplication.instance()
        if sys.platform == "win32" and app.platformName() == "windows":
            import ctypes
            from ctypes import wintypes
            self.api = ctypes.WinDLL("user32", use_last_error=True)
            self.api.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
            self.api.RegisterHotKey.restype = wintypes.BOOL
            self.api.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
            self.api.UnregisterHotKey.restype = wintypes.BOOL
            self.hwnd = int(window.winId())
            self.filter = _NativeFilter(self)
            app.installNativeEventFilter(self.filter)
            for identity, key, action in ((0x6D09, 0x78, "previous"), (0x6D10, 0x79, "next")):
                if self.api.RegisterHotKey(self.hwnd, identity, 0x4000, key):
                    self.registered[identity] = action
                else:
                    self.error += f"F{9 if key == 0x78 else 10}: клавиша занята другим приложением. "
        else:
            for key, action in (("F9", "previous"), ("F10", "next")):
                shortcut = QtWidgets.QShortcut(QtGui.QKeySequence(key), window)
                shortcut.setContext(QtCore.Qt.ApplicationShortcut)
                shortcut.activated.connect(lambda action=action: self.activated.emit(action))
                self.shortcuts.append(shortcut)

    def close(self):
        if not self.active:
            return
        self.active = False
        if self.api:
            for identity in self.registered:
                self.api.UnregisterHotKey(self.hwnd, identity)
        self.registered.clear()
        if self.filter:
            QtWidgets.QApplication.instance().removeNativeEventFilter(self.filter)
        for shortcut in self.shortcuts:
            shortcut.setEnabled(False)
