import asyncio
from concurrent.futures import Future
import os
from pathlib import Path
import sys
import threading
import tempfile
import time
from types import SimpleNamespace
import unittest
import warnings
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

import numpy as np
import soundcard as sc
from PyQt5 import QtCore, QtGui, QtTest, QtWidgets

from model.audio import CAPTURE_BUFFER_SIZE, FRAME_SIZE, SAMPLE_RATE
from model.audio_stream import AudioCaptureWorker, record_available
from model.settings import normalized_settings
from ui.AudioVisualizer import AudioVisualizer
from ui.widgets import BeatGraphWidget, SpectrumWidget


class FakeMicrophone:
    name = "Test audio"

    def __init__(self):
        self.opens = 0
        self.frames = 0
        self.closed = False
        self.options = {}
        self.requested_frames = []

    def recorder(self, **options):
        self.opens += 1
        self.options = options
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def record(self, numframes):
        self.requested_frames.append(numframes)
        numframes = FRAME_SIZE if numframes is None else numframes
        time.sleep(0.003)
        self.frames += 1
        if self.frames == 10:
            wave = 0.4 * np.sin(2 * np.pi * 70 * np.arange(numframes) / SAMPLE_RATE)
            return np.column_stack((wave, wave))
        return np.zeros((numframes, 2))


class FakeController:
    def __init__(self):
        self.devices = [SimpleNamespace(id=lamp_id) for lamp_id in ("a", "b")]
        self.calls = []
        self.update_rate = None
        self.delays = {}

    def set_update_rate(self, value):
        self.update_rate = value

    async def set_colors(self, colors, brightness, **options):
        self.calls.append((dict(colors), brightness, options, time.monotonic()))
        delay = self.delays.get(next(iter(colors)), 0)
        if delay:
            await asyncio.sleep(delay)


class VisualizerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        # The Windows offscreen platform has no system font database.
        if sys.platform == "win32":
            QtGui.QFontDatabase.addApplicationFont("C:/Windows/Fonts/segoeui.ttf")
            cls.app.setFont(QtGui.QFont("Segoe UI", 10))

    def setUp(self):
        self.music_directory = tempfile.TemporaryDirectory()
        self.controller = FakeController()
        self.loop = asyncio.new_event_loop()
        self.loop_thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.loop_thread.start()
        with patch("ui.AudioVisualizer.sc.all_microphones", return_value=[]), \
                patch("ui.AudioVisualizer.load_settings", return_value=normalized_settings({})):
            self.window = AudioVisualizer(self.controller, self.loop, music_root=self.music_directory.name)
        self.window.save_current_settings = lambda: None

    def tearDown(self):
        self.window.stop_stream(send_to_lamps=False)
        if self.window.audio_worker:
            self.window.audio_worker.wait(1000)
        self.window.queued_colors.clear()
        self.window.queued_options.clear()
        self.wait_sends()
        self.window.close()
        self.window.music_panel.jobs.wait(2000)
        self.window.close()
        self.app.processEvents()
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.loop_thread.join(timeout=2)
        self.loop.close()
        self.music_directory.cleanup()

    def wait_sends(self):
        for future in list(self.window.pending_lamp_updates.values()):
            future.result(timeout=2)

    def sent_colors(self):
        colors = {}
        for call in self.controller.calls:
            colors.update(call[0])
        return colors

    def block_sends(self):
        pending = Future()
        for lamp_id in ("a", "b"):
            self.window.pending_lamp_updates[lamp_id] = pending
            self.window.inflight_updates[lamp_id] = ((100, 20, 10), (1, True))
        return pending

    def test_stop_is_queued_while_previous_send_is_pending(self):
        pending = self.block_sends()
        self.window.send_colors({"a": (200, 30, 10)}, 1)
        self.window.stop_stream()
        self.assertEqual(self.controller.calls, [])
        self.assertEqual(self.window.queued_colors, {"a": (0, 0, 0), "b": (0, 0, 0)})
        pending.set_result(None)
        self.window.flush_lamp_updates()
        self.wait_sends()
        self.assertEqual(self.sent_colors(), {"a": (0, 0, 0), "b": (0, 0, 0)})

    def test_latest_color_wins_and_disabling_lamp_preserves_other_updates(self):
        pending = self.block_sends()
        self.window.send_colors({"a": (100, 30, 10), "b": (90, 20, 10)}, 0.5)
        self.window.send_colors({"a": (200, 30, 10)}, 0.7)
        self.window.set_lamp_enabled("b", False)
        pending.set_result(None)
        self.window.flush_lamp_updates()
        self.wait_sends()
        self.assertEqual(self.sent_colors(), {"a": (200, 30, 10), "b": (0, 0, 0)})
        self.assertGreater(next(call[1] for call in self.controller.calls if "a" in call[0]), 0)

    def test_color_updates_do_not_follow_ui_frame_rate(self):
        for value in range(20):
            self.window.send_colors({"a": (value * 10, 30, 10)}, 1)
            self.wait_sends()
        self.assertEqual(len(self.controller.calls), 1)
        self.assertEqual(self.window.queued_colors["a"], (190, 30, 10))
        self.window.last_lamp_sends["a"] -= 0.06
        self.window.flush_lamp_updates()
        self.wait_sends()
        self.assertEqual(self.controller.calls[-1][0]["a"], (190, 30, 10))

    def test_failed_static_update_retries_latest_requested_color(self):
        pending = Future()
        self.window.pending_lamp_updates["a"] = pending
        self.window.inflight_updates["a"] = ((100, 30, 10), (1, False))
        pending.set_result({"a": "unavailable"})
        self.window.send_colors({"a": (200, 30, 10)}, 1, music=False)
        self.window.lamp_retry_after["a"] = 0
        self.window.flush_lamp_updates()
        self.wait_sends()
        self.assertEqual(self.controller.calls[-1][0], {"a": (200, 30, 10)})
        self.assertFalse(self.controller.calls[-1][2]["music"])

    def test_close_waits_for_queued_off_without_blocking_event_loop(self):
        pending = self.block_sends()
        self.window.timer.start()
        event = QtGui.QCloseEvent()
        self.window.closeEvent(event)
        self.assertFalse(event.isAccepted())
        self.assertEqual(self.window.queued_colors, {"a": (0, 0, 0), "b": (0, 0, 0)})
        pending.set_result(None)
        self.window.flush_lamp_updates()
        self.wait_sends()
        event = QtGui.QCloseEvent()
        self.assertTrue(self.window.music_panel.jobs.wait(2000))
        self.window.closeEvent(event)
        self.assertTrue(event.isAccepted())
        self.assertEqual(self.sent_colors(), {"a": (0, 0, 0), "b": (0, 0, 0)})

    def test_static_mode_starts_without_microphone_and_sends_once(self):
        self.window.mode_combo.setCurrentIndex(self.window.mode_combo.findData("static"))
        self.window.start_stream()
        self.assertTrue(self.window.timer.isActive())
        self.assertIsNone(self.window.audio_worker)
        self.wait_sends()
        for _ in range(20):
            self.window.update_visualization()
        self.assertEqual(len(self.controller.calls), 2)
        self.assertFalse(self.controller.calls[0][2]["music"])

    def test_static_mode_still_updates_beat_graph_without_sending_music_colors(self):
        self.window.mode_combo.setCurrentIndex(self.window.mode_combo.findData("static"))
        self.window.start_stream()
        self.wait_sends()
        calls = len(self.controller.calls)
        frame = (np.ones(32, dtype=np.float32) * 0.5, 0.7, True, 1.0)
        worker = SimpleNamespace(take_frame=lambda: frame)
        with patch.object(self.window, "audio_worker", worker):
            self.window.update_visualization()
        self.assertEqual(len(self.controller.calls), calls)
        self.assertEqual(self.window.beat_graph_widget.history[-1][0], 1.0)
        self.assertTrue(self.window.beat_graph_widget.history[-1][3])
        self.assertGreater(float(np.max(self.window.spectrum_widget.bars)), 0)

    def test_capture_uses_one_recorder_and_preserves_short_attacks(self):
        microphone = FakeMicrophone()
        worker = AudioCaptureWorker(microphone)
        worker.start()
        try:
            deadline = time.monotonic() + 2
            while microphone.frames < 12 and time.monotonic() < deadline:
                QtTest.QTest.qWait(20)
            frame = worker.take_frame()
            self.assertIsNotNone(frame)
            self.assertTrue(frame[2])
            self.assertGreater(frame[1], 0.5)
            self.assertEqual(microphone.opens, 1)
            self.assertGreater(microphone.frames, 10)
            self.assertEqual(microphone.options["blocksize"], CAPTURE_BUFFER_SIZE)
            self.assertTrue(all(value is None for value in microphone.requested_frames))
        finally:
            worker.requestInterruption()
            self.assertTrue(worker.wait(1000))
        self.assertTrue(microphone.closed)

    def test_source_change_restarts_capture_without_blocking_ui(self):
        first, second = FakeMicrophone(), FakeMicrophone()
        self.window.mic_devices = [first, second]
        self.window.device_combo.clear()
        self.window.device_combo.addItem(first.name, first.name)
        self.window.device_combo.addItem("Second audio", "Second audio")
        self.window.start_stream()
        QtTest.QTest.qWait(40)
        self.window.device_combo.setCurrentIndex(1)
        QtTest.QTest.qWait(80)
        self.assertIs(self.window.audio_worker.microphone, second)
        self.assertEqual(first.opens, 1)
        self.assertTrue(first.closed)
        self.assertEqual(second.opens, 1)

    def test_graph_uses_time_instead_of_sample_count_and_handles_resize(self):
        graph = BeatGraphWidget(duration_seconds=8)
        graph.resize(850, 310)
        for timestamp, level in ((100, 0), (100.05, 1), (101, 0.2), (108, 0.8)):
            graph.add_level(level, level / 2, level == 1, timestamp)
        path = graph.curve_path(graph.plot_rect(), list(graph.history), 1)
        self.assertAlmostEqual(path.elementAt(path.elementCount() - 1).x, graph.plot_rect().right())
        first_pixmap = graph.background_pixmap()
        self.assertEqual(first_pixmap.devicePixelRatioF(), graph.devicePixelRatioF())
        self.assertFalse(graph.grab().isNull())
        graph.resize(300, 220)
        self.app.processEvents()
        self.assertFalse(graph.grab().isNull())
        graph.add_level(float("nan"), float("inf"), timestamp=109)
        self.assertEqual(graph.history[-1][1:3], (0.0, 0.0))
        graph.clear()
        self.assertFalse(graph.grab().isNull())
        graph.deleteLater()

    def test_spectrum_fits_narrow_widget_and_decays(self):
        widget = SpectrumWidget()
        widget.resize(180, 170)
        widget.set_data(np.ones(32), 1, (20, 200, 255))
        previous = widget.bars.copy()
        self.assertFalse(widget.grab().isNull())
        widget.last_update -= 0.2
        widget.set_data(np.zeros(32), 0, (0, 0, 0))
        self.assertTrue(np.all(widget.bars < previous))
        widget.deleteLater()

    def test_slow_lamp_does_not_limit_healthy_lamp_updates(self):
        self.controller.delays["a"] = 0.3
        for value in range(12):
            self.window.send_colors({"a": (value * 15, 20, 10), "b": (value * 15, 20, 10)}, 1)
            QtTest.QTest.qWait(20)
        healthy = [call for call in self.controller.calls if "b" in call[0]]
        slow = [call for call in self.controller.calls if "a" in call[0]]
        self.assertGreaterEqual(len(healthy), 4)
        self.assertEqual(len(slow), 1)
        self.assertEqual(self.window.queued_colors["a"], (165, 20, 10))
        timestamps = [call[3] for call in healthy]
        self.assertTrue(all(b - a >= 0.045 for a, b in zip(timestamps, timestamps[1:])))

    def test_lamp_rate_can_be_changed_and_static_sends_remain_confirmed(self):
        self.assertEqual(self.controller.update_rate, 20)
        self.window.lamp_update_spin.setValue(30)
        self.assertEqual(self.controller.update_rate, 30)
        self.window.send_colors({"a": (100, 20, 10)}, 1)
        self.wait_sends()
        self.assertFalse(self.controller.calls[-1][2]["wait_for_response"])
        self.assertEqual(self.controller.calls[-1][2]["white_mode"], "rgb")
        self.window.send_colors({"b": (100, 100, 100)}, 1, music=False)
        self.wait_sends()
        self.assertTrue(self.controller.calls[-1][2]["wait_for_response"])
        self.assertEqual(self.controller.calls[-1][2]["white_mode"], "auto")

    def test_beat_attack_starts_gently_on_first_ui_frame(self):
        frame = (np.zeros(32), 1.0, True, time.monotonic())
        self.window.audio_worker = SimpleNamespace(take_frame=lambda: frame)
        try:
            self.window.update_visualization()
            self.wait_sends()
            self.assertTrue(self.window.beat_envelope.accepted_onset)
            self.assertTrue(all(0 < level < 0.04 for level in self.window.smooth_levels.values()))
            self.assertEqual(set(self.sent_colors()), {"a", "b"})
        finally:
            self.window.audio_worker = None

    def test_audio_discontinuity_preserves_lamp_levels_for_a_smooth_fade(self):
        self.window.smooth_levels = {"a": 0.7, "b": 0.6}
        self.window.filtered_beat_level(0.8, onset=True)
        self.window.on_audio_discontinuity(1)
        self.assertEqual(self.window.smooth_levels, {"a": 0.7, "b": 0.6})
        self.assertEqual(self.window.beat_envelope.level, 0)
        self.window.audio_worker = SimpleNamespace(take_frame=lambda: (np.zeros(32), 0, False, time.monotonic()))
        try:
            self.window.update_visualization()
            self.wait_sends()
            self.assertGreater(self.window.smooth_levels["a"], 0.67)
            self.assertGreater(self.window.smooth_levels["b"], 0.57)
        finally:
            self.window.audio_worker = None

    def test_capture_recovers_from_discontinuity_without_false_attack(self):
        class GapMicrophone(FakeMicrophone):
            def record(self, numframes):
                data = super().record(numframes)
                if self.frames == 10:
                    warnings.warn("data discontinuity in recording", sc.SoundcardRuntimeWarning)
                return data

        microphone = GapMicrophone()
        worker = AudioCaptureWorker(microphone)
        notices = QtTest.QSignalSpy(worker.discontinuityDetected)
        worker.start()
        try:
            QtTest.QTest.qWait(100)
            self.assertEqual(worker.discontinuities, 1)
            self.assertEqual(len(notices), 1)
            frame = worker.take_frame()
            self.assertFalse(frame[2])
            self.assertEqual(frame[1], 0)
            self.assertGreater(microphone.frames, 12)
        finally:
            worker.requestInterruption()
            self.assertTrue(worker.wait(1000))

    def test_recording_warning_handler_preserves_unrelated_warnings(self):
        class Recorder:
            def record(self, numframes):
                warnings.warn("unrelated warning", UserWarning)
                return np.zeros((441, 2))

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            data, discontinuity = record_available(Recorder())
        self.assertFalse(discontinuity)
        self.assertEqual(data.shape, (441, 2))
        self.assertEqual([str(item.message) for item in caught], ["unrelated warning"])


if __name__ == "__main__":
    unittest.main()
