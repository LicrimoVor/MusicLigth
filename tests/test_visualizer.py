import asyncio
from concurrent.futures import Future
import os
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

import numpy as np
from PyQt5 import QtCore, QtGui, QtTest, QtWidgets

from model.audio import FRAME_SIZE, SAMPLE_RATE
from model.audio_stream import AudioCaptureWorker
from model.settings import normalized_settings
from ui.AudioVisualizer import AudioVisualizer
from ui.widgets import BeatGraphWidget, SpectrumWidget


class FakeMicrophone:
    name = "Test audio"

    def __init__(self):
        self.opens = 0
        self.frames = 0
        self.closed = False

    def recorder(self, **options):
        self.opens += 1
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def record(self, numframes):
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

    async def set_colors(self, colors, brightness, **options):
        self.calls.append((dict(colors), brightness, options, time.monotonic()))


class VisualizerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        # The Windows offscreen platform has no system font database.
        if sys.platform == "win32":
            QtGui.QFontDatabase.addApplicationFont("C:/Windows/Fonts/segoeui.ttf")
            cls.app.setFont(QtGui.QFont("Segoe UI", 10))

    def setUp(self):
        self.controller = FakeController()
        self.loop = asyncio.new_event_loop()
        self.loop_thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.loop_thread.start()
        with patch("ui.AudioVisualizer.sc.all_microphones", return_value=[]), \
                patch("ui.AudioVisualizer.load_settings", return_value=normalized_settings({})):
            self.window = AudioVisualizer(self.controller, self.loop)
        self.window.save_current_settings = lambda: None

    def tearDown(self):
        self.window.stop_stream(send_to_lamps=False)
        if self.window.audio_worker:
            self.window.audio_worker.wait(1000)
        self.window.queued_colors.clear()
        if self.window.pending_lamp_update:
            self.window.pending_lamp_update.result(timeout=2)
        self.window.close()
        self.app.processEvents()
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.loop_thread.join(timeout=2)
        self.loop.close()

    def test_stop_is_queued_while_previous_send_is_pending(self):
        pending = Future()
        self.window.pending_lamp_update = pending
        self.window.send_colors({"a": (200, 30, 10)}, 1)
        self.window.stop_stream()
        self.assertEqual(self.controller.calls, [])
        self.assertEqual(self.window.queued_colors, {"a": (0, 0, 0), "b": (0, 0, 0)})
        pending.set_result(None)
        self.window.flush_lamp_updates()
        self.window.pending_lamp_update.result(timeout=2)
        self.assertEqual(self.controller.calls[-1][0], {"a": (0, 0, 0), "b": (0, 0, 0)})

    def test_latest_color_wins_and_disabling_lamp_preserves_other_updates(self):
        pending = Future()
        self.window.pending_lamp_update = pending
        self.window.send_colors({"a": (100, 30, 10), "b": (90, 20, 10)}, 0.5)
        self.window.send_colors({"a": (200, 30, 10)}, 0.7)
        self.window.set_lamp_enabled("b", False)
        pending.set_result(None)
        self.window.flush_lamp_updates()
        self.window.pending_lamp_update.result(timeout=2)
        colors, brightness, _, _ = self.controller.calls[-1]
        self.assertEqual(colors, {"a": (200, 30, 10), "b": (0, 0, 0)})
        self.assertGreater(brightness, 0)

    def test_color_updates_do_not_follow_ui_frame_rate(self):
        for value in range(20):
            self.window.send_colors({"a": (value * 10, 30, 10)}, 1)
            if self.window.pending_lamp_update:
                self.window.pending_lamp_update.result(timeout=2)
        self.assertEqual(len(self.controller.calls), 1)
        self.assertEqual(self.window.queued_colors["a"], (190, 30, 10))
        self.window.last_lamp_send -= 0.21
        self.window.flush_lamp_updates()
        self.window.pending_lamp_update.result(timeout=2)
        self.assertEqual(self.controller.calls[-1][0]["a"], (190, 30, 10))

    def test_failed_static_update_retries_latest_requested_color(self):
        pending = Future()
        self.window.pending_lamp_update = pending
        self.window.inflight_colors = {"a": (100, 30, 10)}
        self.window.inflight_music = False
        pending.set_result({"a": "unavailable"})
        self.window.send_colors({"a": (200, 30, 10)}, 1, music=False)
        self.window.pending_lamp_update.result(timeout=2)
        self.assertEqual(self.controller.calls[-1][0], {"a": (200, 30, 10)})
        self.assertFalse(self.controller.calls[-1][2]["music"])

    def test_close_waits_for_queued_off_without_blocking_event_loop(self):
        pending = Future()
        self.window.pending_lamp_update = pending
        self.window.timer.start()
        event = QtGui.QCloseEvent()
        self.window.closeEvent(event)
        self.assertFalse(event.isAccepted())
        self.assertEqual(self.window.queued_colors, {"a": (0, 0, 0), "b": (0, 0, 0)})
        pending.set_result(None)
        self.window.flush_lamp_updates()
        self.window.pending_lamp_update.result(timeout=2)
        event = QtGui.QCloseEvent()
        self.window.closeEvent(event)
        self.assertTrue(event.isAccepted())
        self.assertEqual(self.controller.calls[-1][0], {"a": (0, 0, 0), "b": (0, 0, 0)})

    def test_static_mode_starts_without_microphone_and_sends_once(self):
        self.window.mode_combo.setCurrentIndex(self.window.mode_combo.findData("static"))
        self.window.start_stream()
        self.assertTrue(self.window.timer.isActive())
        self.assertIsNone(self.window.audio_worker)
        self.window.pending_lamp_update.result(timeout=2)
        for _ in range(20):
            self.window.update_visualization()
        self.assertEqual(len(self.controller.calls), 1)
        self.assertFalse(self.controller.calls[0][2]["music"])

    def test_capture_uses_one_recorder_and_preserves_short_attacks(self):
        microphone = FakeMicrophone()
        worker = AudioCaptureWorker(microphone)
        worker.start()
        try:
            QtTest.QTest.qWait(100)
            frame = worker.take_frame()
            self.assertIsNotNone(frame)
            self.assertTrue(frame[2])
            self.assertGreater(frame[1], 0.5)
            self.assertEqual(microphone.opens, 1)
            self.assertGreater(microphone.frames, 10)
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


if __name__ == "__main__":
    unittest.main()
