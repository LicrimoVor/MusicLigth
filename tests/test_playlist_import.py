import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

import soundfile as sf
from PyQt5 import QtCore

from music.downloads import MusicDownloader, choose_result
from music.jobs import MusicJobs
from music.playlist_import import load_playlist, main, parse_playlist, save_playlist
from music.storage import CancelledAnalysis, MusicLibrary
import test_music as shared


def row(identity, title="First &amp; Last", artist="Artist", duration="3:10"):
    return (f'<div data-audio-id="{identity}"><div data-testid="MusicTrackRow">'
            '<img src="unused"><input type="hidden"><svg><path></path></svg>'
            f'<a data-testid="MusicTrackRow_Title" href="/audio{identity}">{title}<span></span></a>'
            f'<a data-testid="MusicTrackRow_Authors"><div>{artist}</div></a>'
            f'<div data-testid="MusicTrackRow_Duration"><h4>{duration}</h4></div></div></div>')


class PlaylistImportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_two_vk_layouts_unicode_entities_duplicates_and_nested_elements(self):
        fragments = [
            '<div role="list"><output></output>' + row("1_2") + row("1_3", "Next", "Two") + '</div>',
            '<div data-testid="MusicPlaylistModal_Tracks"><div data-testid="musicplayliststatistics-title">Треки</div>'
            '<div data-testid="MusicPlaylistTracks_Items"><div data-testid="MusicPlaylistTracks_MusicTrackRow">'
            + row("1_2", "Почерк&nbsp;live", "Ваня &amp; Друг") + '</div>'
            + row("1_2", "Почерк&nbsp;live", "Ваня &amp; Друг") + '</div></div>',
        ]
        for index, fragment in enumerate(fragments):
            source = self.root / f"{index}.txt"
            source.write_text(fragment, encoding="utf-8-sig")
            entries = parse_playlist(source)["entries"]
            self.assertEqual(len(entries), 2 if index == 0 else 1)
            self.assertEqual(entries[0]["duration"], 190)
            self.assertEqual(entries[0]["source_url"], "https://vk.com/audio1_2")
            self.assertEqual(entries[0]["title"], "First & Last" if index == 0 else "Почерк live")
            self.assertEqual(entries[0]["artist"], "Artist" if index == 0 else "Ваня & Друг")

    def test_cli_saves_lists_and_library_import_preserves_edits_on_repeat(self):
        source = self.root / "Русский.txt"
        source.write_text(row("1_2", "Почерк", "Ваня"), encoding="utf-8")
        self.assertEqual(main([str(self.root)]), 0)
        saved = next((self.root / "playlists").glob("*.json"))
        document = load_playlist(saved)
        self.assertEqual(document["entries"][0]["title"], "Почерк")
        library = MusicLibrary(self.root / "music")
        playlist = library.import_html_playlist(document)
        added = library.add_search_track(playlist["id"], "New", "Artist")
        library.set_playlist_tracks(playlist["id"], [added["id"], playlist["tracks"][0]])
        library.import_html_playlist(document)
        self.assertEqual(len(library.tracks()), 2)
        restarted = MusicLibrary(library.root)
        self.assertEqual(restarted.playlists()[0]["tracks"][0], added["id"])
        self.assertTrue(all(track["status"] == "NotDownloaded" for track in restarted.tracks()))

    def test_startup_loads_json_without_reading_html(self):
        shared.application()
        source = self.root / "main.txt"
        source.write_text(row("1_2"), encoding="utf-8")
        saved, document = save_playlist(source)
        source.unlink()
        jobs = MusicJobs(self.root / "music", playlist_root=self.root / "playlists")
        found = []
        jobs.catalogFound.connect(found.append)
        jobs.load_playlists([self.root])
        jobs.start()
        try:
            with patch("music.playlist_import.parse_playlist", side_effect=AssertionError("HTML must not be reparsed")):
                self.assertTrue(shared.wait_until(lambda: bool(found), 3))
            self.assertEqual(found[0], document)
        finally:
            jobs.stop()
            self.assertTrue(jobs.wait(3000))

    def test_bad_json_is_rejected(self):
        bad = self.root / "bad.json"
        bad.write_text(json.dumps({"version": 1, "name": "Bad", "source": "x", "entries": [None]}))
        with self.assertRaises(ValueError):
            load_playlist(bad)

    def test_startup_preserves_removed_tracks_and_deleted_playlists(self):
        source = self.root / "main.txt"
        source.write_text(row("1_2") + row("1_3", "Next"), encoding="utf-8")
        _, document = save_playlist(source)
        library = MusicLibrary(self.root / "music")
        playlist = library.import_html_playlist(document)
        library.set_playlist_tracks(playlist["id"], playlist["tracks"][:1])
        reloaded = library.import_html_playlist(document, from_catalog=True)
        self.assertEqual(len(reloaded["tracks"]), 1)
        library.remove_playlist(playlist["id"])
        self.assertIsNone(MusicLibrary(library.root).import_html_playlist(document, from_catalog=True))
        restored = library.import_html_playlist(document)
        self.assertEqual(len(restored["tracks"]), 2)


