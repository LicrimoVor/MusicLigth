from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import requests
from PyQt5 import QtCore, QtTest, QtWidgets

import test_music as shared
from music.downloads import MusicDownloader
from music.providers import CatalogProviders, request_bytes, safe_choice
from music.storage import CancelledAnalysis
from test_music_downloads import candidate, metadata


class CatalogTests(unittest.TestCase):
    def test_yandex_search_keeps_album_url_artist_version_and_duration(self):
        result = {"result": {"tracks": {"results": [{"id": 42, "title": "Song", "version": "Live",
            "artists": [{"name": "Artist"}], "albums": [{"id": 7}], "durationMs": 190000}]}}}
        with patch("music.providers.request_bytes", return_value=json.dumps(result).encode()) as request:
            found = CatalogProviders().search("yandex", "Artist Song", None)
        self.assertEqual(found[0]["webpage_url"], "https://music.yandex.ru/album/7/track/42")
        self.assertEqual(found[0]["title"], "Song (Live)")
        self.assertEqual(found[0]["duration"], 190)
        self.assertEqual(request.call_args.kwargs["params"]["text"], "Artist Song")

    def test_yandex_full_audio_filters_previews_and_builds_authorized_link(self):
        options = {"result": [{"codec": "mp3", "bitrateInKbps": 320, "preview": True, "downloadInfoUrl": "https://example.test/preview"},
            {"codec": "mp3", "bitrateInKbps": 192, "preview": False, "downloadInfoUrl": "https://example.test/info"}]}
        with patch("music.providers.request_bytes", side_effect=[json.dumps(options).encode(),
                b'<download-info><host>cdn.example.test</host><path>/audio.mp3</path><ts>12</ts><s>salt</s></download-info>']) as request:
            found = CatalogProviders({"yandex": "test-token"}).inspect({"provider_key": "yandex", "source_id": "42"}, None)
        self.assertEqual(found["formats"][0]["abr"], 192)
        self.assertTrue(found["formats"][0]["url"].startswith("https://cdn.example.test/get-mp3/"))
        self.assertEqual(request.call_args_list[0].kwargs["headers"], {"Authorization": "OAuth test-token"})
        with patch("music.providers.request_bytes", return_value=b'{"result": []}'):
            with self.assertRaisesRegex(ValueError, "полную запись"):
                CatalogProviders().inspect({"provider_key": "yandex", "source_id": "42"}, None)

    def test_vk_requires_audio_access_posts_credentials_and_excludes_them_from_candidates(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "токен"):
                CatalogProviders().search("vk", "Song", None)
        response = {"response": {"items": [{"id": 2, "owner_id": 1, "title": "Song", "artist": "Artist", "duration": 190}]}}
        with patch("music.providers.request_bytes", return_value=json.dumps(response).encode()) as request:
            found = CatalogProviders({"vk": "test-token"}).search("vk", "Song", None)
        self.assertEqual(found[0]["webpage_url"], "https://vk.com/audio1_2")
        self.assertEqual(request.call_args.kwargs["data"]["access_token"], "test-token")
        self.assertNotIn("test-token", json.dumps(found))
        self.assertNotIn("url", safe_choice(found[0] | {"url": "https://signed.example.test/audio", "token": "test-token"}))
        with patch("music.providers.request_bytes", return_value=b'{"error":{"error_code":201}}'):
            with self.assertRaisesRegex(ValueError, "201"):
                CatalogProviders({"vk": "test-token"}).search("vk", "Song", None)

    def test_http_failure_does_not_expose_request_url_or_token(self):
        with patch("music.providers.requests.request", side_effect=requests.HTTPError("https://example.test/?access_token=test-secret")):
            with self.assertRaises(RuntimeError) as error:
                request_bytes("https://example.test/", None)
        self.assertNotIn("test-secret", str(error.exception))


