import unittest
from unittest.mock import patch

from PyQt5 import QtCore, QtWidgets

import test_music as shared
from music.storage import MusicLibrary
from ui.theme import colors


class LibraryControlsTests(unittest.TestCase):
    setUp = shared.MusicIntegrationTests.setUp
    tearDown = shared.MusicIntegrationTests.tearDown

    def prepare_library(self):
        first = self.panel.library.create_playlist("Evening")
        second = self.panel.library.create_playlist("Favourites")
        self.panel.library.add_remote_entries(first["id"], [
            {"title": "Yellow", "artist": "Coldplay"}, {"title": "One", "artist": "U2"}])
        self.panel.library.add_remote_entries(second["id"], [{"title": "Midnight City", "artist": "M83"}])
        identities = [track["id"] for track in self.panel.library.tracks()]
        self.panel.library.update_track(identities[0], album="Parachutes", genre="Alternative")
        self.panel.selected_playlist = first["id"]
        self.panel.refresh_library()
        self.panel.refresh_playlists()
        self.window.tabs.setCurrentWidget(self.panel.library_page)
        self.window.show()
        shared.application().processEvents()
        return first, second, identities

    def visible_titles(self):
        table = self.panel.library_table
        return [table.item(row, 0).text() for row in range(table.rowCount()) if not table.isRowHidden(row)]

    def test_search_covers_whole_library_and_multiple_metadata_fields(self):
        self.prepare_library()
        for text, expected in (("yellow COLDPLAY", ["Yellow"]), ("u2", ["One"]),
                               ("Parachutes", ["Yellow"]), ("alternative", ["Yellow"]),
                               ("Evening", ["Yellow", "One"]), ("favourites M83", ["Midnight City"]),
                               ("missing", []), ("", ["Yellow", "One", "Midnight City"])):
            with self.subTest(text=text):
                self.panel.library_search.setText(text)
                self.assertEqual(self.visible_titles(), expected)
        self.assertEqual(self.panel.library_table.rowCount(), 3)
        self.assertEqual(len(MusicLibrary(self.panel.library.root).tracks()), 3)

    def test_filtered_playback_and_selection_only_use_visible_tracks(self):
        _, _, identities = self.prepare_library()
        table = self.panel.library_table
        table.selectRow(0)
        self.panel.library_search.setText("M83")
        self.assertEqual(self.panel.selected_track_ids(), [])
        table.selectRow(2)
        self.assertEqual(self.panel.selected_track_ids(), [identities[2]])
        self.panel.player.preparationRequested.disconnect(self.panel.prepare_track)
        self.panel.play_library_selection()
        self.assertEqual(self.panel.player.queue.ordered_tracks(), [identities[2]])
        self.assertEqual(self.panel.player.current_track, identities[2])

    def test_context_adds_multiple_tracks_and_membership_updates_without_duplicates(self):
        first, second, identities = self.prepare_library()
        table = self.panel.library_table
        self.assertEqual(table.horizontalHeaderItem(2).text(), "Плейлист")
        self.assertEqual(table.item(0, 2).text(), "Evening")
        for row in (0, 1):
            table.selectionModel().select(table.model().index(row, 0),
                QtCore.QItemSelectionModel.Select | QtCore.QItemSelectionModel.Rows)
        menu = self.panel._track_context_menu(table, table.visualItemRect(table.item(0, 0)).center())
        submenu = next(action.menu() for action in menu.actions() if action.text() == "Добавить в плейлист")
        self.assertEqual([action.text() for action in submenu.actions()], ["Evening", "Favourites"])
        self.assertFalse(submenu.actions()[0].isEnabled())
        submenu.actions()[1].trigger()
        self.assertEqual(table.item(0, 2).text(), "Evening, Favourites")
        self.assertEqual(table.item(1, 2).text(), "Evening, Favourites")
        restarted = MusicLibrary(self.panel.library.root)
        self.assertEqual(restarted.playlists()[1]["tracks"], [identities[2], identities[0], identities[1]])
        self.panel._add_tracks_to_playlist(identities[:2], second["id"])
        self.assertEqual(self.panel.library.playlists()[1]["tracks"], restarted.playlists()[1]["tracks"])
        self.panel.library_search.setText("Favourites")
        self.assertEqual(len(self.visible_titles()), 3)
        self.panel.library.remove_playlist(second["id"])
        self.panel.refresh_playlists()
        self.assertEqual(table.item(0, 2).text(), "Evening")
        self.assertEqual(table.item(2, 2).text(), "—")
        self.assertEqual(self.visible_titles(), [])

    def test_context_add_to_active_playlist_keeps_current_order_and_position(self):
        first, _, identities = self.prepare_library()
        self.panel.active_playlist = first["id"]
        self.panel.shuffle_check.setChecked(True)
        self.panel.player.set_queue(identities[:2])
        self.panel.player.backend.setPosition(1234)
        before = self.panel.player.queue.ordered_tracks()
        self.panel._add_tracks_to_playlist([identities[2]], first["id"])
        self.assertEqual(self.panel.player.queue.ordered_tracks(), before + [identities[2]])
        self.assertEqual(self.panel.player.backend.position(), 1234)
        self.assertEqual(self.panel.player.current_track, before[0])

    def test_now_is_distinct_and_remains_first_when_tabs_are_dragged(self):
        self.prepare_library()
        tabs = self.panel.playlist_tabs
        bar = tabs.tabBar()
        self.assertIs(tabs.widget(0), self.panel.now_table)
        self.assertFalse(tabs.tabIcon(0).isNull())
        self.assertEqual(bar.tabTextColor(0).name(), colors()["accent"])
        bar.moveTab(0, 2)
        self.assertIs(tabs.widget(0), self.panel.now_table)
        bar.moveTab(2, 0)
        self.assertIs(tabs.widget(0), self.panel.now_table)
        names = {playlist["id"]: playlist["name"] for playlist in self.panel.library.playlists()}
        for index in range(1, tabs.count()):
            self.assertEqual(tabs.tabText(index), names[tabs.widget(index).property("playlist_id")])
        self.panel.refresh_playlists()
        self.assertIs(tabs.widget(0), self.panel.now_table)

    def test_close_to_tray_keeps_playback_and_double_click_restores_window(self):
        _, _, identities = self.prepare_library()
        self.panel.player.set_queue(identities)
        self.panel.player.preparationRequested.disconnect(self.panel.prepare_track)
        self.window.select_light_source("player")
        self.panel.player.play()
        self.backend.play()
        self.backend.setPosition(2345)
        with patch.object(QtWidgets.QSystemTrayIcon, "isSystemTrayAvailable", return_value=True), \
                patch.object(self.panel, "shutdown", wraps=self.panel.shutdown) as shutdown, \
                patch.object(self.window.hotkeys, "close", wraps=self.window.hotkeys.close) as hotkeys:
            self.window.close()
            self.assertFalse(self.window.isVisible())
            self.assertFalse(self.window._closing)
            self.assertFalse(self.panel.closing)
            self.assertTrue(self.panel.jobs.isRunning())
            self.assertTrue(self.panel.player.want_play)
            self.assertEqual(self.panel.player.state, "playing")
            self.assertEqual(self.backend.position(), 2345)
            shutdown.assert_not_called()
            hotkeys.assert_not_called()
            self.window.tray.tray.activated.emit(QtWidgets.QSystemTrayIcon.DoubleClick)
            self.assertTrue(self.window.isVisible())
            self.window.tray.exit_action.trigger()
            self.assertTrue(self.window._closing)
            self.assertTrue(self.window._exit_requested)
            shutdown.assert_called()
            hotkeys.assert_called_once()

    def test_close_to_tray_keeps_live_timer_running(self):
        self.window.show()
        self.window.timer.start()
        with patch.object(QtWidgets.QSystemTrayIcon, "isSystemTrayAvailable", return_value=True):
            self.window.close()
        self.assertTrue(self.window.timer.isActive())
        self.assertFalse(self.window._closing)

    def test_exit_from_hidden_window_emits_completion_once_after_worker_stops(self):
        completed = []
        self.window.exitReady.connect(lambda: completed.append(True))
        self.window.hide()
        self.window.tray.exit_action.trigger()
        self.assertTrue(shared.wait_until(lambda: bool(completed), 3))
        self.assertFalse(self.panel.jobs.isRunning())
        self.assertFalse(self.window.tray.tray.isVisible())
        self.window.request_exit()
        self.assertEqual(completed, [True])


if __name__ == "__main__":
    unittest.main()
