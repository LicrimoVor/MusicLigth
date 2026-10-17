import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import numpy as np
import soundfile as sf

import test_music as shared
from music.downloads import MusicDownloader
from music.matching import audio_format, choose_result, rank_results, recording_url


def candidate(url, provider="YouTube", duration=190, title="Artist - Song"):
    return {"title": title, "artist": "Artist", "duration": duration, "webpage_url": url, "provider": provider}


def metadata(url, codec="opus", bitrate=160, duration=2):
    return candidate(url, duration=duration) | {"formats": [{"format_id": "good", "vcodec": "none",
        "acodec": codec, "abr": bitrate, "asr": 48000, "audio_channels": 2}]}


class MatchingTests(unittest.TestCase):
    def test_wrong_versions_and_duration_are_rejected(self):
        track = {"title": "Song", "artist": "Artist", "expected_duration": 190}
        wrong = [candidate("https://example.test/" + str(index), title="Artist - Song " + suffix)
                 for index, suffix in enumerate(("Кавер", "Slowed reverb", "Bass Boosted", "Live", "Remix", "Acoustic", "Demo", "Bootleg"))]
        wrong.append(candidate("https://example.test/short", duration=150))
        wrong.append(candidate("https://example.test/other", title="Artist - Some Other Song"))
        wrong.append(candidate("https://example.test/hidden-remix", title="Artist - Song Remix") | {"track": "Song"})
        self.assertEqual(rank_results(wrong, track), [])
        remix = candidate("https://example.test/remix", title="Artist - Song Remix")
        self.assertEqual(choose_result([remix], track | {"title": "Song (Remix)"})["webpage_url"], remix["webpage_url"])

    def test_official_audio_artist_and_exact_duration_are_preferred(self):
        track = {"title": "Song", "artist": "Artist", "expected_duration": 190}
        normal = candidate("https://example.test/normal", duration=200)
        official = candidate("https://example.test/official", title="Artist - Song (Official Audio)")
        official["channel_is_verified"] = True
        self.assertEqual(choose_result([normal, official], track)["webpage_url"], official["webpage_url"])
        audio = candidate("https://example.test/audio", title="LSD - Audio (Official Audio)") | {"artist": "LSD"}
        self.assertIsNotNone(choose_result([audio], {"title": "Audio", "artist": "LSD", "duration": 190}))
        with self.assertRaises(ValueError):
            choose_result([candidate("https://example.test/games", title="Artist - Games")],
                          {"title": "Video Games", "artist": "Artist", "duration": 190})

    def test_formats_reject_previews_low_rates_and_prefer_lossless(self):
        info = metadata("https://example.test/audio")
        formats = info["formats"]
        formats.extend([formats[0] | {"format_id": "preview", "abr": 320},
                        formats[0] | {"format_id": "bad", "acodec": "mp3", "abr": 64},
                        formats[0] | {"format_id": "telephone", "abr": 320, "asr": 16000},
                        formats[0] | {"format_id": "original", "acodec": "flac", "abr": None}])
        self.assertEqual(audio_format(info)["format_id"], "original")
        with self.assertRaises(ValueError):
            audio_format({"formats": formats[1:4]})

    def test_bad_urls_are_excluded_across_youtube_url_forms(self):
        track = {"title": "Song", "artist": "Artist", "duration": 190,
                 "excluded_urls": ["https://youtu.be/bad"]}
        bad = candidate("https://www.youtube.com/watch?v=bad&feature=share")
        good = candidate("http://soundcloud.com/artist/song", "SoundCloud")
        ranked = rank_results([bad, good], track)
        self.assertEqual(len(ranked), 1)
        self.assertEqual(ranked[0]["webpage_url"], "https://soundcloud.com/artist/song")


