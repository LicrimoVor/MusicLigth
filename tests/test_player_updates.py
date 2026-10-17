import json
import sys
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import numpy as np
from PyQt5 import QtCore, QtGui, QtWidgets

import test_music as shared
from music.player import MusicPlayer
from music.sources import SourceImporter, classify_line
from music.storage import MusicLibrary, atomic_json
from ui.PlaylistTable import PlaylistTable
from ui.widgets import BeatGraphWidget


class ImportSourcesTests(unittest.TestCase):
    def test_mixed_lines_detect_services_and_keep_successful_rows(self):
        with tempfile.TemporaryDirectory() as root:
            importer = SourceImporter(root)
            with patch.object(importer, "_youtube", return_value=("YT list", [{"title": "One", "artist": "A"}], True)), \
                    patch.object(importer, "_yandex", return_value=("YM", [{"title": "Two", "artist": "B"}], False)), \
                    patch.object(importer, "_vk", side_effect=ValueError("VK login required")):
                result = importer.resolve("youtube.com/playlist?list=abc\nhttps://music.yandex.ru/track/12\nArtist — Name\nhttps://vk.com/audio1_2\nhttps://invalid.test/song")
            self.assertEqual([item["name"] for item in result["documents"]], ["YT list"])
            self.assertEqual([item["title"] for item in result["entries"]], ["Two", "Name"])
            self.assertEqual(len(result["errors"]), 2)
            self.assertIn("Строка 4", result["errors"][0])
            self.assertEqual(classify_line("Song: Part One")[0], "name")
            with self.assertRaises(ValueError):
                classify_line("file:///C:/private")

    def test_yandex_playlist_resolves_track_ids_and_durations(self):
        with tempfile.TemporaryDirectory() as root:
            importer = SourceImporter(root)
            responses = [json.dumps({"result": {"title": "YM", "tracks": [{"id": 123}]}}),
                         json.dumps({"result": [{"id": 123, "title": "Song", "artists": [{"name": "Artist"}], "durationMs": 123000}]})]
            with patch.object(importer, "_request", side_effect=responses):
                result = importer.resolve("https://music.yandex.ru/users/test/playlists/10")
            self.assertFalse(result["errors"])
            track = result["documents"][0]["entries"][0]
            self.assertEqual((track["title"], track["artist"], track["duration"]), ("Song", "Artist", 123))

    def test_vk_html_and_saved_vk_links_are_imported_without_network(self):
        with tempfile.TemporaryDirectory() as root:
            importer = SourceImporter(root, Path(root) / "playlists")
            html = ('<div data-audio-id="1_2"><a data-testid="MusicTrackRow_Title" href="/audio1_2">Song</a>'
                    '<a data-testid="MusicTrackRow_Authors">Artist</a><span data-testid="MusicTrackRow_Duration">2:00</span></div>')
            result = importer.resolve(html)
            document = result["documents"][0]
            atomic_json(importer.catalog / "saved.json", document)
            with patch.object(importer, "_request", side_effect=AssertionError("use existing metadata")):
                result = importer.resolve("https://vk.com/audio1_2")
            self.assertEqual(result["entries"][0]["title"], "Song")
            self.assertFalse(result["errors"])


class FiniteRepeatTests(unittest.TestCase):
    def test_repeat_counter_decreases_and_manual_skip_clears_it(self):
        shared.application()
        backend = shared.FakeMediaBackend()
        lookup = lambda identity: {"id": identity, "status": "Ready", "duration": 10}
        player = MusicPlayer(lookup, backend=backend)
        player.trackRequested.connect(lambda identity: player.start_ready(identity, "test.wav"))
        listened = []
        player.listenCompleted.connect(listened.append)
        player.set_queue(["a", "b"])
        player.play()
        player.add_repeat()
        player.add_repeat()
        for remaining in (1, 0):
            backend.mediaStatusChanged.emit(shared.QtMultimedia.QMediaPlayer.EndOfMedia)
            self.assertEqual(player.current_track, "a")
            self.assertEqual(player.repeat_remaining, remaining)
        backend.mediaStatusChanged.emit(shared.QtMultimedia.QMediaPlayer.EndOfMedia)
        self.assertEqual(player.current_track, "b")
        self.assertEqual(listened, ["a", "a", "a"])
        player.add_repeat()
        player.previous(force=True)
        self.assertEqual(player.repeat_remaining, 0)
        player.stop()