class ManualDownloadTests(unittest.TestCase):
    def test_all_providers_are_queried_with_bounded_parallelism_and_manual_versions_are_kept(self):
        with tempfile.TemporaryDirectory() as root:
            downloader = MusicDownloader(root)
            lock, counts = threading.Lock(), [0, 0]
            def search(provider, prefix, query, cancel):
                with lock:
                    counts[0] += 1
                    counts[1] = max(counts)
                threading.Event().wait(0.01)
                with lock:
                    counts[0] -= 1
                if provider == "VK Музыка":
                    raise KeyError("Malformed provider response")
                return [candidate(f"https://example.test/{prefix}", provider, 190, "Artist - Song Live")]
            with patch.object(downloader, "_search_source", side_effect=search) as lookup:
                entries, errors = downloader.browse({"title": "Song", "artist": "Artist", "duration": 190})
            self.assertEqual(lookup.call_count, 4)
            self.assertLessEqual(counts[1], 2)
            self.assertEqual(len(entries), 3)
            self.assertTrue(all(entry["match_score"] is None for entry in entries))
            self.assertIn("VK Музыка", errors[0])

    def test_chosen_preview_is_reused_for_replacement_and_never_falls_back_to_another_result(self):
        with tempfile.TemporaryDirectory() as root:
            downloader = MusicDownloader(root)
            choice = candidate("https://soundcloud.com/artist/song", "SoundCloud", 3)
            info = metadata(choice["webpage_url"], duration=3)
            info.update(provider="SoundCloud", selected_audio={"format_id": "good", "acodec": "opus", "abr": 160})
            @contextmanager
            def download(*_args):
                path = Path(root) / "prepared.wav"
                shared.write_beats(path, duration=3, samplerate=48000)
                samples, rate = shared.sf.read(path)
                shared.sf.write(path, shared.np.column_stack((samples, samples)), rate)
                yield path, 3
            track = {"title": "Song", "artist": "Artist", "expected_duration": 190}
            with patch.object(downloader, "_inspect", return_value=info), patch.object(downloader, "_download", download), \
                    patch.object(downloader, "_candidates", side_effect=AssertionError("manual choice must be exact")):
                preview = downloader.fetch(track, candidate=choice)
            with patch.object(downloader, "_inspect", side_effect=AssertionError("preview cache must be reused")):
                selected = downloader.fetch(track | {"recording_choice": safe_choice(choice), "download_revision": 1})
            self.assertEqual(preview, selected)
            self.assertTrue(Path(selected["path"]).exists())
            self.assertEqual(selected["recording_choice"]["webpage_url"], choice["webpage_url"])