class DownloadPipelineTests(unittest.TestCase):
    def test_search_survives_one_provider_failure(self):
        with tempfile.TemporaryDirectory() as root:
            downloader = MusicDownloader(root)
            def search(provider, prefix, query, cancel):
                if provider == "SoundCloud":
                    raise RuntimeError("Unavailable")
                return [candidate("https://www.youtube.com/watch?v=good")]
            with patch.object(downloader, "_search_source", side_effect=search) as lookup:
                found = downloader.search({"title": "Song", "artist": "Artist", "duration": 190})
            self.assertEqual(lookup.call_count, 2)
            self.assertEqual(len(found), 1)

    def test_silent_youtube_file_falls_back_to_soundcloud_and_then_local_cache(self):
        with tempfile.TemporaryDirectory() as root:
            downloader = MusicDownloader(root)
            original_run = downloader._run
            youtube, soundcloud = "https://www.youtube.com/watch?v=one", "https://soundcloud.com/artist/song"
            downloads = []
            def run(args, directory, cancel, timeout=180):
                if any(value.startswith("ytsearch8:") for value in args):
                    return json.dumps({"entries": [candidate(youtube, duration=2)]})
                if any(value.startswith("scsearch8:") for value in args):
                    return json.dumps({"entries": [candidate(soundcloud, "SoundCloud", duration=2)]})
                if "--dump-single-json" in args:
                    return json.dumps(metadata(args[-1], bitrate=160 if args[-1] == youtube else 128))
                if "yt_dlp" in args:
                    downloads.append(args[-1])
                    source = Path(directory) / "source.wav"
                    if args[-1] == youtube:
                        sf.write(source, np.zeros((96000, 2)), 48000)
                    else:
                        shared.write_beats(source, duration=2, samplerate=48000)
                    return ""
                return original_run(args, directory, cancel, timeout)
            track = {"title": "Song", "artist": "Artist", "duration": 2}
            with patch.object(downloader, "_run", side_effect=run):
                result = downloader.fetch(track, threading.Event())
            self.assertEqual(downloads, [youtube, soundcloud])
            self.assertEqual(result["resolved_source"], "SoundCloud")
            self.assertEqual(result["source_bitrate_kbps"], 128)
            self.assertEqual(sf.info(result["path"]).samplerate, 48000)
            with patch.object(downloader, "_run", side_effect=AssertionError("cache must not use network")):
                self.assertEqual(downloader.fetch(track), result)
            self.assertFalse(list(downloader.root.glob(".download-*")))

    def test_downloaded_preview_is_rejected_by_actual_duration(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "preview.wav"
            shared.write_beats(path, duration=2, samplerate=48000)
            mono, samplerate = sf.read(path)
            sf.write(path, np.column_stack((mono, mono)), samplerate)
            with self.assertRaisesRegex(ValueError, "превью"):
                MusicDownloader._validate_audio(path, {"title": "Song", "duration": 20}, {"duration": 20}, None)


class ReplacementIntegrationTests(unittest.TestCase):
    setUp = shared.MusicIntegrationTests.setUp
    tearDown = shared.MusicIntegrationTests.tearDown

    def make_remote(self):
        playlist = self.panel.library.create_playlist("Remote")
        track = self.panel.library.add_search_track(playlist["id"], "Song", "Artist")
        old = self.root / "old.wav"
        shared.write_beats(old, duration=3)
        track = self.panel.library.update_track(track["id"], status="Ready", path=str(old), play_count=7,
            resolved_url="https://www.youtube.com/watch?v=old", download_url="https://youtu.be/old")
        self.panel.selected_playlist = playlist["id"]
        self.panel.refresh_playlists()
        self.panel.playlist_tracks.selectRow(0)
        return track

    def test_failed_replacement_preserves_existing_recording_and_statistics(self):
        track = self.make_remote()
        with patch.object(self.panel.jobs.downloader, "fetch", side_effect=RuntimeError("No alternatives")):
            self.panel.replace_playlist_recording()
            self.assertTrue(shared.wait_until(lambda: not self.panel.replacing, 4))
        self.assertEqual(self.panel.library.track(track["id"]), track)
        self.assertTrue(Path(track["path"]).is_file())
        self.assertIn("No alternatives", self.panel.status.text())

    def test_replacement_is_reanalysed_and_remembers_rejected_url(self):
        track = self.make_remote()
        replacement = self.root / "better.wav"
        shared.write_beats(replacement, duration=3, amplitude=0.7)
        downloaded = {"path": str(replacement), "resolved_url": "https://soundcloud.com/artist/song",
            "resolved_source": "SoundCloud", "source_codec": "mp3", "source_bitrate_kbps": 320}
        with patch.object(self.panel.jobs.downloader, "fetch", return_value=downloaded) as fetch:
            self.panel.replace_playlist_recording()
            self.assertTrue(shared.wait_until(lambda: not self.panel.replacing, 15), self.panel.status.text())
            requested = fetch.call_args[0][0]
            self.assertTrue(requested["search_by_name"])
            self.assertIn(track["resolved_url"], requested["excluded_urls"])
        result = self.panel.library.track(track["id"])
        self.assertEqual(result["status"], "Ready")
        self.assertEqual(result["play_count"], 7)
        self.assertEqual(result["path"], str(replacement))
        self.assertEqual(result["download_revision"], 1)
        self.assertEqual(result["excluded_urls"], [track["resolved_url"]])
        self.assertTrue(Path(result["cache_path"]).is_dir())
        self.assertTrue(Path(result["playback_path"]).is_file())
        self.assertTrue(Path(track["path"]).is_file())

    def test_current_song_restarts_from_new_audio_after_successful_replacement(self):
        track = self.make_remote()
        with patch.object(self.panel.jobs.downloader, "fetch", return_value={"path": track["path"], "resolved_url": track["resolved_url"]}):
            self.panel.enqueue_analysis(track)
            self.assertTrue(shared.wait_until(lambda: bool(self.panel.library.track(track["id"]).get("cache_path")), 12))
        self.panel.player.set_queue([track["id"]])
        self.panel.play()
        self.assertTrue(shared.wait_until(lambda: self.panel.player.state == "playing", 5))
        self.backend.setPosition(1500)
        old_media = self.backend.media.canonicalUrl().toLocalFile()
        new = self.root / "new.wav"
        shared.write_beats(new, duration=3, amplitude=0.7)
        with patch.object(self.panel.jobs.downloader, "fetch", return_value={"path": str(new),
                "resolved_url": "https://soundcloud.com/artist/new", "resolved_source": "SoundCloud"}):
            self.panel.replace_playlist_recording()
            self.assertTrue(shared.wait_until(lambda: not self.panel.replacing and self.panel.player.state == "playing" and
                self.backend.media.canonicalUrl().toLocalFile() != old_media, 12), self.panel.status.text())
        self.assertEqual(self.backend.clock, 0)
        self.assertEqual(self.panel.player.current_track, track["id"])
