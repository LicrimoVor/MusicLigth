import unittest

from PyQt5 import QtCore, QtGui, QtWidgets

import test_music as shared
from music.storage import MusicLibrary
from ui.theme import apply_theme, colors, current_theme
from ui.widgets import BeatGraphWidget


class ThemeLayoutTests(unittest.TestCase):
    setUp = shared.MusicIntegrationTests.setUp

    def tearDown(self):
        shared.MusicIntegrationTests.tearDown(self)
        apply_theme("dark")

    def test_theme_switch_in_footer_updates_palette_icons_graph_and_saved_settings(self):
        window = self.window
        self.assertIs(window.theme_button.parent(), window.footer)
        before_icon = self.panel.next_button.icon().cacheKey()
        graph = window.beat_graph_widget
        before_graph = graph.background_pixmap().cacheKey()
        self.backend.setPosition(1234)
        window.theme_button.click()
        self.assertEqual(current_theme(), "light")
        self.assertEqual(shared.application().palette().color(QtGui.QPalette.Base).name(), "#ffffff")
        self.assertNotEqual(self.panel.next_button.icon().cacheKey(), before_icon)
        self.assertNotEqual(graph.background_pixmap().cacheKey(), before_graph)
        self.assertEqual(MusicLibrary(self.panel.library.root).settings()["theme"], "light")
        self.assertEqual(self.backend.position(), 1234)
        window.theme_button.click()
        self.assertEqual(current_theme(), "dark")
        self.assertEqual(MusicLibrary(self.panel.library.root).settings()["theme"], "dark")

    def test_music_colors_are_in_right_sidebar_and_beat_page_only_contains_graph(self):
        self.window.select_light_source("player")
        layout = self.window.player_control_layout
        self.assertIs(layout.itemAt(1).widget(), self.window.visual_panel)
        self.assertIs(layout.itemAt(2).widget(), self.window.music_colors_sidebar)
        for button in self.panel.music_color_buttons:
            self.assertTrue(self.window.music_colors_sidebar.isAncestorOf(button))
            self.assertFalse(self.panel.settings_page.isAncestorOf(button))
        self.assertEqual(self.window.beat_page.layout().count(), 1)
        self.assertIs(self.window.beat_page.layout().itemAt(0).widget(), self.window.beat_graph_widget)
        self.assertFalse(self.window.beat_page.findChildren(QtWidgets.QLabel))
        self.assertFalse(self.window.beat_page.findChildren(QtWidgets.QPushButton))
        self.window.update_beat_graph(0.5, 0.6, True, 1.0)
        self.assertEqual(self.window.beat_graph_widget.history[-1], (1.0, 0.5, 0.6, True))

    def test_mix_is_a_checkable_icon_button_with_distinct_on_and_off_states(self):
        button = self.panel.shuffle_check
        self.assertIsInstance(button, QtWidgets.QToolButton)
        self.assertEqual(button.text(), "")
        self.assertFalse(button.isChecked())
        before = button.icon().cacheKey()
        button.click()
        self.assertTrue(button.isChecked())
        self.assertTrue(self.panel.player.queue.shuffle)
        self.assertNotEqual(button.icon().cacheKey(), before)
        self.assertEqual(button.toolTip(), "Микс включён")
        self.assertTrue(MusicLibrary(self.panel.library.root).settings()["shuffle"])
        button.click()
        self.assertFalse(self.panel.player.queue.shuffle)
        self.assertEqual(button.icon().cacheKey(), before)

    def test_dark_tab_corners_and_table_corner_do_not_paint_system_white(self):
        first = self.panel.library.create_playlist("First")
        self.panel.library.create_playlist("Second")
        self.panel.library.add_search_track(first["id"], "Track")
        self.panel.selected_playlist = first["id"]
        self.panel.refresh_library()
        self.panel.refresh_playlists()
        self.window.tabs.setCurrentWidget(self.panel.playlists_page)
        self.window.show()
        shared.application().processEvents()
        bar = self.panel.playlist_tabs.tabBar()
        self.assertFalse(bar.drawBase())
        image = bar.grab().toImage()
        ratio = image.devicePixelRatio()
        for index in range(bar.count() - 1):
            rect = bar.tabRect(index)
            for point in (QtCore.QPoint(rect.right(), rect.top()), QtCore.QPoint(rect.left(), rect.top())):
                color = image.pixelColor(round(point.x() * ratio), round(point.y() * ratio))
                self.assertLess(color.lightness(), 180)
        corner = self.panel.playlist_tracks.findChild(QtWidgets.QAbstractButton)
        self.assertIsNotNone(corner)
        corner_image = corner.grab().toImage()
        pixel = corner_image.pixelColor(corner_image.width() // 2, corner_image.height() // 2)
        self.assertLess(pixel.lightness(), 180)

    def test_desktop_widget_uses_current_theme(self):
        widget = self.window.desktop_widget
        widget.set_enabled(True)
        self.window.set_theme("light")
        self.assertIn(colors()["window"], widget.styleSheet())
        self.assertEqual(widget.graph.background_pixmap().toImage().pixelColor(0, 0).name(), colors()["plot"])


if __name__ == "__main__":
    unittest.main()
