import asyncio
from pathlib import Path
import sys
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from libs.controller import TuyaLampController


class FakeBulb:
    def __init__(self, device_id="lamp"):
        self.id = device_id
        self.dpset = {"switch": "20"}
        self.calls = []
        self.error = None
        self.active = 0
        self.max_active = 0

    def __getattr__(self, name):
        def command(*args):
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.calls.append((name, args, time.monotonic()))
            time.sleep(0.002)
            self.active -= 1
            return self.error or {"dps": {}}
        return command


class ControllerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bulb = FakeBulb()
        self.controller = TuyaLampController([self.bulb], commands_per_second=10000)

    async def test_music_enters_mode_once_and_deduplicates(self):
        for _ in range(20):
            await self.controller.set_colors({"lamp": (200, 20, 10)})
        self.assertEqual([call[0] for call in self.bulb.calls], ["set_mode", "set_music_colour"])
        await self.controller.set_colors({"lamp": (202, 21, 12)})
        self.assertEqual(len(self.bulb.calls), 2)
        await self.controller.set_colors({"lamp": (205, 21, 12)})
        self.assertEqual(self.bulb.calls[-1][0], "set_music_colour")
        self.assertEqual(len(self.bulb.calls), 3)

    async def test_white_and_static_use_one_command_and_off_is_deduplicated(self):
        await self.controller.set_colors({"lamp": (100, 100, 100)})
        self.assertEqual([call[0] for call in self.bulb.calls], ["set_white_percentage"])
        await self.controller.set_colors({"lamp": (100, 90, 50)}, music=False)
        self.assertEqual(self.bulb.calls[-1][0], "set_colour")
        for _ in range(10):
            await self.controller.set_colors({"lamp": (100, 90, 50)}, music=False)
        self.assertEqual(len(self.bulb.calls), 2)
        for _ in range(10):
            await self.controller.set_colors({"lamp": (0, 0, 0)}, 0)
        self.assertEqual(len(self.bulb.calls), 3)
        self.assertEqual(self.bulb.calls[-1][0:2], ("set_status", (False, "20", False)))
        await self.controller.set_colors({"lamp": (100, 90, 50)}, music=False)
        self.assertEqual(len(self.bulb.calls), 4)

    async def test_mode_changes_and_music_keepalive(self):
        await self.controller.set_colors({"lamp": (100, 90, 50)})
        self.controller.async_devices[0].color_sent_at -= 2
        await self.controller.set_colors({"lamp": (100, 90, 50)})
        self.assertEqual(len(self.bulb.calls), 3)
        await self.controller.set_colors({"lamp": (100, 100, 100)})
        await self.controller.set_colors({"lamp": (100, 90, 50)})
        self.assertEqual([call[0] for call in self.bulb.calls[-3:]],
                         ["set_white_percentage", "set_mode", "set_music_colour"])
        await self.controller.set_mode("music", [])
        self.assertEqual(len(self.bulb.calls), 6)

    async def test_every_command_is_rate_limited(self):
        controller = TuyaLampController([self.bulb], commands_per_second=8)
        for value in (50, 100, 150):
            await controller.set_colors({"lamp": (value, 20, 10)})
        await controller.diagnose_devices()
        await controller.set_colors({"lamp": (0, 0, 0)}, 0)
        times = [call[2] for call in self.bulb.calls]
        self.assertEqual(len(times), 6)
        self.assertTrue(all(b - a >= 0.12 for a, b in zip(times, times[1:])), times)

    async def test_failed_commands_back_off_and_recover(self):
        self.bulb.error = {"Error": "unavailable", "Err": "901"}
        await self.controller.set_colors({"lamp": (100, 20, 10)})
        for _ in range(50):
            await self.controller.set_colors({"lamp": (100, 20, 10)})
        self.assertEqual(len(self.bulb.calls), 1)
        device = self.controller.async_devices[0]
        self.assertIsNone(device.mode)
        self.assertTrue(device.last_error)
        device.retry_after = 0
        self.bulb.error = None
        await self.controller.set_colors({"lamp": (100, 20, 10)})
        self.assertEqual([call[0] for call in self.bulb.calls[-2:]], ["set_mode", "set_music_colour"])
        self.assertEqual(device.last_error, "")

    async def test_diagnostics_and_updates_share_the_same_socket_lock(self):
        await asyncio.gather(self.controller.set_colors({"lamp": (100, 20, 10)}),
                             self.controller.set_colors({"lamp": (200, 20, 10)}),
                             self.controller.diagnose_devices())
        self.assertEqual(self.bulb.max_active, 1)

    async def test_controller_can_be_reused_by_backend_event_loops(self):
        # The web backend uses asyncio.run for successive preset applications.
        controller = self.controller
        await asyncio.to_thread(asyncio.run, controller.set_colors({"lamp": (100, 20, 10)}, music=False))
        await asyncio.to_thread(asyncio.run, controller.set_colors({"lamp": (200, 20, 10)}, music=False))
        self.assertEqual(len(self.bulb.calls), 2)


if __name__ == "__main__":
    unittest.main()