class PlayerUpdatesIntegrationTests(unittest.TestCase):
    setUp = shared.MusicIntegrationTests.setUp
    tearDown = shared.MusicIntegrationTests.tearDown

    def test_table_counts_status_duration_and_order_are_persisted(self):
        playlist = self.panel.library.create_playlist("Table")
        self.panel.library.add_remote_entries(playlist["id"], [
            {"title": "One", "artist": "Artist", "duration": 183}, {"title": "Two", "duration": 55}])
        self.panel.selected_playlist = playlist["id"]
        self.panel.refresh_playlists()
        table = self.panel.playlist_tracks
        self.assertIsInstance(table, QtWidgets.QTableWidget)
        self.assertEqual(table.rowCount(), 2)
        self.assertEqual(table.item(0, 2).text(), "3:03")
        self.assertIn("Не скачан", table.item(0, 4).text())
        identity = table.item(0, 0).data(QtCore.Qt.UserRole)
        self.panel.library.update_track(identity, status="Error", error="Unavailable")
        self.panel._playlist_selected()
        self.assertIn("Error", table.item(0, 4).text())
        self.assertEqual(table.item(0, 4).toolTip(), "Unavailable")
        self.panel.player.listenCompleted.emit(identity)
        self.assertEqual(table.item(0, 3).text(), "1")
        table.selectRow(0)
        self.panel.move_playlist_track(1)
        self.assertEqual(table.item(1, 0).data(QtCore.Qt.UserRole), identity)
        restarted = MusicLibrary(self.panel.library.root)
        self.assertEqual(restarted.track(identity)["play_count"], 1)
        self.assertEqual(restarted.playlists()[0]["tracks"][1], identity)
        from types import SimpleNamespace
        accepted, actions = [], []
        event = SimpleNamespace(source=lambda: table, pos=lambda: QtCore.QPoint(5, 5),
            setDropAction=actions.append, accept=lambda: accepted.append(True))
        table.dropEvent(event)
        self.assertEqual(table.rowCount(), 2)
        self.assertEqual(table.item(0, 0).data(QtCore.Qt.UserRole), identity)
        self.assertEqual(actions, [QtCore.Qt.CopyAction])
        self.assertTrue(accepted)

    def test_import_dialog_and_mixed_import_run_in_worker(self):
        self.panel.open_import()
        dialog = self.panel.text_import_dialog
        dialog.text.setPlainText("Artist — First\nOther — Second")
        dialog.submit.click()
        self.assertIsNone(self.panel.text_import_dialog)
        self.assertTrue(shared.wait_until(lambda: len(self.panel.library.tracks()) == 2, 3))
        self.assertEqual(self.panel.playlist_tracks.rowCount(), 2)
        buttons = self.panel.playlists_page.findChildren(QtWidgets.QToolButton)
        self.assertEqual(len([button for button in buttons if button.toolTip() == "Импорт"]), 1)
        self.assertFalse(hasattr(self.panel, "repeat_combo"))
        self.panel.repeat_button.click()
        self.panel.repeat_button.click()
        self.assertEqual(self.panel.repeat_button.text(), "2")
        self.panel.repeat_button.customContextMenuRequested.emit(QtCore.QPoint())
        self.assertEqual(self.panel.repeat_button.text(), "0")

    def test_hotkey_dispatch_switches_tracks_and_closes_registration(self):
        playlist = self.panel.library.create_playlist("Keys")
        for title in ("A", "B"):
            self.panel.library.add_search_track(playlist["id"], title)
        ids = self.panel.library.playlists()[0]["tracks"]
        with patch.object(self.panel.jobs, "analyse"):
            self.panel.player.set_queue(ids)
            self.window.hotkeys.activated.emit("next")
            self.assertTrue(shared.wait_until(lambda: self.panel.player.current_track == ids[1], 2))
            self.backend.clock = 8000
            self.window.hotkeys.activated.emit("previous")
            self.assertTrue(shared.wait_until(lambda: self.panel.player.current_track == ids[0], 2))
        self.window.hotkeys.close()
        self.assertFalse(self.window.hotkeys.active)
        self.assertFalse(self.window.hotkeys.registered)


class PlotPerformanceTests(unittest.TestCase):
    def test_dense_plot_renders_without_slow_path_stroking_and_hidden_timer(self):
        shared.application()
        graph = BeatGraphWidget()
        graph.resize(1500, 800)
        for index in range(600):
            graph.add_level((index % 31) / 31, (index % 11) / 11, index % 8 == 0, index / 75)
        image = QtGui.QImage(graph.size(), QtGui.QImage.Format_ARGB32_Premultiplied)
        graph.render(image)
        start = time.perf_counter()
        for _ in range(4):
            graph.render(image)
        self.assertLess((time.perf_counter() - start) / 4, 0.12)
        self.assertFalse(graph.paint_timer.isActive())
        self.assertEqual(len(graph.history), 600)
