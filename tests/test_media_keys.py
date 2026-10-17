import ctypes
from ctypes import wintypes
import unittest
from unittest.mock import Mock, patch

from PyQt5 import QtCore, QtWidgets

import test_music as shared
from ui.global_hotkeys import APP_COMMANDS, FUNCTION_BINDINGS, MEDIA_BINDINGS, WM_APPCOMMAND, WM_HOTKEY, GlobalMediaKeys, _NativeFilter


class MediaKeyTests(unittest.TestCase):
    def setUp(self):
        shared.application()
        self.window = QtWidgets.QWidget()
        self.keys = GlobalMediaKeys(self.window, media_enabled=False)
        self.events = []
        self.keys.activated.connect(self.events.append)

    def tearDown(self):
        self.keys.close()
        self.window.close()
        self.window.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.DeferredDelete)

    def fake_api(self):
        self.keys.api = Mock()
        self.keys.api.RegisterHotKey.return_value = True
        self.keys.hwnd = 123
        return self.keys.api

    def test_media_registration_changes_with_mode_and_is_released_on_exit(self):
        api = self.fake_api()
        self.keys._register(FUNCTION_BINDINGS)
        self.keys.set_media_enabled(True)
        self.assertEqual(api.RegisterHotKey.call_count, 6)
        self.keys.set_media_enabled(True)
        self.assertEqual(api.RegisterHotKey.call_count, 6)
        self.assertEqual(len(self.keys.registered_media), 4)
        self.keys.set_media_enabled(False)
        self.assertEqual(api.UnregisterHotKey.call_count, 4)
        self.assertEqual(set(self.keys.registered), {binding[0] for binding in FUNCTION_BINDINGS})
        self.keys.close()
        self.assertEqual(api.UnregisterHotKey.call_count, 6)
        self.assertFalse(self.keys.registered)
        self.assertFalse(self.keys.registered_media)

    def test_appcommands_decode_device_flags_and_leave_volume_to_windows(self):
        self.keys.set_media_enabled(True)
        for command, action in APP_COMMANDS.items():
            result = self.keys.handle_native_message(WM_APPCOMMAND, 123, (command | 0x8000) << 16)
            self.assertEqual(result, (True, 1))
            self.assertEqual(self.events[-1], action)
        before = list(self.events)
        for command in (8, 9, 10, 99):
            self.assertEqual(self.keys.handle_native_message(WM_APPCOMMAND, 123, command << 16), (False, 0))
        self.assertEqual(self.events, before)
        self.assertEqual(self.keys.handle_native_message(WM_APPCOMMAND, 123, (11 | 0x1000) << 16), (True, 1))

    def test_duplicate_driver_messages_do_not_skip_two_tracks(self):
        self.fake_api()
        self.keys.set_media_enabled(True)
        identity = MEDIA_BINDINGS[0][0]
        with patch("ui.global_hotkeys.time.monotonic", side_effect=(1, 1.05, 1.06, 1.08, 1.4)):
            for kind, value in ((WM_HOTKEY, identity), (WM_APPCOMMAND, 11 << 16),
                                (WM_HOTKEY, identity), (WM_APPCOMMAND, 11 << 16), (WM_APPCOMMAND, 11 << 16)):
                self.keys.handle_native_message(kind, value if kind == WM_HOTKEY else 0,
                                                value if kind == WM_APPCOMMAND else 0)
        self.assertEqual(self.events, ["next"] * 3)

    def test_conflicts_report_error_without_unregistering_someone_elses_key(self):
        api = self.fake_api()
        failed = MEDIA_BINDINGS[-1][0]
        api.RegisterHotKey.side_effect = lambda _hwnd, identity, _flags, _key: identity != failed
        self.keys.set_media_enabled(True)
        self.assertIn("Воспроизведение/пауза", self.keys.error)
        self.assertNotIn(failed, self.keys.registered)
        self.keys.set_media_enabled(False)
        self.assertEqual(self.keys.error, "")
        self.assertNotIn((123, failed), [call.args for call in api.UnregisterHotKey.call_args_list])

    def test_fallback_media_shortcuts_are_inactive_in_live_mode(self):
        self.assertTrue(all(not shortcut.isEnabled() for shortcut in self.keys.media_shortcuts))
        self.keys.set_media_enabled(True)
        self.assertTrue(all(shortcut.isEnabled() and not shortcut.autoRepeat() for shortcut in self.keys.media_shortcuts))
        self.keys.set_media_enabled(False)
        self.assertTrue(all(not shortcut.isEnabled() for shortcut in self.keys.media_shortcuts))
        self.assertEqual(self.keys.handle_native_message(WM_APPCOMMAND, 0, 14 << 16), (False, 0))
        self.keys.close()
        self.assertEqual(self.keys.handle_native_message(WM_APPCOMMAND, 0, 11 << 16), (False, 0))
        self.assertEqual(self.events, [])

    @unittest.skipUnless(__import__("sys").platform == "win32", "Windows message layout")
    def test_native_filter_decodes_a_real_windows_message_structure(self):
        self.keys.set_media_enabled(True)
        native = _NativeFilter(self.keys)
        message = wintypes.MSG()
        message.message = WM_APPCOMMAND
        message.lParam = 14 << 16
        self.assertEqual(native.nativeEventFilter(b"windows_generic_MSG", ctypes.addressof(message)), (True, 1))
        self.assertEqual(self.events, ["play_pause"])
        self.assertEqual(native.nativeEventFilter(b"unrelated", ctypes.addressof(message)), (False, 0))


class RemoteControlIntegrationTests(unittest.TestCase):
    setUp = shared.MusicIntegrationTests.setUp
    tearDown = shared.MusicIntegrationTests.tearDown

    def prepare_player(self):
        playlist = self.panel.library.create_playlist("Remote")
        for title in ("One", "Two", "Three"):
            self.panel.library.add_search_track(playlist["id"], title)
        identities = self.panel.library.playlists()[0]["tracks"]
        self.panel.player.preparationRequested.disconnect(self.panel.prepare_track)
        self.panel.player.set_queue(identities)
        self.window.select_light_source("player")
        self.window.hide()
        return identities

    def command(self, command):
        self.assertEqual(self.window.hotkeys.handle_native_message(WM_APPCOMMAND, 0, command << 16), (True, 1))
        shared.application().processEvents()

    def test_remote_navigation_and_toggle_work_while_hidden(self):
        identities = self.prepare_player()
        self.command(11)
        self.assertEqual(self.panel.player.current_track, identities[1])
        self.backend.setPosition(8000)
        self.command(12)
        self.assertEqual(self.panel.player.current_track, identities[0])
        self.command(14)
        self.assertTrue(self.panel.player.want_play)
        self.command(14)
        self.assertFalse(self.panel.player.want_play)
        self.assertFalse(self.window.isVisible())

    def test_discrete_play_pause_stop_and_mode_switch(self):
        self.prepare_player()
        self.command(46)
        self.assertTrue(self.panel.player.want_play)
        self.command(46)
        self.assertTrue(self.panel.player.want_play)
        self.command(47)
        self.command(47)
        self.assertFalse(self.panel.player.want_play)
        self.command(46)
        self.backend.setPosition(1200)
        self.command(13)
        self.assertFalse(self.panel.player.want_play)
        self.assertEqual(self.backend.position(), 0)
        self.window.select_light_source("live")
        self.assertFalse(self.window.hotkeys.media_enabled)
        self.assertEqual(self.window.hotkeys.handle_native_message(WM_APPCOMMAND, 0, 14 << 16), (False, 0))


if __name__ == "__main__":
    unittest.main()
