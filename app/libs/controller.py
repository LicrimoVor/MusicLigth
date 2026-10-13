import asyncio
from functools import partial
import time

import tinytuya


DEFAULT_BULB_MAPPING = {
    "value_min": 10,
    "value_max": 1000,
    "value_hexformat": "hsv16",
}


class AsyncTuyaDevice:
    def __init__(self, device: tinytuya.BulbDevice, commands_per_second=8):
        self.device = device
        self.last_error = ""
        self.lock = asyncio.Lock()
        self.command_interval = 1.0 / max(1.0, float(commands_per_second))
        self.next_command_at = 0.0
        self.retry_after = 0.0
        self.failures = 0
        self.power = None
        self.mode = None
        self.color_state = None
        self.color_sent_at = 0.0

    def invalidate_state(self):
        self.power = None
        self.mode = None
        self.color_state = None

    async def send_command(self, command, *args):
        loop = asyncio.get_running_loop()
        delay = self.next_command_at - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        self.next_command_at = time.monotonic() + self.command_interval
        try:
            result = await loop.run_in_executor(None, partial(getattr(self.device, command), *args))
            error = str(result) if isinstance(result, dict) and (result.get("Error") or result.get("Err")) else ""
        except Exception as exc:
            result, error = None, str(exc)

        self.last_error = error
        if error:
            self.failures += 1
            self.retry_after = time.monotonic() + min(30.0, 2.0 ** min(self.failures, 5))
            self.invalidate_state()
            print(f"Ошибка при выполнении {command} на устройстве {self.device.id}: {error}")
        else:
            self.failures = 0
            self.retry_after = 0.0
        return result

    async def diagnose(self):
        started = time.perf_counter()
        async with self.lock:
            status = await self.send_command("status")
            latency_ms = int((time.perf_counter() - started) * 1000)
            ok = isinstance(status, dict) and not (status.get("Error") or status.get("Err"))
            self.last_error = "" if ok else str(status)
            # A diagnostic may reveal changes made by another controller.
            self.invalidate_state()
            return {
                "id": self.device.id,
                "ip": getattr(self.device, "address", ""),
                "version": getattr(self.device, "version", ""),
                "ok": ok,
                "latency_ms": latency_ms,
                "status": status,
                "error": "" if ok else str(status),
            }


class TuyaLampController:
    def __init__(self, devices, commands_per_second=8, color_tolerance=3):
        self.devices = devices
        self.async_devices = [AsyncTuyaDevice(dev, commands_per_second) for dev in devices]
        self.color_tolerance = max(0, int(color_tolerance))

    async def set_color(self, device, r, g, b, brightness, *, music=True,
                        white_mode="auto", white_temperature=50):
        async with device.lock:
            await self._set_color_locked(device, r, g, b, brightness, music=music,
                                         white_mode=white_mode, white_temperature=white_temperature)

    async def _set_color_locked(self, device, r, g, b, brightness, *, music,
                                white_mode, white_temperature):
        if time.monotonic() < device.retry_after:
            return
        tuya_brightness = brightness * 100 if 0 <= brightness <= 1 else brightness
        switch = device.device.dpset.get("switch") or "20"
        if tuya_brightness <= 0 or (r, g, b) == (0, 0, 0):
            if device.power is not False:
                await device.send_command("set_status", False, switch, False)
                if not device.last_error:
                    device.power = False
                    device.mode = None
                    device.color_state = None
            return

        mode = "white" if r == g == b and white_mode == "auto" else ("music" if music else "colour")
        state = (mode, (r, g, b), white_temperature if mode == "white" else None)
        previous = device.color_state
        if (device.power is True and previous and previous[0] == mode and previous[2] == state[2]
                and max(abs(a - b) for a, b in zip(previous[1], state[1])) <= self.color_tolerance
                and (mode != "music" or time.monotonic() - device.color_sent_at < 1.0)):
            return

        # RGB is already brightness-scaled. Neutral shades use the dedicated white
        # LEDs, avoiding the tinted white produced by mixing RGB in music mode.
        # set_colour/set_white_percentage include power and mode in one command.
        if mode == "white":
            await device.send_command("set_white_percentage", r / 255 * 100, white_temperature, False)
        elif music:
            if device.mode != "music" or device.power is not True:
                await device.send_command("set_mode", "music", False)
                if device.last_error:
                    return
                device.mode, device.power = "music", True
            await device.send_command("set_music_colour", 0, r, g, b, 0, 0, False)
        else:
            await device.send_command("set_colour", r, g, b, False)
        if not device.last_error:
            device.mode, device.power = mode, True
            device.color_state = state
            device.color_sent_at = time.monotonic()

    async def set_colors(self, colors_dict, brightness=1.0, **options):
        tasks = []
        for dev in self.async_devices:
            device_id = dev.device.id
            if device_id in colors_dict:
                r, g, b = colors_dict[device_id]
                tasks.append(self.set_color(dev, r, g, b, brightness, **options))
        await asyncio.gather(*tasks)
        return {dev.device.id: dev.last_error for dev in self.async_devices
                if dev.device.id in colors_dict and dev.last_error}

    async def set_mode(self, mode, device_ids=None):
        allowed = None if device_ids is None else set(device_ids)

        async def update(dev):
            async with dev.lock:
                if time.monotonic() < dev.retry_after or (dev.mode == mode and dev.power is True):
                    return
                await dev.send_command("set_mode", mode)
                if not dev.last_error:
                    dev.mode, dev.power = mode, True
                    dev.color_state = None

        await asyncio.gather(*(update(dev) for dev in self.async_devices
                               if allowed is None or dev.device.id in allowed))

    async def diagnose_devices(self):
        return await asyncio.gather(*(dev.diagnose() for dev in self.async_devices))
