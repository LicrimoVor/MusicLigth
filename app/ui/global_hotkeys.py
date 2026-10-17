"""Windows media commands and global hotkeys, without keyboard polling."""
import sys
import time

from PyQt5 import QtCore, QtGui, QtWidgets


WM_HOTKEY = 0x0312
WM_APPCOMMAND = 0x0319
FUNCTION_BINDINGS = ((0x6D09, 0x78, "previous", "F9"), (0x6D10, 0x79, "next", "F10"))
MEDIA_BINDINGS = ((0x6D20, 0xB0, "next", "Следующий трек"),
                  (0x6D21, 0xB1, "previous", "Предыдущий трек"),
                  (0x6D22, 0xB2, "stop", "Стоп"),
                  (0x6D23, 0xB3, "play_pause", "Воспроизведение/пауза"))
APP_COMMANDS = {11: "next", 12: "previous", 13: "stop", 14: "play_pause", 46: "play", 47: "pause"}


class _NativeFilter(QtCore.QAbstractNativeEventFilter):
    def __init__(self, owner):
        super().__init__()
        self.owner = owner

    def nativeEventFilter(self, event_type, message):
        if self.owner.active and event_type in (b"windows_generic_MSG", b"windows_dispatcher_MSG"):
            import ctypes
            from ctypes import wintypes
            event = wintypes.MSG.from_address(int(message))
            return self.owner.handle_native_message(int(event.message), int(event.wParam), int(event.lParam))
        return False, 0


class GlobalMediaKeys(QtCore.QObject):
    activated = QtCore.pyqtSignal(str)
    errorChanged = QtCore.pyqtSignal(str)

    def __init__(self, window, media_enabled=True):
        super().__init__(window)
        self.registered = {}
        self.active = True
        self.error = ""
        self.api = None
        self.filter = None
        self.shortcuts = []
        self.media_shortcuts = []
        self.media_enabled = False
        self.registered_media = set()
        self._errors = {}
        self._last_media_event = None
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
            self._register(FUNCTION_BINDINGS)
        else:
            for key, action in (("F9", "previous"), ("F10", "next")):
                self.shortcuts.append(self._shortcut(window, key, action))
            for key, action in ((QtCore.Qt.Key_MediaNext, "next"), (QtCore.Qt.Key_MediaPrevious, "previous"),
                                (QtCore.Qt.Key_MediaPlay, "play_pause"), (QtCore.Qt.Key_MediaTogglePlayPause, "play_pause"),
                                (QtCore.Qt.Key_MediaPause, "pause"), (QtCore.Qt.Key_MediaStop, "stop")):
                shortcut = self._shortcut(window, key, action)
                shortcut.setEnabled(False)
                self.media_shortcuts.append(shortcut)
        self.set_media_enabled(media_enabled)

    def _shortcut(self, window, key, action):
        shortcut = QtWidgets.QShortcut(QtGui.QKeySequence(key), window)
        shortcut.setContext(QtCore.Qt.ApplicationShortcut)
        shortcut.setAutoRepeat(False)
        shortcut.activated.connect(lambda: self._emit_action(action, "qt"))
        return shortcut

    def _register(self, bindings, media=False):
        for identity, key, action, label in bindings:
            if self.api.RegisterHotKey(self.hwnd, identity, 0x4000, key):
                self.registered[identity] = action
                if media:
                    self.registered_media.add(identity)
            else:
                self._errors[identity] = f"{label}: клавиша занята другим приложением."
        self._refresh_error()

    def _refresh_error(self):
        message = " ".join(self._errors.values())
        if message != self.error:
            self.error = message
            self.errorChanged.emit(message)

    def set_media_enabled(self, enabled):
        enabled = bool(enabled)
        if not self.active or self.media_enabled == enabled:
            return
        self.media_enabled = enabled
        self._last_media_event = None
        if self.api:
            if enabled:
                self._register(MEDIA_BINDINGS, media=True)
            else:
                for identity, _key, _action, _label in MEDIA_BINDINGS:
                    if identity in self.registered_media:
                        self.api.UnregisterHotKey(self.hwnd, identity)
                        self.registered.pop(identity, None)
                    self._errors.pop(identity, None)
                self.registered_media.clear()
                self._refresh_error()
        for shortcut in self.media_shortcuts:
            shortcut.setEnabled(enabled)

    def _emit_action(self, action, source):
        if not self.active:
            return
        if source in ("media_hotkey", "appcommand"):
            now = time.monotonic()
            previous = self._last_media_event
            # Some device drivers send both kinds of message for one press.
            if previous and previous[0] == action and previous[1] != source and now - previous[2] < 0.12:
                return
            self._last_media_event = (action, source, now)
        self.activated.emit(action)

    def handle_native_message(self, message, wparam, lparam):
        if not self.active:
            return False, 0
        if message == WM_HOTKEY:
            action = self.registered.get(wparam)
            if action:
                self._emit_action(action, "media_hotkey" if wparam in self.registered_media else "shortcut")
                return True, 0
        elif message == WM_APPCOMMAND and self.media_enabled:
            # GET_APPCOMMAND_LPARAM: strip the keyboard/mouse/OEM device flags.
            action = APP_COMMANDS.get((lparam >> 16) & 0x0FFF)
            if action:
                self._emit_action(action, "appcommand")
                return True, 1
        return False, 0

    def close(self):
        if not self.active:
            return
        self.active = False
        if self.api:
            for identity in self.registered:
                self.api.UnregisterHotKey(self.hwnd, identity)
        self.registered.clear()
        self.registered_media.clear()
        if self.filter:
            QtWidgets.QApplication.instance().removeNativeEventFilter(self.filter)
        for shortcut in self.shortcuts + self.media_shortcuts:
            shortcut.setEnabled(False)
