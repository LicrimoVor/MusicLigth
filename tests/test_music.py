import asyncio
import json
import os
from pathlib import Path
import random
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

import numpy as np
import soundfile as sf
from PyQt5 import QtCore, QtMultimedia, QtTest, QtWidgets
from mutagen.id3 import TALB, TIT2, TPE1
from mutagen.wave import WAVE

from model.settings import normalized_settings
from music.analysis import ANALYSIS_VERSION, AnalysisData, MusicAnalyser
from music.player import MusicPlayer, PlaybackQueue
from music.audio_backend import PCMPlayerBackend
from music.scheduler import LightShowScheduler
from music.storage import CancelledAnalysis, MusicLibrary, atomic_json
from music.timeline import EVENT_TYPES, LightTimeline, TimelineGenerator, show_settings, timeline_key
from ui.AudioVisualizer import AudioVisualizer


QT_APP = None


def application():
    global QT_APP
    QT_APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    QT_APP.setQuitOnLastWindowClosed(False)
    return QT_APP


def wait_until(predicate, seconds=10):
    end = time.monotonic() + seconds
    while not predicate() and time.monotonic() < end:
        QtTest.QTest.qWait(20)
    return bool(predicate())


def write_beats(path, amplitude=0.5, duration=10, samplerate=22050):
    times = np.arange(round(samplerate * duration)) / samplerate
    data = np.zeros(len(times))
    for onset in np.arange(0.5, duration - 0.2, 0.5):
        elapsed = times - onset
        selected = (elapsed >= 0) & (elapsed < 0.14)
        data[selected] += amplitude * np.sin(2 * np.pi * 70 * elapsed[selected]) * np.exp(-30 * elapsed[selected])
    sf.write(path, data, samplerate)


class FakeMediaBackend(QtCore.QObject):
    positionChanged = QtCore.pyqtSignal(int)
    durationChanged = QtCore.pyqtSignal(int)
    stateChanged = QtCore.pyqtSignal(int)
    mediaStatusChanged = QtCore.pyqtSignal(int)
    error = QtCore.pyqtSignal(int)

    def __init__(self):
        super().__init__()
        self.clock = 0
        self.volume = 75
        self.media = None
        self.play_calls = 0

    def setNotifyInterval(self, value):
        self.interval = value

    def setVolume(self, value):
        self.volume = value

    def position(self):
        return self.clock

    def setPosition(self, value):
        self.clock = value
        self.positionChanged.emit(value)

    def setMedia(self, media):
        self.media = media
        self.clock = 0

    def play(self):
        self.play_calls += 1
        self.stateChanged.emit(QtMultimedia.QMediaPlayer.PlayingState)

    def pause(self):
        self.stateChanged.emit(QtMultimedia.QMediaPlayer.PausedState)

    def stop(self):
        self.stateChanged.emit(QtMultimedia.QMediaPlayer.StoppedState)

    def errorString(self):
        return "test playback error"


class FakeLampController:
    def __init__(self):
        from types import SimpleNamespace
        self.devices = [SimpleNamespace(id=lamp) for lamp in ("a", "b")]
        self.calls = []
        self.generation = 0
        self.rate = 20

    def set_update_rate(self, value):
        self.rate = value

    def invalidate_pending(self):
        self.generation += 1
        return self.generation

    async def set_colors(self, colors, brightness, **options):
        if options.get("generation", self.generation) == self.generation:
            self.calls.append((dict(colors), brightness, options, time.monotonic()))
        return {}

    async def diagnose_devices(self):
        return [{"id": "a", "ok": True, "latency_ms": 40, "error": ""},
                {"id": "b", "ok": False, "latency_ms": 0, "error": "unavailable"}]


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.wav = self.root / "track.wav"
        self.flac = self.root / "track.flac"
        write_beats(self.wav, duration=2)
        write_beats(self.flac, duration=2)

    def tearDown(self):
        self.temporary.cleanup()

    def test_import_playlist_reorder_and_library_survive_restart(self):
        library = MusicLibrary(self.root / "music")
        tracks = library.add_files([self.wav, self.flac, self.wav, self.root])
        self.assertEqual(len(tracks), 2)
        playlist = library.create_playlist("A playlist")
        ordered = [tracks[1]["id"], tracks[0]["id"], tracks[1]["id"]]
        library.set_playlist_tracks(playlist["id"], ordered)
        library.update_settings({"volume": 25})
        restarted = MusicLibrary(library.root)
        self.assertEqual(len(restarted.tracks()), 2)
        self.assertEqual(restarted.playlists()[0]["tracks"], ordered)
        self.assertEqual(restarted.settings()["volume"], 25)
        restarted.remove_tracks([tracks[1]["id"]])
        self.assertTrue(self.flac.exists())
        self.assertEqual(restarted.playlists()[0]["tracks"], [tracks[0]["id"]])

    def test_corrupt_manifest_is_backed_up(self):
        root = self.root / "music"
        root.mkdir()
        (root / "library.json").write_text("{bad json", encoding="utf-8")
        library = MusicLibrary(root)
        self.assertTrue(library.load_error)
        self.assertEqual(library.tracks(), [])
        self.assertEqual(len(list(root.glob("library.backup-*.json"))), 1)


class OfflineAnalysisTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name)
        cls.source = cls.root / "beats.wav"
        write_beats(cls.source)
        tagged = WAVE(cls.source)
        tagged.add_tags()
        tagged.tags.add(TIT2(encoding=3, text="Beat fixture"))
        tagged.tags.add(TPE1(encoding=3, text="Fixture artist"))
        tagged.tags.add(TALB(encoding=3, text="Fixture album"))
        tagged.save()
        cls.analyser = MusicAnalyser(cls.root / "cache")
        cls.result = cls.analyser.analyse(cls.source)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def test_beats_are_tracked_separately_from_onsets_and_metadata_is_read(self):
        data = AnalysisData(self.result["cache_path"])
        try:
            self.assertAlmostEqual(data.metadata["bpm"], 120, delta=2)
            self.assertGreater(data.metadata["tempo_confidence"], 0.6)
            expected = np.arange(0.5, 9.8, 0.5)
            np.testing.assert_allclose(data.beats, expected, atol=0.035)
            self.assertGreater(len(data.onsets), len(data.beats))
            self.assertEqual(data.metadata["title"], "Beat fixture")
            self.assertEqual(data.metadata["artist"], "Fixture artist")
            self.assertEqual(data.metadata["album"], "Fixture album")
            self.assertTrue(np.all(np.isfinite(data.features)))
            self.assertTrue(Path(data.metadata["playback_path"]).is_file())
        finally:
            data.close()

    def test_content_cache_skips_decoding_and_fft(self):
        with patch("music.analysis.sf.SoundFile", side_effect=AssertionError("Unexpected decoding")):
            result = self.analyser.analyse(self.source)
        self.assertTrue(result["cache_hit"])
        self.assertEqual(result["content_id"], self.result["content_id"])

    def test_content_and_algorithm_changes_invalidate_cache(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "changed.wav"
            write_beats(source, duration=3)
            analyser = MusicAnalyser(root / "cache")
            first = analyser.analyse(source)
            write_beats(source, amplitude=0.1, duration=3)
            second = analyser.analyse(source)
            self.assertNotEqual(first["content_id"], second["content_id"])
            with patch("music.analysis.ANALYSIS_VERSION", ANALYSIS_VERSION + 1):
                third = analyser.analyse(source)
            self.assertFalse(third["cache_hit"])
            self.assertNotEqual(second["cache_path"], third["cache_path"])

    def test_silence_and_stationary_bass_do_not_invent_beats(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            analyser = MusicAnalyser(root / "cache")
            for frequency in (0, 70):
                times = np.arange(22050 * 4) / 22050
                source = root / f"tone{frequency}.wav"
                sf.write(source, 0.2 * np.sin(2 * np.pi * frequency * times), 22050)
                result = analyser.analyse(source)
                self.assertEqual(result["bpm"], 0)
                data = AnalysisData(result["cache_path"])
                self.assertEqual(len(data.beats), 0)
                data.close()

    def test_cancellation_cleans_partial_cache(self):
        cancelled = threading.Event()
        cancelled.set()
        with self.assertRaises(CancelledAnalysis):
            self.analyser.analyse(self.source, cancelled)
        self.assertEqual(list(self.analyser.cache_root.glob(".work-*")), [])

    def test_mp3_and_flac_decode_to_native_pcm_and_alias_titles_stay_distinct(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            analyser = MusicAnalyser(root / "cache")
            for extension, format in (("mp3", "MP3"), ("flac", "FLAC")):
                source = root / f"first.{extension}"
                sf.write(source, np.zeros(44100), 44100, format=format)
                first = analyser.analyse(source)
                alias = root / f"second.{extension}"
                shutil.copyfile(source, alias)
                second = analyser.analyse(alias)
                self.assertTrue(second["cache_hit"])
                self.assertEqual(second["title"], "second")
                pcm = sf.info(first["playback_path"])
                self.assertEqual((pcm.samplerate, pcm.channels, pcm.subtype), (48000, 2, "PCM_16"))

    def test_timeline_supports_build_drop_and_no_selected_lamps(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            analysis_path = root / "analysis"
            shutil.copytree(self.result["cache_path"], analysis_path)
            metadata = json.loads((analysis_path / "analysis.json").read_text(encoding="utf-8"))
            metadata["transitions"] = [{"time": 2.0, "type": "build_up", "duration": 2, "strength": 0.5},
                                        {"time": 4.0, "type": "drop", "duration": 0.5, "strength": 0.5}]
            atomic_json(analysis_path / "analysis.json", metadata)
            generator = TimelineGenerator(root / "cache")
            settings = show_settings({}, ["a"])
            timeline = LightTimeline(generator.generate(analysis_path, settings))
            self.assertEqual({timeline.event(index)["type"] for index in range(len(timeline.events))}, set(EVENT_TYPES))
            timeline.close()
            settings["lamp_ids"] = []
            timeline = LightTimeline(generator.generate(analysis_path, settings))
            self.assertEqual(timeline.state_at(2, "a"), (0, 0, 0))
            timeline.close()

    def test_compiled_accents_peak_on_beats_without_smoothing_delay(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            analysis_path = root / "analysis"
            shutil.copytree(self.result["cache_path"], analysis_path)
            features = np.load(analysis_path / "features.npy", mmap_mode="r+")
            features[:, 7] = 0.5
            features.flush()
            features._mmap.close()
            settings = show_settings({"preset": "Rhythm"}, ["a"])
            timeline = LightTimeline(TimelineGenerator(root / "cache").generate(analysis_path, settings))
            analysis = AnalysisData(analysis_path)
            try:
                errors = []
                for beat in analysis.beats[2:-2]:
                    left = max(0, int((beat - 0.18) * timeline.metadata["hz"]))
                    right = min(len(timeline.frames), int((beat + 0.18) * timeline.metadata["hz"]) + 1)
                    peak = left + int(np.argmax(timeline.frames[left:right, 0, 3]))
                    errors.append(abs(peak / timeline.metadata["hz"] - beat))
                self.assertTrue(errors)
                self.assertLessEqual(max(errors), 0.05)
            finally:
                analysis.close()
                timeline.close()

    def test_timeline_presets_are_cached_separately_and_have_complete_event_schema(self):
        with tempfile.TemporaryDirectory() as temporary:
            generator = TimelineGenerator(temporary)
            settings = show_settings({}, ["a", "b"])
            feature_file = Path(self.result["cache_path"]) / "features.npy"
            before = feature_file.stat().st_mtime_ns
            path = generator.generate(self.result["cache_path"], settings)
            timeline = LightTimeline(path)
            try:
                self.assertEqual(timeline.lamp_ids, ["a", "b"])
                self.assertTrue(np.all(np.isfinite(timeline.frames)))
                self.assertLessEqual(float(timeline.frames[:, :, 3].max()), settings["max_brightness"] + 1e-6)
                self.assertTrue(np.all(np.diff(timeline.events["timestamp"]) >= 0))
                kinds = {timeline.event(index)["type"] for index in range(len(timeline.events))}
                self.assertTrue(set(EVENT_TYPES[:5]).issubset(kinds))
                event = timeline.event(0)
                self.assertEqual(set(event), {"timestamp", "type", "color", "brightness", "target", "duration", "transition"})
                self.assertEqual(timeline.state_at(2, "unknown"), (0, 0, 0))
                self.assertTrue(any(timeline.state_at(2, "a")))
            finally:
                timeline.close()
            changed = settings | {"preset": "Rhythm"}
            other = generator.generate(self.result["cache_path"], changed)
            self.assertNotEqual(path, other)
            self.assertEqual(before, feature_file.stat().st_mtime_ns)
            self.assertEqual(generator.generate(self.result["cache_path"], settings), path)
            self.assertEqual(timeline_key(self.result["content_id"], settings),
                             timeline_key(self.result["content_id"], settings | {"latency_ms": 200}))


class PlayerAndSchedulerTests(unittest.TestCase):
    def setUp(self):
        application()
        self.backend = FakeMediaBackend()
        self.tracks = {track: {"id": track, "status": "Ready", "duration": 10, "playback_path": "C:/test.wav"}
                       for track in ("a", "b", "c")}
        self.player = MusicPlayer(self.tracks.get, backend=self.backend)
        self.player.trackRequested.connect(lambda track: self.player.start_ready(track, "C:/test.wav"))

    def tearDown(self):
        self.player.stop()

    def test_play_pause_resume_seek_and_automatic_next_use_backend_clock(self):
        self.player.set_queue(["a", "b"])
        self.player.play()
        self.assertEqual(self.player.state, "playing")
        self.backend.setPosition(1700)
        self.assertEqual(self.player.position_seconds(), 1.7)
        self.player.pause()
        self.assertEqual(self.player.state, "paused")
        self.player.play()
        self.assertEqual(self.player.position_seconds(), 1.7)
        self.player.seek(4)
        self.assertEqual(self.backend.clock, 4000)
        self.backend.mediaStatusChanged.emit(QtMultimedia.QMediaPlayer.EndOfMedia)
        self.assertEqual(self.player.current_track, "b")
        self.assertEqual(self.player.position_seconds(), 0)
        self.player.stop()
        self.assertEqual(self.player.state, "stopped")

    def test_waiting_track_resumes_on_analysis_completion_without_polling(self):
        self.tracks["b"]["status"] = "Analyzing"
        self.player.set_queue(["a", "b"])
        self.player.play()
        self.backend.mediaStatusChanged.emit(QtMultimedia.QMediaPlayer.EndOfMedia)
        self.assertEqual(self.player.state, "waiting")
        self.assertEqual(self.player.waiting, "b")
        self.tracks["b"]["status"] = "Ready"
        self.player.track_updated("b")
        self.assertEqual(self.player.state, "playing")

    def test_repeat_shuffle_previous_and_error_tracks(self):
        queue = PlaybackQueue(random.Random(7))
        queue.set_tracks(["a", "b", "c"])
        queue.repeat = "track"
        self.assertEqual(queue.advance(manual=False), "a")
        self.assertEqual(queue.advance(manual=True), "b")
        queue.repeat = "playlist"
        queue.advance()
        self.assertEqual(queue.advance(), "a")
        queue.set_shuffle(True)
        self.assertEqual(queue.current, "a")
        self.assertEqual(set(queue.ordered_tracks()), {"a", "b", "c"})
        queue.set_shuffle(False)
        self.assertEqual(queue.current, "a")
        self.tracks["a"]["status"] = "Error"
        self.player.set_queue(["a", "b"])
        self.player.play()
        self.assertEqual(self.player.current_track, "b")

    def test_reordering_future_queue_does_not_restart_current_audio(self):
        self.player.set_queue(["a", "b", "c"])
        self.player.play()
        self.backend.setPosition(1200)
        self.player.replace_queue(["a", "c", "b"])
        self.assertEqual(self.backend.clock, 1200)
        self.assertEqual(self.backend.play_calls, 1)
        self.backend.mediaStatusChanged.emit(QtMultimedia.QMediaPlayer.EndOfMedia)
        self.assertEqual(self.player.current_track, "c")

    def test_scheduler_offsets_pause_and_seek_are_driven_only_by_audio_position(self):
        from types import SimpleNamespace
        settings = show_settings({"latency_ms": 100, "lamp_offsets": {"b": 50}}, ["a", "b"])
        scheduler = LightShowScheduler(self.player, ["a", "b"], settings)
        scheduler.timeline = SimpleNamespace(state_at=lambda when, lamp: (255, 0, 0) if when >= 0.5 else (0, 0, 0), close=lambda: None)
        scheduler.analysis = SimpleNamespace(frame_at=lambda _when: np.zeros(42), close=lambda: None)
        colors = []
        scheduler.colorsReady.connect(colors.append)
        self.player.set_queue(["a"])
        scheduler.set_active(True)
        self.player.play()
        self.backend.setPosition(350)
        scheduler.tick()
        self.assertEqual(colors[-1], {"a": (0, 0, 0), "b": (255, 0, 0)})
        before = len(colors)
        QtTest.QTest.qWait(220)
        self.assertEqual(len(colors), before)
        self.backend.setPosition(400)
        scheduler.tick()
        self.assertEqual(colors[-1]["a"], (255, 0, 0))
        self.player.pause()
        before = len(colors)
        scheduler.tick()
        self.assertEqual(len(colors), before)
        self.player.seek(0)
        self.assertEqual(colors[-1], {"a": (0, 0, 0), "b": (0, 0, 0)})
        self.assertEqual(scheduler.samples[-1]["audio_position"], 0)
        scheduler.shutdown()

    def test_graphs_follow_audio_at_50hz_without_extra_lamp_commands(self):
        from types import SimpleNamespace
        settings = show_settings({"update_hz": 1}, ["a"])
        scheduler = LightShowScheduler(self.player, ["a"], settings)
        scheduler.timeline = SimpleNamespace(state_at=lambda when, lamp: (100, 0, 0), close=lambda: None)
        frame = np.ones(42, dtype=np.float32) * 0.8
        frame[5] = 0
        scheduler.analysis = SimpleNamespace(frame_at=lambda when: frame,
            beats=np.array([0.1, 0.5, 0.9]), onsets=np.array([0.1, 0.5, 0.9]), close=lambda: None)
        commands, previews, beats = [], [], []
        scheduler.colorsReady.connect(commands.append)
        scheduler.previewReady.connect(lambda *args: previews.append(args))
        scheduler.beatReady.connect(lambda *args: beats.append(args))
        try:
            self.player.set_queue(["a"])
            scheduler.set_active(True)
            self.player.play()
            before = len(commands)
            for millis in range(20, 1000, 20):
                self.backend.clock = millis
                scheduler.update_visuals()
            self.assertEqual(scheduler.visual_timer.interval(), 20)
            self.assertEqual(scheduler.timer.interval(), 1000)
            self.assertEqual(len(commands), before)
            self.assertGreaterEqual(len(previews), 49)
            accents = [point for point in beats if point[2]]
            self.assertEqual([round(point[3], 2) for point in accents], [0.1, 0.5, 0.9])
            self.assertGreater(accents[0][0], 0.8)
            self.assertEqual(beats[-1][3], 0.98)
            self.player.pause()
            self.assertFalse(scheduler.visual_timer.isActive())
            count = len(beats)
            self.backend.clock = 2000
            scheduler.update_visuals()
            self.assertEqual(len(beats), count)
        finally:
            scheduler.shutdown()


class MusicIntegrationTests(unittest.TestCase):
    def setUp(self):
        application()
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.controller = FakeLampController()
        self.backend = FakeMediaBackend()
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        with patch("ui.AudioVisualizer.sc.all_microphones", return_value=[]), \
                patch("ui.AudioVisualizer.load_settings", return_value=normalized_settings({})):
            self.window = AudioVisualizer(self.controller, self.loop, music_root=self.root / "music", music_backend=self.backend)
        self.window.save_current_settings = lambda: None
        self.panel = self.window.music_panel

    def tearDown(self):
        self.window.request_exit()
        self.panel.jobs.wait(10000)
        wait_until(lambda: not self.window.pending_lamp_updates and not self.window.queued_colors, 3)
        self.window.request_exit()
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(2)
        self.loop.close()
        # Destroy Qt objects on the GUI thread; Python's cycle collector can run
        # in the analysis worker while collecting windows from preceding tests.
        self.window.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.DeferredDelete)
        self.temporary.cleanup()

    def test_ten_files_auto_analysis_playlist_player_seek_and_restart(self):
        paths = []
        first = self.root / "track0.wav"
        write_beats(first, duration=4)
        paths.append(str(first))
        for index in range(1, 10):
            other = self.root / f"track{index}.wav"
            shutil.copyfile(first, other)
            paths.append(str(other))
        heartbeat = []
        timer = QtCore.QTimer()
        timer.setInterval(10)
        timer.timeout.connect(lambda: heartbeat.append(1))
        timer.start()
        self.panel.import_paths(paths)
        self.assertTrue(wait_until(lambda: len(self.panel.library.tracks()) == 10, 5))
        self.assertTrue(wait_until(lambda: all(track["status"] == "Ready" for track in self.panel.library.tracks()), 20),
                        self.panel.library.tracks())
        timer.stop()
        self.assertGreater(len(heartbeat), 3)
        tracks = self.panel.library.tracks()
        playlist = self.panel.library.create_playlist("Test playlist")
        self.panel.library.set_playlist_tracks(playlist["id"], [track["id"] for track in tracks])
        self.panel.refresh_playlists()
        self.panel.play_playlist()
        self.assertTrue(wait_until(lambda: self.panel.player.state == "playing", 10), self.panel.status.text())
        self.assertEqual(self.window.light_source, "player")
        self.assertEqual(self.controller.rate, 10)
        self.assertIsNotNone(self.panel.scheduler.timeline)
        self.backend.setPosition(1500)
        self.panel.scheduler.tick()
        self.panel.player.pause()
        self.panel.player.seek(500 / 1000)
        self.assertEqual(self.panel.scheduler.samples[-1]["audio_position"], 0.5)
        self.panel.player.play()
        self.backend.mediaStatusChanged.emit(QtMultimedia.QMediaPlayer.EndOfMedia)
        self.assertTrue(wait_until(lambda: self.panel.player.state == "playing", 10))
        self.assertEqual(self.panel.player.current_track, tracks[1]["id"])
        self.panel.apply_show_settings()
        restored = MusicLibrary(self.panel.library.root)
        self.assertEqual(len(restored.tracks()), 10)
        self.assertEqual(len(restored.playlists()[0]["tracks"]), 10)
        self.assertTrue(all(track["analysis_version"] == ANALYSIS_VERSION for track in restored.tracks()))

    def test_source_ownership_rejects_conflicting_live_commands(self):
        self.window.select_light_source("player")
        count = len(self.controller.calls)
        self.window.send_colors({"a": (255, 0, 0)}, 1)
        self.assertEqual(len(self.controller.calls), count)
        self.window.send_colors({"a": (0, 255, 0)}, 1, source="player")
        self.assertTrue(wait_until(lambda: len(self.controller.calls) > count, 2))
        self.window.select_light_source("live")
        self.assertEqual(self.controller.rate, 20)
        count = len(self.controller.calls)
        self.window.on_music_colors({"a": (255, 0, 0)})
        self.assertEqual(len(self.controller.calls), count)

    def test_directory_import_and_invalid_audio_are_handled_in_worker(self):
        folder = self.root / "nested"
        folder.mkdir()
        write_beats(folder / "song.flac", duration=3)
        (folder / "bad.wav").write_bytes(b"not audio")
        (folder / "ignored.txt").write_text("ignore", encoding="utf-8")
        self.panel.import_paths([str(folder)])
        self.assertTrue(wait_until(lambda: len(self.panel.library.tracks()) == 2, 5))
        self.assertTrue(wait_until(lambda: all(track["status"] in ("Ready", "Error") for track in self.panel.library.tracks()), 15))
        self.assertEqual({track["status"] for track in self.panel.library.tracks()}, {"Ready", "Error"})

    def test_add_dialog_is_asynchronous_and_reusable(self):
        ticks = []
        timer = QtCore.QTimer()
        timer.timeout.connect(lambda: ticks.append(1))
        timer.start(10)
        self.panel.choose_files()
        dialog = self.panel.import_dialog
        self.assertIsNotNone(dialog)
        self.assertTrue(dialog.testOption(QtWidgets.QFileDialog.DontUseNativeDialog))
        self.assertEqual(dialog.fileMode(), QtWidgets.QFileDialog.ExistingFiles)
        self.panel.choose_files()
        self.assertIs(self.panel.import_dialog, dialog)
        QtTest.QTest.qWait(100)
        self.assertGreater(len(ticks), 2)
        dialog.reject()
        self.assertIsNone(self.panel.import_dialog)
        self.assertEqual(self.panel.library.tracks(), [])
        self.panel.choose_directory()
        self.assertEqual(self.panel.import_dialog.fileMode(), QtWidgets.QFileDialog.Directory)
        self.panel.import_dialog.reject()
        timer.stop()

    def test_slow_import_does_not_block_gui_and_saves_only_once_per_batch(self):
        entered, release = threading.Event(), threading.Event()
        discover = self.panel.jobs._discover_files
        worker_threads = []

        def slow_discover(paths, cancel):
            worker_threads.append(threading.get_ident())
            entered.set()
            release.wait(3)
            discover(paths, cancel)

        paths = []
        for index in range(12):
            path = self.root / f"pending{index}.wav"
            path.write_bytes(b"audio")
            paths.append(str(path))
        ticks = []
        timer = QtCore.QTimer()
        timer.timeout.connect(lambda: ticks.append(1))
        timer.start(10)
        try:
            with patch.object(self.panel.jobs, "_discover_files", side_effect=slow_discover), \
                    patch.object(self.panel.jobs, "analyse"), \
                    patch.object(self.panel.library, "save", wraps=self.panel.library.save) as save:
                self.panel.import_paths(paths)
                self.assertTrue(wait_until(entered.is_set, 2))
                QtTest.QTest.qWait(100)
                self.assertGreater(len(ticks), 2)
                self.assertNotEqual(worker_threads[0], threading.get_ident())
                self.assertEqual(self.panel.library.tracks(), [])
                release.set()
                self.assertTrue(wait_until(lambda: len(self.panel.library.tracks()) == 12, 3))
                self.assertEqual(save.call_count, 1)
        finally:
            release.set()
            timer.stop()

    def test_latency_measurement_is_individual_and_handles_offline_lamp(self):
        self.panel.measure_latency()
        self.assertTrue(wait_until(lambda: self.panel.measure_button.isEnabled(), 5))
        self.assertEqual(self.panel.settings["lamp_offsets"]["a"], 20)
        self.assertEqual(self.panel.lamp_table.item(1, 3).text(), "Недоступна")

    def test_new_playlist_is_selected_for_editing_and_adding_tracks(self):
        with patch("ui.MusicPanel.QtWidgets.QInputDialog.getText", return_value=("First", True)):
            self.panel.create_playlist()
        with patch("ui.MusicPanel.QtWidgets.QInputDialog.getText", return_value=("Second", True)):
            self.panel.create_playlist()
        self.assertEqual(self.panel.playlist_tabs.tabText(self.panel.playlist_tabs.currentIndex()), "Second")
        self.assertFalse(hasattr(self.panel, "playlist_combo"))
        self.assertEqual(self.panel.add_playlist_combo.currentText(), "Second")

    def test_top_level_navigation_header_and_contextual_controls(self):
        self.assertEqual([self.window.tabs.tabText(i) for i in range(self.window.tabs.count())],
                         ["Управление", "Музыка", "Плейлист", "Бит", "Диагностика"])
        self.assertFalse(hasattr(self.panel, "tabs"))
        self.assertTrue(self.window.isAncestorOf(self.panel.player_header))
        self.assertTrue(self.window.control_page.isAncestorOf(self.panel.settings_page))
        self.window.tabs.setCurrentWidget(self.window.beat_page)
        self.window.select_light_source("player")
        self.assertIs(self.window.tabs.currentWidget(), self.window.beat_page)
        self.assertTrue(self.window.control_page.isEnabled())
        self.assertEqual(self.window.control_stack.currentIndex(), 1)
        self.assertIs(self.window.visual_panel.parent(), self.window.control_stack.widget(1))
        self.panel.update_spin.setValue(7)
        self.panel.apply_show_settings()
        self.window.select_light_source("live")
        self.assertEqual(self.window.control_stack.currentIndex(), 0)
        self.assertIs(self.window.visual_panel.parent(), self.window.control_stack.widget(0))
        self.assertEqual(self.panel.settings["update_hz"], 7)
        self.panel.queue_button.click()
        self.assertFalse(self.panel.playback_queue.isHidden())

    def test_header_transport_and_music_graphs_work_from_any_tab(self):
        for index in range(2):
            path = self.root / f"song{index}.wav"
            write_beats(path, duration=3)
            self.panel.import_paths([str(path)])
        self.assertTrue(wait_until(lambda: len(self.panel.library.tracks()) == 2 and
            all(track["status"] == "Ready" for track in self.panel.library.tracks()), 15))
        tracks = self.panel.library.tracks()
        self.panel.player.set_queue([track["id"] for track in tracks])
        self.window.tabs.setCurrentIndex(4)
        self.panel.play_pause_button.click()
        self.assertTrue(wait_until(lambda: self.panel.player.state == "playing", 5))
        self.assertEqual(self.window.tabs.currentIndex(), 4)
        self.assertEqual(self.panel.now_title.text(), "song0")
        self.assertEqual(self.panel.play_pause_button.toolTip(), "Пауза")
        self.backend.setPosition(500)
        self.panel.scheduler.update_visuals()
        self.assertEqual(self.window.beat_graph_widget.history[-1][0], 0.5)
        self.assertGreater(float(np.max(self.window.spectrum_widget.bars)), 0)
        self.panel.play_pause_button.click()
        self.assertEqual(self.panel.player.state, "paused")
        self.assertEqual(self.panel.play_pause_button.toolTip(), "Воспроизвести")
        self.panel.play_pause_button.click()
        self.assertEqual(self.panel.player.state, "playing")
        self.panel.volume_slider.setValue(31)
        self.assertEqual(self.backend.volume, 31)
        self.panel.seek_slider.setValue(1800)
        self.panel.seek_slider.sliderReleased.emit()
        self.assertEqual(self.backend.clock, 1800)
        self.assertEqual(len(self.window.beat_graph_widget.history), 1)
        self.panel.player.queue.repeat = "track"
        self.panel.next_button.click()
        self.assertTrue(wait_until(lambda: self.panel.player.state == "playing", 5))
        self.assertEqual(self.panel.now_title.text(), "song1")
        self.panel.previous_button.click()
        self.assertTrue(wait_until(lambda: self.panel.player.state == "playing", 5))
        self.assertEqual(self.panel.now_title.text(), "song0")
        self.panel.stop_button.click()
        self.assertEqual(self.panel.player.state, "stopped")

    def test_switching_to_live_cancels_play_intent_while_preparing(self):
        playlist = self.panel.library.create_playlist("Pending")
        track = self.panel.library.add_search_track(playlist["id"], "Song", "Artist")
        with patch.object(self.panel.jobs, "analyse"):
            self.panel.player.set_queue([track["id"]])
            self.panel.play()
            self.assertTrue(self.panel.player.want_play)
            self.window.select_light_source("live")
            self.assertFalse(self.panel.player.want_play)
            self.assertEqual(self.panel.player.state, "paused")

    def test_closing_music_mode_sends_off_after_invalidating_old_scene(self):
        self.window.select_light_source("player")
        self.window.send_colors({"a": (100, 100, 0)}, 1, source="player")
        self.assertTrue(wait_until(lambda: any(call[0].get("a") == (100, 100, 0) for call in self.controller.calls)))
        self.window.request_exit()
        self.assertTrue(wait_until(lambda: not self.window.pending_lamp_updates and not self.window.queued_colors, 5))
        latest = {}
        for call in self.controller.calls:
            latest.update(call[0])
        self.assertEqual(latest, {"a": (0, 0, 0), "b": (0, 0, 0)})


class NativePlaybackSmokeTest(unittest.TestCase):
    def test_muted_local_wav_advances_native_audio_clock(self):
        application()
        player = PCMPlayerBackend()
        if not player.isAvailable():
            self.skipTest("Qt multimedia service unavailable")
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "silence.wav"
            sf.write(path, np.zeros((48000, 2)), 48000)
            player.setVolume(0)
            player.setNotifyInterval(20)
            player.setMedia(QtMultimedia.QMediaContent(QtCore.QUrl.fromLocalFile(str(path))))
            player.play()
            try:
                self.assertTrue(wait_until(lambda: player.position() >= 100 or player.error_code() != QtMultimedia.QMediaPlayer.NoError, 5))
                self.assertEqual(player.error_code(), QtMultimedia.QMediaPlayer.NoError, player.errorString())
                self.assertGreaterEqual(player.position(), 100)
            finally:
                player.stop()
                player.setMedia(QtMultimedia.QMediaContent())
                application().processEvents()

    def test_native_pause_resume_seek_stop_and_end_clock(self):
        application()
        player = PCMPlayerBackend()
        if not player.isAvailable():
            self.skipTest("Audio output unavailable")
        ended = []
        player.mediaStatusChanged.connect(lambda status: ended.append(status) if status == QtMultimedia.QMediaPlayer.EndOfMedia else None)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "Тишина 01.wav"
            sf.write(path, np.zeros((96000, 2)), 48000)
            player.setVolume(0)
            player.setMedia(QtMultimedia.QMediaContent(QtCore.QUrl.fromLocalFile(str(path))))
            player.play()
            try:
                self.assertTrue(wait_until(lambda: player.position() > 100, 3))
                player.pause()
                paused = player.position()
                QtTest.QTest.qWait(150)
                self.assertLess(abs(player.position() - paused), 40)
                player.play()
                self.assertTrue(wait_until(lambda: player.position() > paused + 40, 2))
                player.pause()
                player.setPosition(1600)
                self.assertEqual(player.position(), 1600)
                player.play()
                self.assertTrue(wait_until(lambda: bool(ended), 3))
                player.stop()
                self.assertEqual(player.position(), 0)
            finally:
                player.stop()
                player.setMedia(QtMultimedia.QMediaContent())
                application().processEvents()


if __name__ == "__main__":
    unittest.main()