class RecordingDialogTests(unittest.TestCase):
    def setUp(self):
        shared.MusicIntegrationTests.setUp(self)
        playlist = self.panel.library.create_playlist("Recordings")
        track = self.panel.library.add_search_track(playlist["id"], "Song", "Artist")
        self.old = self.root / "old.wav"
        shared.write_beats(self.old, duration=3)
        self.track = self.panel.library.update_track(track["id"], status="Ready", path=str(self.old),
            resolved_url="https://www.youtube.com/watch?v=old", duration=3, play_count=7)
        self.panel.selected_playlist = playlist["id"]
        self.panel.refresh_playlists()
        self.panel.playlist_tracks.selectRow(0)
        self.entries = [candidate("https://soundcloud.com/artist/song", "SoundCloud", 3),
                        candidate("https://example.test/other", "Яндекс Музыка", 3)]
        self.preview_backend = shared.FakeMediaBackend()
        for mock in (patch("ui.RecordingDialog.PCMPlayerBackend", return_value=self.preview_backend),
                     patch.object(MusicDownloader, "browse", return_value=(self.entries, ["VK Музыка: нужен доступ"]))):
            mock.start()
            self.addCleanup(mock.stop)

    def tearDown(self):
        shared.MusicIntegrationTests.tearDown(self)

    def open(self):
        self.panel.replace_playlist_recording()
        dialog = self.panel.recording_dialog
        self.assertTrue(shared.wait_until(lambda: dialog.table.rowCount() == 2, 3))
        return dialog

    def test_open_and_cancel_only_search_preserve_recording_and_statistics(self):
        dialog = self.open()
        self.assertTrue(dialog.isVisible())
        self.assertIn("VK", dialog.provider_status.toPlainText())
        self.assertEqual(self.panel.library.track(self.track["id"]), self.track)
        dialog.reject()
        self.assertTrue(shared.wait_until(lambda: not self.panel.recording_dialogs, 3))
        self.assertEqual(self.panel.library.track(self.track["id"]), self.track)

    def test_preview_pauses_main_resumes_at_same_position_and_keeps_preview_controls_separate(self):
        self.panel.player.trackRequested.disconnect(self.panel.prepare_track)
        self.panel.player.set_queue([self.track["id"]])
        self.panel.player.prepared = self.track["id"]
        self.panel.player.want_play = True
        self.backend.play()
        self.backend.setPosition(1200)
        dialog = self.open()
        result = {"path": str(self.old), "source_duration": 3, "resolved_source": "SoundCloud", "resolved_title": "Song"}
        with patch.object(dialog.worker.downloader, "fetch", return_value=result) as fetch:
            dialog.preview()
            self.assertTrue(shared.wait_until(lambda: dialog.previewing, 3))
            self.assertEqual(self.panel.player.state, "paused")
            self.assertEqual(self.backend.position(), 1200)
            self.assertEqual(fetch.call_args.kwargs["candidate"]["webpage_url"], self.entries[0]["webpage_url"])
            self.preview_backend.setPosition(800)
            dialog.preview()
            self.assertFalse(dialog.previewing)
            dialog.preview()
            self.assertTrue(dialog.previewing)
            self.assertEqual(self.preview_backend.position(), 800)
            dialog.volume.setValue(20)
            self.assertEqual(self.preview_backend.volume, 20)
            self.assertEqual(self.panel.volume_slider.value(), 75)
            dialog.reject()
            self.assertEqual(self.panel.player.state, "playing")
            self.assertEqual(self.backend.position(), 1200)

    def test_choose_sends_only_explicit_selection_to_analysis_worker(self):
        dialog = self.open()
        dialog.table.selectRow(1)
        with patch.object(self.panel.jobs, "replace_recording") as replace:
            dialog.choose_button.click()
        self.assertEqual(replace.call_count, 1)
        self.assertEqual(replace.call_args.args[0]["id"], self.track["id"])
        self.assertEqual(replace.call_args.args[2]["webpage_url"], self.entries[1]["webpage_url"])
        self.assertEqual(self.panel.library.track(self.track["id"]), self.track)

    def test_source_credentials_are_shared_in_memory_and_never_saved_to_library(self):
        dialog = self.open()
        dialog.show_access()
        fields = dialog.access_dialog.findChildren(QtWidgets.QLineEdit)
        fields[0].setText("test-yandex-secret")
        fields[1].setText("test-vk-secret")
        dialog.access_dialog.findChild(QtWidgets.QDialogButtonBox).button(QtWidgets.QDialogButtonBox.Apply).click()
        self.assertIs(dialog.worker.downloader.catalogs.credentials, self.panel.jobs.downloader.catalogs.credentials)
        self.assertEqual(self.panel.jobs.downloader.catalogs.credentials["vk"], "test-vk-secret")
        saved = self.panel.library.path.read_text(encoding="utf-8")
        self.assertNotIn("test-vk-secret", saved)
        self.assertNotIn("test-yandex-secret", saved)

    def test_close_cancels_preparation_without_blocking_qt(self):
        dialog = self.open()
        started = threading.Event()
        def blocked(track, cancel, progress, **kwargs):
            started.set()
            cancel.wait(3)
            raise CancelledAnalysis()
        with patch.object(dialog.worker.downloader, "fetch", side_effect=blocked):
            dialog.preview()
            self.assertTrue(shared.wait_until(started.is_set, 2))
            ticks = []
            timer = QtCore.QTimer()
            timer.setInterval(5)
            timer.timeout.connect(lambda: ticks.append(1))
            timer.start()
            QtTest.QTest.qWait(50)
            self.assertGreater(len(ticks), 3)
            dialog.reject()
            self.assertTrue(shared.wait_until(lambda: not self.panel.recording_dialogs, 3))
            timer.stop()


if __name__ == "__main__":
    unittest.main()
