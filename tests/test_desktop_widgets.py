import unittest
from unittest.mock import patch

from PyQt5 import QtCore, QtWidgets

import test_music as shared
from music.lyrics import LyricsCache
from music.storage import MusicLibrary
from ui.theme import apply_theme


class DesktopWidgetsTests(unittest.TestCase):
    setUp = shared.MusicIntegrationTests.setUp
    tearDown = shared.MusicIntegrationTests.tearDown

    def tracks(self):
        playlist = self.panel.library.create_playlist("Widgets")
        self.panel.library.add_remote_entries(playlist["id"], [
            {"title": "One", "artist": "Artist", "duration": 120},
            {"title": "Two", "artist": "Artist", "duration": 100}])
        tracks = self.panel.library.tracks()
        for track, duration in zip(tracks, (120, 100)):
            self.panel.library.update_track(track["id"], duration=duration)
        tracks = self.panel.library.tracks()
        cache = LyricsCache(self.panel.library.root)
        cache.save(tracks[0], {"status": "found", "source": "Local", "plain": "First\nSecond",
                               "synced": "[00:01]First\n[00:03]Second"})
        cache.save(tracks[1], {"status": "found", "source": "Local", "plain": "Other text", "synced": ""})
        self.panel.player.preparationRequested.disconnect(self.panel.prepare_track)
        self.panel.player.set_queue([t["id"] for t in tracks])
        return tracks

    def test_footer_and_tray_share_independent_widget_switches_and_save_positions(self):
        self.assertIs(self.window.widgets_button.parent(), self.window.footer)
        self.assertEqual([a.text() for a in self.window.widgets_menu.actions()], ["Бит", "Музыка", "Текст"])
        self.assertEqual(self.window.tray.widgets_menu.actions(), self.window.widgets_menu.actions())
        for kind, widget in self.window.desktop_widgets.items():
            self.window.widget_actions[kind].trigger()
            self.assertTrue(widget.isVisible())
        self.window.show()
        with patch.object(QtWidgets.QSystemTrayIcon, "isSystemTrayAvailable", return_value=True):
            self.window.close()
        self.assertFalse(self.window.isVisible())
        self.assertTrue(all(w.isVisible() for w in self.window.desktop_widgets.values()))
        widget = self.window.desktop_widgets["music"]
        widget.move(80, 100)
        widget.positionSaved.emit()
        widget.close()
        self.assertFalse(self.window.widget_actions["music"].isChecked())
        self.assertTrue(self.window.widget_actions["lyrics"].isChecked())
        preferences = MusicLibrary(self.panel.library.root).settings()["desktop_widgets"]
        self.assertEqual(preferences["music"], {"position": [80, 100], "enabled": False})
        self.assertTrue(preferences["lyrics"]["enabled"])
        self.window.request_exit()
        self.assertTrue(MusicLibrary(self.panel.library.root).settings()["desktop_widgets"]["lyrics"]["enabled"])

    def test_music_widget_uses_same_player_transport_and_volume_when_main_window_hidden(self):
        tracks = self.tracks()
        widget = self.window.desktop_widgets["music"]
        widget.set_enabled(True)
        self.window.hide()
        self.assertEqual(widget.title.text(), "One")
        self.assertIs(self.panel.player.backend, self.backend)
        widget.next.click()
        self.assertEqual(self.panel.player.current_track, tracks[1]["id"])
        self.assertEqual(widget.title.text(), "Two")
        widget.previous.click()
        self.assertEqual(self.panel.player.current_track, tracks[0]["id"])
        widget.play_pause.click()
        self.assertEqual(self.window.light_source, "player")
        self.assertTrue(self.panel.player.want_play)
        widget.play_pause.click()
        self.assertFalse(self.panel.player.want_play)
        widget.volume.setValue(32)
        self.assertEqual(self.backend.volume, 32)
        self.panel.volume_slider.setValue(64)
        self.assertEqual(widget.volume.value(), 64)
        self.backend.setPosition(3500)
        self.assertEqual(widget.seek.value(), 3500)
        self.assertEqual(widget.time.text(), "0:03 / 2:00")
        self.panel.player.prepared = self.panel.player.current_track
        widget.seek.setValue(5400)
        widget.seek.sliderReleased.emit()
        self.assertEqual(self.backend.position(), 5400)

    def test_text_widget_uses_cache_tracks_audio_clock_and_refreshes_plain_text(self):
        self.tracks()
        widget = self.window.desktop_widgets["lyrics"]
        with patch.object(widget.client.manager, "get") as get:
            widget.set_enabled(True)
            self.assertEqual(widget.stack.currentIndex(), 1)
            self.backend.setPosition(1500)
            self.assertEqual(widget.current_line.text(), "First")
            self.assertEqual(widget.next_line.text(), "Second")
            self.backend.setPosition(3500)
            self.assertEqual(widget.current_line.text(), "Second")
            self.backend.setPosition(500)
            self.assertEqual(widget.current_line.text(), "♪")
            self.panel.player.next()
            self.assertEqual(widget.plain.toPlainText(), "Other text")
            self.assertEqual(widget.stack.currentIndex(), 0)
            self.assertFalse(get.called)
        widget.close()
        with patch.object(widget.client, "request") as request:
            self.panel.player.previous(force=True)
            self.assertFalse(request.called)

    def test_local_lyrics_import_refreshes_visible_widget_and_ignores_stale_results(self):
        tracks = self.tracks()
        widget = self.window.desktop_widgets["lyrics"]
        widget.set_enabled(True)
        self.panel.toggle_lyrics()
        path = self.root / "local.lrc"
        path.write_text("[00:01]Local words", encoding="utf-8")
        self.panel.lyrics_dialog.load_file(str(path))
        self.backend.setPosition(1500)
        self.assertEqual(widget.current_line.text(), "Local words")
        self.panel.player.next()
        widget._ready(tracks[0]["id"], {"status": "found", "plain": "Stale", "synced": ""})
        self.assertEqual(widget.plain.toPlainText(), "Other text")
        self.panel.player.set_queue([])
        self.assertIn("Выберите", widget.plain.toPlainText())

    def test_large_library_has_no_progress_widgets_and_theme_does_not_reset_qt_style(self):
        self.tracks()
        for track in self.panel.library.data["tracks"]:
            track.update(status="Ready", progress=100)
        self.panel.refresh_library()
        self.assertFalse(self.panel.library_table.findChildren(QtWidgets.QProgressBar))
        self.assertEqual(self.panel.library_table.item(0, 6).data(QtCore.Qt.UserRole), 100)
        with patch.object(shared.application(), "setStyle", wraps=shared.application().setStyle) as set_style:
            apply_theme("light")
            apply_theme("dark")
            apply_theme("dark")
            self.assertFalse(set_style.called)
        self.panel.library.data["tracks"][0]["status"] = "Pending"
        self.panel._progress(self.panel.library.tracks()[0]["id"], 42, "Analysis")
        self.assertEqual(self.panel.library_table.item(0, 6).data(QtCore.Qt.UserRole), 42)

    def test_lyrics_settings_apply_sizes_and_line_counts_and_survive_reload(self):
        self.tracks()
        widget = self.window.desktop_widgets["lyrics"]
        widget.set_enabled(True)
        widget.show_settings()
        controls = widget.settings_controls
        for key, value in {"width": 600, "height": 450, "font_size": 28, "lines_before": 0, "lines_after": 3}.items():
            controls[key].setValue(value)
        self.backend.setPosition(1500)
        self.assertEqual(len(widget.line_labels), 4)
        self.assertIs(widget.current_line, widget.line_labels[0])
        self.assertEqual(widget.current_line.text(), "First")
        self.assertEqual(widget.line_labels[1].text(), "Second")
        widget.settings_dialog.close()
        prefs = self.panel.library.settings()["desktop_widgets"]["lyrics"]
        self.assertEqual(prefs["size"], [600, 450])
        self.assertEqual((prefs["lines_before"], prefs["lines_after"], prefs["font_size"]), (0, 3, 28))
        from ui.DesktopMusicWidgets import DesktopLyricsWidget
        restored = DesktopLyricsWidget(self.panel, prefs)
        try:
            self.assertEqual((restored.width(), restored.height()), (600, 450))
            self.assertEqual(len(restored.line_labels), 4)
            self.assertEqual(restored.font_size, 28)
        finally:
            restored.shutdown()


if __name__ == "__main__":
    unittest.main()