class DownloadTests(unittest.TestCase):
    def test_search_chooses_artist_title_duration_and_rejects_wrong_versions(self):
        entries = [{"title": "Artist - Song remix", "duration": 190, "url": "https://example.test/remix"},
                   {"title": "Other - Song", "duration": 190},
                   {"title": "Artist - Song (official audio)", "duration": 191, "url": "https://example.test/song"},
                   {"title": "Artist - Song", "duration": 3600}]
        track = {"title": "Song", "artist": "Artist", "expected_duration": 190}
        self.assertEqual(choose_result(entries, track)["url"], "https://example.test/song")
        with self.assertRaises(ValueError):
            choose_result(entries[:2], track)
        self.assertEqual(choose_result(entries, track | {"expected_duration": 47})["url"], "https://example.test/song")

    def test_download_converts_audio_and_reuses_local_cache_without_network(self):
        with tempfile.TemporaryDirectory() as temporary:
            downloader = MusicDownloader(temporary)
            original_run = downloader._run
            result = {"title": "Artist - Song", "duration": 190, "url": "https://www.youtube.com/watch?v=test"}

            def run(args, directory, cancel, timeout=180):
                if any(value.startswith("ytsearch5:") for value in args):
                    return json.dumps({"entries": [result]})
                if "yt_dlp" in args:
                    shared.write_beats(Path(directory) / "source.wav", duration=2)
                    return ""
                return original_run(args, directory, cancel, timeout)

            track = {"title": "Song", "artist": "Artist", "duration": 190}
            with patch.object(downloader, "_run", side_effect=run) as process:
                downloaded = downloader.fetch(track, threading.Event())
                self.assertEqual(process.call_count, 3)
            audio = sf.info(downloaded["path"])
            self.assertEqual((audio.samplerate, audio.channels), (48000, 2))
            with patch.object(downloader, "_run", side_effect=AssertionError("cache must not use network")):
                self.assertEqual(downloader.fetch(track), downloaded)
            self.assertFalse(list(downloader.root.glob(".download-*")))

    def test_cancellation_terminates_download_subprocess(self):
        with tempfile.TemporaryDirectory() as temporary:
            downloader = MusicDownloader(temporary)
            cancel = threading.Event()
            timer = threading.Timer(0.2, cancel.set)
            start = time.monotonic()
            timer.start()
            try:
                with self.assertRaises(CancelledAnalysis):
                    downloader._run([sys.executable, "-c", "import time; time.sleep(15)"], temporary, cancel)
                self.assertLess(time.monotonic() - start, 3)
            finally:
                timer.join()


class ImportedPlaylistIntegrationTests(unittest.TestCase):
    setUp = shared.MusicIntegrationTests.setUp
    tearDown = shared.MusicIntegrationTests.tearDown

    def test_import_creates_tabs_and_click_downloads_analyses_plays_then_uses_cache(self):
        first, second = self.root / "main.txt", self.root / "russian.txt"
        first.write_text(row("1_2", "First", "Artist") + row("1_3", "Next", "Artist"), encoding="utf-8")
        second.write_text(row("1_4", "Почерк", "Ваня"), encoding="utf-8")
        audio = self.root / "downloaded.wav"
        shared.write_beats(audio, duration=3)
        with patch.object(self.panel.jobs.downloader, "fetch", return_value={"path": str(audio), "resolved_url": "https://example.test/audio"}) as fetch:
            self.panel.jobs.import_playlists([self.root])
            self.assertTrue(shared.wait_until(lambda: self.panel.playlist_tabs.count() == 2, 5))
            self.assertEqual(fetch.call_count, 0)
            self.assertTrue(all(track["status"] == "NotDownloaded" for track in self.panel.library.tracks()))
            self.panel.playlist_tabs.setCurrentIndex(0)
            self.panel.playlist_tracks.selectRow(0)
            self.panel.play_playlist()
            self.assertTrue(shared.wait_until(lambda: self.panel.player.state == "playing", 15), self.panel.status.text())
            self.assertEqual(fetch.call_count, 1)
            track = self.panel.library.track(self.panel.player.current_track)
            self.assertEqual(track["title"], "First")
            self.assertEqual(track["path"], str(audio))
            self.panel.player.stop()
            self.panel.play_playlist()
            self.assertTrue(shared.wait_until(lambda: self.panel.player.state == "playing", 5))
            self.assertEqual(fetch.call_count, 1)
            self.backend.mediaStatusChanged.emit(shared.QtMultimedia.QMediaPlayer.EndOfMedia)
            self.assertTrue(shared.wait_until(lambda: self.panel.player.state == "playing" and self.panel.library.track(self.panel.player.current_track)["title"] == "Next", 10))
            self.assertEqual(fetch.call_count, 2)
        self.assertEqual(len(list(self.panel.jobs.playlist_cache.glob("*.json"))), 2)

    def test_mix_is_limited_to_selected_playlist_and_local_additions_are_supported(self):
        first = self.panel.library.create_playlist("First")
        second = self.panel.library.create_playlist("Second")
        for title in ("One", "Two", "Three"):
            self.panel.library.add_search_track(first["id"], title, "Artist")
        other = self.panel.library.add_search_track(second["id"], "Other", "Artist")
        self.panel.refresh_playlists()
        self.panel.playlist_tabs.setCurrentIndex(0)
        with patch.object(self.panel.jobs, "analyse"), patch("ui.MusicPanel.random.randrange", return_value=2):
            self.panel.play_playlist(mix=True)
            tracks = self.panel.player.queue.ordered_tracks()
            self.assertEqual(len(tracks), len(set(tracks)))
            self.assertEqual(len(tracks), 3)
            self.assertEqual(self.panel.library.track(tracks[0])["title"], "Three")
            self.assertNotIn(other["id"], tracks)
            self.assertTrue(self.panel.player.queue.shuffle)
        self.panel.player.stop()
        audio = self.root / "local.wav"
        shared.write_beats(audio, duration=2)
        self.panel.import_paths([str(audio)], first["id"])
        self.assertTrue(shared.wait_until(lambda: len(self.panel.library.playlists()[0]["tracks"]) == 4, 3))
        self.assertEqual(len(self.panel.library.playlists()[1]["tracks"]), 1)


if __name__ == "__main__":
    unittest.main()
