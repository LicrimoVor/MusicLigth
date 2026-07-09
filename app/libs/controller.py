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
    def __init__(self, device: tinytuya.BulbDevice):
        self.device = device
        self.last_error = ""

    async def send_command(self, command, *args):
        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(None, partial(getattr(self.device, command), *args))
            if isinstance(result, dict) and result.get("Error"):
                self.last_error = str(result)
                print(f"Ошибка при выполнении {command} на устройстве {self.device.id}: {result}")
                return result
            self.last_error = ""
            return result
        except Exception as exc:
            self.last_error = str(exc)
            print(f"Ошибка при выполнении {command} на устройстве {self.device.id}: {exc}")
            return None

    async def diagnose(self):
        loop = asyncio.get_running_loop()
        started = time.perf_counter()

        try:
            status = await loop.run_in_executor(None, self.device.status)
            latency_ms = int((time.perf_counter() - started) * 1000)
            ok = isinstance(status, dict) and not status.get("Error")
            self.last_error = "" if ok else str(status)
            return {
                "id": self.device.id,
                "ip": getattr(self.device, "address", ""),
                "version": getattr(self.device, "version", ""),
                "ok": ok,
                "latency_ms": latency_ms,
                "status": status,
                "error": "" if ok else str(status),
            }
        except Exception as exc:
            latency_ms = int((time.perf_counter() - started) * 1000)
            self.last_error = str(exc)
            return {
                "id": self.device.id,
                "ip": getattr(self.device, "address", ""),
                "version": getattr(self.device, "version", ""),
                "ok": False,
                "latency_ms": latency_ms,
                "status": None,
                "error": str(exc),
            }


class TuyaLampController:
    def __init__(self, devices):
        self.devices = devices
        self.async_devices = [AsyncTuyaDevice(dev) for dev in devices]

    async def set_color(self, device, r, g, b, brightness):
        tuya_brightness = brightness * 100 if 0 <= brightness <= 1 else brightness
        switch = device.device.dpset.get("switch") or "20"
        if tuya_brightness <= 0 or (r, g, b) == (0, 0, 0):
            await device.send_command("set_status", False, switch, False)
            return

        await device.send_command("set_status", True, switch, True)
        # In music mode this brightness argument drives the white channel; RGB is already scaled by callers.
        await device.send_command("set_music_colour", 0, r, g, b, 0, 0, True)
        if "Bulb not configured" in device.last_error:
            device.device.set_bulb_type("B", mapping=DEFAULT_BULB_MAPPING)
            await device.send_command("set_music_colour", 0, r, g, b, 0, 0, True)

    async def set_colors(self, colors_dict, brightness=1.0):
        tasks = []
        for dev in self.async_devices:
            device_id = dev.device.id
            if device_id in colors_dict:
                r, g, b = colors_dict[device_id]
                tasks.append(self.set_color(dev, r, g, b, brightness))
        await asyncio.gather(*tasks)

    async def set_mode(self, mode, device_ids=None):
        allowed = set(device_ids or [])
        tasks = []
        for dev in self.async_devices:
            if allowed and dev.device.id not in allowed:
                continue
            tasks.append(dev.send_command("set_mode", mode))
        await asyncio.gather(*tasks)

    async def diagnose_devices(self):
        return await asyncio.gather(*(dev.diagnose() for dev in self.async_devices))
