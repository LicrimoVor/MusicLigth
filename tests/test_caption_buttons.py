import unittest
from unittest.mock import Mock

import test_music as shared
from PyQt5 import QtCore, QtTest
from ui.caption_buttons import (CaptionButtons, WM_NCLBUTTONDOWN, WM_NCLBUTTONUP, WM_CAPTURECHANGED,
                                WM_CANCELMODE, WM_SYSCOMMAND, HTMINBUTTON, HTMAXBUTTON, HTCLOSE,
                                SC_MINIMIZE, SC_MAXIMIZE, SC_RESTORE, SC_CLOSE)


class CaptionButtonTests(unittest.TestCase):
    def setUp(self):
        self.api = Mock()
        self.api.GetCapture.return_value = 123
        self.window = Mock()
        self.window.isMaximized.return_value = False
        self.controller = CaptionButtons(self.window, self.api)

    def test_commands_are_posted_on_matching_release_only(self):
        for hit, command in ((HTMINBUTTON, SC_MINIMIZE), (HTMAXBUTTON, SC_MAXIMIZE), (HTCLOSE, SC_CLOSE)):
            self.api.reset_mock()
            self.assertTrue(self.controller.handle(WM_NCLBUTTONDOWN, hit, 0, 123))
            self.api.PostMessageW.assert_not_called()
            self.api.SendMessageW.return_value = hit
            self.assertTrue(self.controller.handle(WM_NCLBUTTONUP, hit, 0, 123))
            self.api.PostMessageW.assert_called_once_with(123, WM_SYSCOMMAND, command, 0)
            self.api.ReleaseCapture.assert_called_once()
        self.window.isMaximized.return_value = True
        self.controller.handle(WM_NCLBUTTONDOWN, HTMAXBUTTON, 0, 123)
        self.api.SendMessageW.return_value = HTMAXBUTTON
        self.controller.handle(WM_NCLBUTTONUP, HTMAXBUTTON, 0, 123)
        self.assertEqual(self.api.PostMessageW.call_args.args[2], SC_RESTORE)

    def test_release_outside_cancel_escape_and_lost_capture_do_not_trigger_actions(self):
        self.controller.handle(WM_NCLBUTTONDOWN, HTCLOSE, 0, 123)
        self.api.SendMessageW.return_value = 1
        self.controller.handle(WM_NCLBUTTONUP, 1, 0, 123)
        for message, key in ((WM_CAPTURECHANGED, 0), (WM_CANCELMODE, 0), (0x0100, 0x1B)):
            self.controller.handle(WM_NCLBUTTONDOWN, HTCLOSE, 0, 123)
            self.controller.handle(message, key, 0, 123)
            self.assertIsNone(self.controller.pressed)
        self.api.PostMessageW.assert_not_called()
        self.assertFalse(self.controller.handle(WM_NCLBUTTONDOWN, 2, 0, 123))  # caption drag remains native


class NativeCaptionTests(unittest.TestCase):
    def test_windows_caption_hold_keeps_qt_timers_running(self):
        if shared.application().platformName() != "windows":
            self.skipTest("Native Windows platform required")
        case = shared.MusicIntegrationTests()
        case.setUp()
        timer = QtCore.QTimer()
        try:
            case.window.show()
            QtTest.QTest.qWait(50)
            api = case.window.caption_buttons.api
            hwnd = int(case.window.winId())
            ticks = []
            timer.setInterval(10)
            timer.timeout.connect(lambda: ticks.append(1))
            timer.start()
            for hit in (HTMINBUTTON, HTMAXBUTTON, HTCLOSE):
                ticks.clear()
                api.PostMessageW(hwnd, WM_NCLBUTTONDOWN, hit, 0)
                QtTest.QTest.qWait(250)
                self.assertEqual(case.window.caption_buttons.pressed, hit)
                self.assertGreater(len(ticks), 10)
                api.PostMessageW(hwnd, WM_CANCELMODE, 0, 0)
                QtTest.QTest.qWait(20)
                self.assertIsNone(case.window.caption_buttons.pressed)
        finally:
            timer.stop()
            case.tearDown()


if __name__ == "__main__":
    unittest.main()
