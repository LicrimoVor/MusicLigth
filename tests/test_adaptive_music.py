from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import numpy as np
import soundfile as sf
from PyQt5 import QtWidgets

import test_music as shared
from music.analysis import ANALYSIS_VERSION, AnalysisData, MusicAnalyser, track_dynamic_beats
from music.storage import CancelledAnalysis, MusicLibrary
from music.timeline import LightTimeline, TimelineGenerator, show_settings, timeline_key


def write_rhythm(path, beats, duration, amplitudes=None):
    rate = 22050
    audio = np.zeros(round(duration * rate), dtype=np.float32)
    pulse_time = np.arange(round(0.14 * rate)) / rate
    pulse = np.sin(2 * np.pi * 75 * pulse_time) * np.exp(-30 * pulse_time)
    amplitudes = np.full(len(beats), 0.5) if amplitudes is None else amplitudes
    for beat, amplitude in zip(beats, amplitudes):
        start = round(beat * rate)
        count = min(len(pulse), len(audio) - start)
        if count > 0:
            audio[start:start + count] += pulse[:count] * amplitude
    sf.write(path, audio, rate)


class AdaptiveBeatTests(unittest.TestCase):
    def test_tempo_changes_and_silent_break_are_tracked_end_to_end(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            expected = np.r_[np.arange(0.6, 12, 60 / 80), np.arange(12.3, 24, 60 / 150),
                             np.arange(30, 39, 60 / 100)]
            write_rhythm(root / "changes.wav", expected, 40)
            result = MusicAnalyser(root / "cache").analyse(root / "changes.wav")
            data = AnalysisData(result["cache_path"])
            try:
                for position, bpm in ((6, 80), (18, 150), (34, 100)):
                    self.assertAlmostEqual(data.tempo_at(position), bpm, delta=5)
                self.assertEqual(data.tempo_at(27), 0)
                self.assertFalse(np.any((data.beats > 24.8) & (data.beats < 29.5)))
                central = expected[((expected > 2) & (expected < 11)) |
                                   ((expected > 14) & (expected < 23)) | (expected > 32)]
                errors = np.min(abs(data.beats[:, None] - central), axis=0)
                self.assertGreaterEqual(float(np.mean(errors < 0.055)), 0.9)
                self.assertLess(result["tempo_min"], 90)
                self.assertGreater(result["tempo_max"], 140)
            finally:
                data.close()

    def test_accelerando_keeps_fast_final_beats_instead_of_trimming_them(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            expected = np.cumsum(60 / np.linspace(75, 165, 36))
            write_rhythm(root / "ramp.wav", expected, expected[-1] + 0.5)
            result = MusicAnalyser(root / "cache").analyse(root / "ramp.wav")
            data = AnalysisData(result["cache_path"])
            try:
                errors = np.min(abs(data.beats[:, None] - expected), axis=0)
                self.assertGreaterEqual(float(np.mean(errors < 0.055)), 0.9)
                self.assertGreater(data.tempo_at(expected[-3]), data.tempo_at(expected[3]) + 50)
                self.assertAlmostEqual(float(data.beats[-1]), float(expected[-1]), delta=0.055)
            finally:
                data.close()

    def test_local_tempo_work_can_be_cancelled(self):
        event = threading.Event()
        event.set()
        with self.assertRaises(CancelledAnalysis):
            track_dynamic_beats(np.ones(2000), 22050, 441, event)

    def test_quiet_section_can_have_a_different_tempo_from_the_loud_section(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            expected = np.r_[np.arange(0.6, 12, 0.5), np.arange(12.3, 24, 60 / 90)]
            write_rhythm(root / "quiet.wav", expected, 25, np.where(expected < 12, 0.6, 0.03))
            result = MusicAnalyser(root / "cache").analyse(root / "quiet.wav")
            data = AnalysisData(result["cache_path"])
            try:
                wanted = expected[expected > 14]
                errors = np.min(abs(data.beats[:, None] - wanted), axis=0)
                self.assertGreaterEqual(float(np.mean(errors < 0.055)), 0.9)
                self.assertAlmostEqual(data.tempo_at(18), 90, delta=5)
            finally:
                data.close()


class PaletteTimelineTests(unittest.TestCase):
    def test_custom_palette_changes_colors_and_cache_without_reanalysing_audio(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "beats.wav"
            shared.write_beats(source, duration=3)
            result = MusicAnalyser(root / "cache").analyse(source)
            features = Path(result["cache_path"]) / "features.npy"
            before = features.stat().st_mtime_ns
            red = show_settings({"color_mode": "custom", "palette": [[255, 0, 0]] * 3}, ["a"])
            blue = show_settings({"color_mode": "custom", "palette": [[0, 0, 255]] * 3}, ["a"])
            generator = TimelineGenerator(root / "cache")
            red_path = generator.generate(result["cache_path"], red)
            blue_path = generator.generate(result["cache_path"], blue)
            self.assertNotEqual(red_path, blue_path)
            for path, channel in ((red_path, 0), (blue_path, 2)):
                timeline = LightTimeline(path)
                try:
                    self.assertGreater(float(timeline.frames[:, 0, channel].max()), 0.9)
                    for other in set(range(3)) - {channel}:
                        self.assertEqual(float(timeline.frames[:, 0, other].max()), 0)
                finally:
                    timeline.close()
            self.assertEqual(features.stat().st_mtime_ns, before)


class MusicCustomizationTests(unittest.TestCase):
    setUp = shared.MusicIntegrationTests.setUp
    tearDown = shared.MusicIntegrationTests.tearDown

    def test_colors_are_saved_separately_from_live_settings_and_available_in_tray(self):
        quiet = self.window.quiet_color
        for button in self.panel.music_color_buttons:
            button.set_color((0, 200, 80))
        self.panel.apply_show_settings()
        self.assertEqual(self.panel.settings["color_mode"], "custom")
        self.assertEqual(self.panel.settings["palette"], [[0, 200, 80]] * 3)
        self.assertEqual(MusicLibrary(self.panel.library.root).settings()["show"]["palette"], [[0, 200, 80]] * 3)
        self.assertEqual(self.window.quiet_color, quiet)
        self.window.select_light_source("player")
        self.assertTrue(self.window.tray.music_colors_menu.menuAction().isVisible())
        actions = [action.text() for action in self.window.tray.menu.actions()]
        self.assertNotIn("Открыть приложение", actions)
        self.assertNotIn("Свернуть в трей", actions)
        labels = [label.text() for label in self.panel.settings_page.findChildren(QtWidgets.QLabel)]
        self.assertFalse(any("Положительная компенсация" in text for text in labels))

    def test_palette_update_while_playing_keeps_audio_position(self):
        source = self.root / "palette.wav"
        shared.write_beats(source, duration=3)
        self.panel.import_paths([str(source)])
        self.assertTrue(shared.wait_until(lambda: self.panel.library.tracks() and self.panel.library.tracks()[0]["status"] == "Ready", 10))
        identity = self.panel.library.tracks()[0]["id"]
        self.panel.player.set_queue([identity])
        self.panel.play()
        self.assertTrue(shared.wait_until(lambda: self.panel.player.state == "playing", 10))
        self.backend.setPosition(1100)
        before = self.backend.play_calls
        self.panel.music_color_buttons[0].set_color((255, 0, 0))
        self.panel.apply_show_settings()
        expected = timeline_key(self.panel.library.track(identity)["content_id"], self.panel.settings)
        self.assertTrue(shared.wait_until(lambda: self.panel.timeline_hash == expected, 10))
        self.assertEqual(self.backend.position(), 1100)
        self.assertEqual(self.backend.play_calls, before)

    def test_old_ready_remote_analysis_is_rebuilt_on_play(self):
        playlist = self.panel.library.create_playlist("Old")
        track = self.panel.library.add_search_track(playlist["id"], "Song")
        self.panel.library.update_track(track["id"], status="Ready", analysis_version=ANALYSIS_VERSION - 1)
        with patch.object(self.panel, "enqueue_analysis") as analyse:
            self.panel.prepare_track(track["id"])
        self.assertEqual(analyse.call_args.args[0]["id"], track["id"])
        self.assertTrue(analyse.call_args.kwargs["priority"])

    def test_widget_uses_shared_samples_and_survives_main_window_hiding(self):
        widget = self.window.desktop_widget
        self.window.tray.widget_action.trigger()
        self.assertTrue(widget.isVisible())
        self.window.show()
        with patch.object(QtWidgets.QSystemTrayIcon, "isSystemTrayAvailable", return_value=True):
            self.window.close()
        self.assertFalse(self.window.isVisible())
        self.assertTrue(widget.isVisible())
        self.window.update_beat_graph(0.7, 0.9, True, 1.0)
        self.assertEqual(widget.graph.history[-1], (1.0, 0.7, 0.9, True))
        self.assertEqual(widget.level.text(), "70%")
        widget.close()
        self.assertFalse(self.window.tray.widget_action.isChecked())
        self.assertFalse(self.panel.library.settings()["desktop_widget"]["enabled"])
        self.assertFalse(widget.graph.paint_timer.isActive())
        self.window.update_beat_graph(0.2, 0.3, False, 2.0)
        self.assertEqual(len(widget.graph.history), 1)

    def test_widget_position_and_enabled_state_persist_across_exit(self):
        widget = self.window.desktop_widget
        widget.set_enabled(True)
        widget.move(100, 120)
        widget.positionSaved.emit()
        self.assertEqual(self.panel.library.settings()["desktop_widget"]["position"], [100, 120])
        self.window.request_exit()
        self.assertTrue(MusicLibrary(self.panel.library.root).settings()["desktop_widget"]["enabled"])


if __name__ == "__main__":
    unittest.main()
