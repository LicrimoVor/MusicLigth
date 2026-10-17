from pathlib import Path
import random
import unittest
from unittest.mock import patch

from PyQt5 import QtCore, QtGui, QtWidgets

import test_music as shared
from ui.icons import icon
from ui.MusicFileDialog import MusicFileDialog


class DesktopControlsTests(unittest.TestCase):
    setUp = shared.MusicIntegrationTests.setUp
    tearDown = shared.MusicIntegrationTests.tearDown

    def queue(self):
        playlist = self.panel.library.create_playlist("Desktop")
        self.panel.library.add_remote_entries(playlist["id"], [{"title": title} for title in ("One", "Two", "Three", "Four")])
        self.panel.selected_playlist = playlist["id"]
        self.panel.refresh_library()
        self.panel.refresh_playlists()
        return self.panel.library.playlists()[0]["tracks"]

    def test_footer_switch_and_tray_source_actions_stay_in_sync(self):
        self.assertFalse(self.window.source_button.isChecked())
        self.window.source_button.click()
        self.assertEqual(self.window.light_source, "player")
        self.assertEqual(self.window.control_stack.currentIndex(), 1)
        tray = self.window.tray
        self.assertTrue(tray.sources["player"].isChecked())
        self.assertFalse(tray.colors_menu.menuAction().isVisible())
        self.assertTrue(tray.volume_action.isVisible())
        tray.sources["live"].trigger()
        self.assertEqual(self.window.light_source, "live")
        self.assertFalse(self.window.source_button.isChecked())
        self.assertTrue(tray.colors_menu.menuAction().isVisible())
        self.assertFalse(tray.music_next.isVisible())
        tray.live_modes[-1].trigger()
        self.assertEqual(self.window.mode, "static")

    def test_tray_player_controls_presets_and_volume_while_window_is_hidden(self):
        identities = self.queue()
        self.panel.player.set_queue(identities)
        self.window.select_light_source("player")
        self.window.hide()
        tray = self.window.tray
        self.panel.player.preparationRequested.disconnect(self.panel.prepare_track)
        tray.music_next.trigger()
        self.assertEqual(self.panel.player.current_track, identities[1])
        tray.music_previous.trigger()
        self.assertEqual(self.panel.player.current_track, identities[0])
        tray.music_pause.trigger()
        self.assertTrue(self.panel.player.want_play)
        tray.music_pause.trigger()
        self.assertFalse(self.panel.player.want_play)
        tray.volume.setValue(37)
        self.assertEqual(self.panel.volume_slider.value(), 37)
        self.panel.volume_slider.setValue(61)
        self.assertEqual(tray.volume.value(), 61)
        tray.presets[-1].trigger()
        self.panel.apply_show_settings()
        self.assertEqual(self.panel.settings["preset"], self.panel.preset_combo.itemText(len(tray.presets) - 1))
        self.assertFalse(self.window.isVisible())
        tray.show_window()
        self.assertTrue(self.window.isVisible())

    def test_tray_color_dialog_works_with_main_window_hidden(self):
        self.window.hide()
        tray = self.window.tray
        tray.colors_menu.actions()[0].trigger()
        self.assertIsNotNone(tray.color_dialog)
        self.assertTrue(tray.color_dialog.isVisible())
        tray.color_dialog.setCurrentColor(QtGui.QColor(10, 20, 30))
        tray.color_dialog.accept()
        self.assertEqual(self.window.quiet_color, (10, 20, 30))
        self.assertIsNone(tray.color_dialog)
        self.assertFalse(self.window.isVisible())

    def test_add_dialog_accepts_a_directory_and_a_file_in_one_selection(self):
        directory = self.root / "album"
        directory.mkdir()
        nested = directory / "one.wav"
        single = self.root / "two.wav"
        shared.write_beats(nested, duration=2)
        shared.write_beats(single, duration=2)
        self.panel.add_music_button.click()
        dialog = self.panel.import_dialog
        self.assertIsInstance(dialog, MusicFileDialog)
        dialog.setDirectory(str(self.root))
        view = dialog.findChild(QtWidgets.QTreeView, "treeView")
        view.show()
        model = view.model()
        self.assertTrue(shared.wait_until(lambda: model.index(str(directory)).isValid() and model.index(str(single)).isValid(), 3))
        for path in (directory, single):
            view.selectionModel().select(model.index(str(path)),
                QtCore.QItemSelectionModel.Select | QtCore.QItemSelectionModel.Rows)
        self.assertEqual(set(map(Path, dialog.selectedFiles())), {directory, single})
        accept = dialog.findChild(QtWidgets.QDialogButtonBox).button(QtWidgets.QDialogButtonBox.Open)
        self.assertTrue(accept.isEnabled())
        accept.click()
        self.assertIsNone(self.panel.import_dialog)
        self.assertTrue(shared.wait_until(lambda: len(self.panel.library.tracks()) == 2, 5))
        self.assertEqual({Path(track["path"]).resolve() for track in self.panel.library.tracks()}, {nested.resolve(), single.resolve()})
        labels = {button.text() for button in self.panel.library_page.findChildren(QtWidgets.QPushButton)}
        self.assertNotIn("Play", labels)
        self.assertNotIn("Удалить", labels)
        self.assertNotIn("Повторить анализ", labels)
        self.assertNotIn("Отменить анализ", labels)
        self.assertNotIn("Добавить папку", labels)

    def test_add_button_accepts_a_folder_without_navigating_into_it(self):
        directory = self.root / "album"
        directory.mkdir()
        self.panel.choose_music()
        dialog = self.panel.import_dialog
        dialog.setDirectory(str(self.root))
        dialog.selectFile(str(directory))
        with patch.object(self.panel.jobs, "import_paths") as importer:
            accept = dialog.findChild(QtWidgets.QDialogButtonBox).button(QtWidgets.QDialogButtonBox.Open)
            self.assertTrue(accept.isEnabled())
            accept.click()
        self.assertIsNone(self.panel.import_dialog)
        self.assertEqual([Path(path) for path in importer.call_args.args[0]], [directory])

    def test_library_drop_queues_files_and_folders_in_background(self):
        mime = QtCore.QMimeData()
        paths = [str(self.root / "album"), str(self.root / "song.wav")]
        mime.setUrls([QtCore.QUrl.fromLocalFile(path) for path in paths])
        event = QtGui.QDropEvent(QtCore.QPointF(10, 10), QtCore.Qt.CopyAction, mime,
                                QtCore.Qt.LeftButton, QtCore.Qt.NoModifier)
        with patch.object(self.panel.jobs, "import_paths") as importer:
            self.panel.library_table.dropEvent(event)
        self.assertTrue(event.isAccepted())
        self.assertEqual(list(map(Path, importer.call_args.args[0])), list(map(Path, paths)))

    def test_now_shows_actual_shuffle_order_marks_current_and_supports_jumping(self):
        identities = self.queue()
        self.panel.player.queue.rng = random.Random(1)
        self.panel.shuffle_check.setChecked(True)
        self.panel.player.set_queue(identities)
        order = self.panel.player.queue.ordered_tracks()
        now = self.panel.now_table
        self.assertEqual(now.track_ids(), order)
        self.assertNotEqual(order, identities)
        self.panel.playlist_tabs.setCurrentWidget(now)
        self.assertEqual(self.panel.player.queue.ordered_tracks(), order)
        self.assertTrue(now.item(0, 0).font().bold())
        self.panel.player.preparationRequested.disconnect(self.panel.prepare_track)
        self.panel.player.next()
        self.assertEqual(self.panel.player.current_track, order[1])
        self.assertTrue(now.item(1, 0).font().bold())
        self.assertFalse(now.item(0, 0).font().bold())
        now.cellDoubleClicked.emit(3, 0)
        self.assertEqual(self.panel.player.current_track, order[3])
        self.assertTrue(self.panel.player.want_play)
        self.assertEqual(now.track_ids(), order)
        self.panel.player.listenCompleted.emit(order[3])
        self.assertEqual(now.item(3, 3).text(), "1")

    def test_now_reorder_preserves_shuffle_order_and_playback_position(self):
        identities = self.queue()
        self.panel.shuffle_check.setChecked(True)
        self.panel.player.set_queue(identities)
        self.panel.playlist_tabs.setCurrentWidget(self.panel.now_table)
        current = self.panel.player.current_track
        self.panel.player.backend.setPosition(1234)
        before = self.panel.now_table.track_ids()
        self.panel.now_table.move_row(1, 3)
        expected = [before[0], before[2], before[3], before[1]]
        self.assertEqual(self.panel.player.queue.ordered_tracks(), expected)
        self.assertEqual(self.panel.player.current_track, current)
        self.assertEqual(self.panel.player.backend.position(), 1234)
        self.assertTrue(self.panel.player.queue.shuffle)
        self.assertEqual(self.panel.library.playlists()[0]["tracks"], identities)

    def test_deleting_current_from_now_continues_with_next_without_reshuffling(self):
        identities = self.queue()
        self.panel.shuffle_check.setChecked(True)
        self.panel.player.set_queue(identities)
        self.panel.player.preparationRequested.disconnect(self.panel.prepare_track)
        self.panel.player.play()
        self.panel.playlist_tabs.setCurrentWidget(self.panel.now_table)
        before = self.panel.now_table.track_ids()
        self.panel.now_table.selectRow(0)
        position = self.panel.now_table.visualItemRect(self.panel.now_table.item(0, 0)).center()
        menu = self.panel._track_context_menu(self.panel.now_table, position)
        next(action for action in menu.actions() if action.text() == "Удалить из очереди").trigger()
        self.assertEqual(self.panel.player.current_track, before[1])
        self.assertEqual(self.panel.player.queue.ordered_tracks(), before[1:])
        self.assertEqual(self.panel.now_table.track_ids(), before[1:])
        self.assertTrue(self.panel.player.want_play)
        self.assertEqual(self.panel.library.playlists()[0]["tracks"], identities)

    def test_playlist_toolbar_is_in_tab_corner_and_icons_are_available(self):
        self.assertIs(self.panel.playlist_tabs.cornerWidget(QtCore.Qt.TopRightCorner), self.panel.playlist_toolbar)
        self.assertNotIn("play", self.panel.playlist_buttons)
        for button in self.panel.playlist_buttons.values():
            self.assertEqual(button.text(), "")
            self.assertTrue(button.toolTip())
            self.assertFalse(button.icon().pixmap(24, 24).isNull())
        self.assertFalse(icon("app").pixmap(32, 32).isNull())
        self.assertFalse(self.window.windowIcon().isNull())
        self.window.tray.close()
        self.assertFalse(self.window.tray.tray.isVisible())


if __name__ == "__main__":
    unittest.main()
