"""Keep the native caption without entering Windows' button tracking loop."""
import ctypes
import sys

from PyQt5 import QtWidgets


WM_NCHITTEST = 0x0084
WM_NCLBUTTONDOWN, WM_NCLBUTTONUP = 0x00A1, 0x00A2
WM_LBUTTONUP, WM_CAPTURECHANGED, WM_CANCELMODE = 0x0202, 0x0215, 0x001F
WM_SYSCOMMAND = 0x0112
HTMINBUTTON, HTMAXBUTTON, HTCLOSE = 8, 9, 20
SC_MINIMIZE, SC_MAXIMIZE, SC_RESTORE, SC_CLOSE = 0xF020, 0xF030, 0xF120, 0xF060


class CaptionButtons:
    def __init__(self, window, api=None):
        self.window = window
        self.pressed = None
        self.api = api
        if api is None and sys.platform == "win32" and QtWidgets.QApplication.platformName() == "windows":
            from ctypes import wintypes
            self.api = ctypes.WinDLL("user32", use_last_error=True)
            self.api.SetCapture.argtypes = [wintypes.HWND]
            self.api.SetCapture.restype = wintypes.HWND
            self.api.GetCapture.restype = wintypes.HWND
            self.api.ReleaseCapture.restype = wintypes.BOOL
            self.api.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
            self.api.SendMessageW.restype = ctypes.c_ssize_t
            self.api.PostMessageW.argtypes = self.api.SendMessageW.argtypes
            self.api.PostMessageW.restype = wintypes.BOOL
            self.api.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
            self.api.ClientToScreen.restype = wintypes.BOOL

    def handle(self, message, wparam, lparam, hwnd):
        if self.api is None:
            return False
        if message == WM_NCLBUTTONDOWN and wparam in (HTMINBUTTON, HTMAXBUTTON, HTCLOSE):
            # DefWindowProc normally waits here until release. Qt audio/lighting
            # timers must continue to run, so capture and handle release ourselves.
            self.api.SetCapture(hwnd)
            self.pressed = wparam
            return True
        if self.pressed is None:
            return False
        if message in (WM_LBUTTONUP, WM_NCLBUTTONUP):
            from ctypes import wintypes
            point = wintypes.POINT(ctypes.c_short(lparam & 0xFFFF).value,
                                   ctypes.c_short((lparam >> 16) & 0xFFFF).value)
            if message == WM_LBUTTONUP:
                self.api.ClientToScreen(hwnd, ctypes.byref(point))
            packed = (point.x & 0xFFFF) | ((point.y & 0xFFFF) << 16)
            target = self.api.SendMessageW(hwnd, WM_NCHITTEST, 0, packed)
            pressed, self.pressed = self.pressed, None
            self._release(hwnd)
            if target == pressed:
                command = {HTMINBUTTON: SC_MINIMIZE, HTCLOSE: SC_CLOSE,
                           HTMAXBUTTON: SC_RESTORE if self.window.isMaximized() else SC_MAXIMIZE}[pressed]
                self.api.PostMessageW(hwnd, WM_SYSCOMMAND, command, 0)
            return True
        if message in (WM_CANCELMODE, WM_CAPTURECHANGED) or (message == 0x0100 and wparam == 0x1B):
            self.pressed = None
            if message != WM_CAPTURECHANGED:
                self._release(hwnd)
        return False

    def _release(self, hwnd):
        if self.api.GetCapture() == hwnd:
            self.api.ReleaseCapture()

    def native_event(self, pointer):
        if self.api is None:
            return False
        from ctypes import wintypes
        event = wintypes.MSG.from_address(int(pointer))
        return self.handle(event.message, int(event.wParam), int(event.lParam), int(event.hWnd or 0))
