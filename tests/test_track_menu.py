import unittest
from unittest.mock import patch

from PyQt5 import QtCore, QtWidgets

import test_music as shared
from music.matching import choose_result, search_query
from music.storage import MusicLibrary


class TrackMenuTests(unittest.TestCase):
    setUp = shared.MusicIntegrationTests.setUp
    tearDown = shared.MusicIntegrationTests.tearDown

    def prepare_playlist(self):
        playlist = self.panel.library.create_playlist("Menu")
        self.panel.library.add_remote_entries(playlist["id"], [
            {"title": "One", "artist": "Artist"}, {"title": "Two", "artist": "Artist"}])
        self.panel.selected_playlist = playlist["id"]
        self.panel.refresh_library()
        self.panel.refresh_playlists()
        self.window.tabs.setCurrentWidget(self.panel.playlists_page)
        self.window.show()
        shared.application().processEvents()
        return self.panel.playlist_tracks

    def menu_at(self, table, row):
        return self.panel._track_context_menu(table, table.visualItemRect(table.item(row, 0)).center())

    def action(self, menu, text):
        return next(action for action in menu.actions() if action.text() == text)

    def test_right_click_targets_clicked_track_and_analyses_without_library_selection(self):
        table = self.prepare_playlist()
        table.selectRow(0)
        menu = self.menu_at(table, 1)
        identity = table.item(1, 0).data(QtCore.Qt.UserRole)
        self.assertEqual([index.row() for index in table.selectionModel().selectedRows()], [1])
        with patch.object(self.panel, "enqueue_analysis") as analyse:
            self.action(menu, "Проанализировать").trigger()
        self.assertEqual(analyse.call_args.args[0]["id"], identity)
        self.assertIsNone(self.panel._track_context_menu(table, QtCore.QPoint(5, table.viewport().height() - 1)))
        self.assertFalse({"Выше", "Ниже", "Убрать трек", "Другая запись"} &
                         {button.text() for button in self.panel.playlists_page.findChildren(QtWidgets.QPushButton)})

    def test_multiselection_is_preserved_and_deletion_only_changes_playlist(self):
        table = self.prepare_playlist()
        for row in range(2):
            table.selectionModel().select(table.model().index(row, 0),
                QtCore.QItemSelectionModel.Select | QtCore.QItemSelectionModel.Rows)
        menu = self.menu_at(table, 1)
        self.assertFalse(self.action(menu, "Переименовать").isEnabled())
        self.assertFalse(self.action(menu, "Выше").isEnabled())
        self.action(menu, "Удалить из плейлиста").trigger()
        self.assertEqual(table.rowCount(), 0)
        self.assertEqual(len(self.panel.library.tracks()), 2)
        self.assertEqual(MusicLibrary(self.panel.library.root).playlists()[0]["tracks"], [])

    def test_rename_survives_analysis_reimport_and_preserves_search_identity(self):
        table = self.prepare_playlist()
        identity = table.item(0, 0).data(QtCore.Qt.UserRole)
        self.panel.player.set_queue([identity])
        self.panel.seek_slider.setRange(0, 180)
        self.panel.seek_slider.setValue(42)
        menu = self.menu_at(table, 0)
        with patch.object(QtWidgets.QInputDialog, "getText", return_value=("My song", True)):
            self.action(menu, "Переименовать").trigger()
        self.assertEqual(self.panel.now_title.text(), "My song")
        self.assertEqual(self.panel.seek_slider.value(), 42)
        self.assertIn("My song", self.panel.playback_queue.item(0).text())
        self.panel._analysed(identity, {"title": "Old embedded title", "artist": "Artist", "duration": 180})
        self.assertEqual(table.item(0, 0).text(), "My song")
        renamed = MusicLibrary(self.panel.library.root).track(identity)
        self.assertEqual(renamed["title"], "My song")
        self.assertEqual(search_query(renamed), "artist one")
        candidate = {"title": "Artist - One", "duration": 180, "webpage_url": "https://example.test/one"}
        self.assertEqual(choose_result([candidate], renamed)["webpage_url"], candidate["webpage_url"])
        self.panel.library.add_remote_entries(self.panel.selected_playlist, [{"title": "One", "artist": "Artist"}])
        self.assertEqual(len(self.panel.library.tracks()), 2)

    def test_library_context_deletes_track_from_all_playlists(self):
        self.prepare_playlist()
        table = self.panel.library_table
        self.window.tabs.setCurrentWidget(self.panel.library_page)
        shared.application().processEvents()
        identity = table.item(0, 0).data(QtCore.Qt.UserRole)
        menu = self.menu_at(table, 0)
        self.action(menu, "Удалить из библиотеки").trigger()
        self.assertIsNone(self.panel.library.track(identity))
        self.assertEqual(len(self.panel.library.playlists()[0]["tracks"]), 1)

    def test_player_header_is_above_mode_selector_without_brand_label(self):
        layout = self.window.layout()
        self.assertIs(layout.itemAt(0).widget(), self.panel.player_header)
        self.assertIs(layout.itemAt(1).layout().itemAt(0).widget(), self.window.light_source_combo)
        self.assertNotIn("Music Light", [label.text() for label in self.window.findChildren(QtWidgets.QLabel)])


if __name__ == "__main__":
    unittest.main()
