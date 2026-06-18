import asyncio
from functools import partial
import tinytuya


class AsyncTuyaDevice:
    def __init__(self, device: tinytuya.BulbDevice):
        self.device = device

    async def send_command(self, command, *args):
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, partial(getattr(self.device, command), *args))
        except Exception as e:
            print(f"Ошибка при выполнении {command} на устройстве {self.device.id}: {e}")


class TuyaLampController:
    def __init__(self, devices):
        self.devices = devices
        self.async_devices = [AsyncTuyaDevice(dev) for dev in devices]

    async def set_color(self, device, r, g, b, brightness):
        await device.send_command("set_music_colour", 0, r, g, b, brightness, None, True)

    async def set_colors(self, colors_dict, brightness):
        tasks = []
        for dev in self.async_devices:
            device_id = dev.device.id
            if device_id in colors_dict:
                r, g, b = colors_dict[device_id]
                tasks.append(self.set_color(dev, r, g, b, brightness))
            # else:
            #     print(f"Цвет не задан для устройства {device_id}")
        await asyncio.gather(*tasks)


if __name__ == "__main__":
    DEVICE_LOCAL = {
        "bf7d83acbfbab53cd44ign": "1vjT&upb7d2-2y?+",
        "bfe54d10d2e3ade05dtln0": "z5+_vK9~p!@S/Vq6",
        "bf8848fb5d7b7a568drlsq": "sn-;71NM?F6VXoRf",
        "bff31a95d7f1a6f2d4qtyx": "7-cDK51:&+&gO]rL",
        "bfe331579ebc27528cy2ax": ">gVA==q!@4!nOA(s",
        "bf5e44965ed19fd2c4ppel": "-ubJ0R?A|}Zn>sl=",
        "bf49dd04e681068f35izf9": "Yi:i~C#MAo8;VES;",
        "bf38fd090103ffb48cjkwy": "(kVkTWn|3'<+9lfn",
        "bfa81b616f3eb626de6gja": "4!7]]rC}Z49?ocq)",
        "bfbd4cb669009901e7p2fh": "<A_{R/Lvvui6qvOi",
    }

    DEVICE_IP = {
        "bfa81b616f3eb626de6gja": "192.168.1.33",
        "bfe54d10d2e3ade05dtln0": "192.168.1.34",
        "bf38fd090103ffb48cjkwy": "192.168.1.35",
        "bf7d83acbfbab53cd44ign": "192.168.1.36",
        "bf49dd04e681068f35izf9": "192.168.1.37",
        "bf5e44965ed19fd2c4ppel": "192.168.1.38",
        "bfbd4cb669009901e7p2fh": "192.168.1.39",
        "bfe331579ebc27528cy2ax": "192.168.1.40",
        "bf8848fb5d7b7a568drlsq": "192.168.1.41",
        "bff31a95d7f1a6f2d4qtyx": "192.168.1.42",
    }

    devices = []
    for device_id, ip in DEVICE_IP.items():
        local_key = DEVICE_LOCAL.get(device_id)
        device = tinytuya.BulbDevice(device_id, ip, local_key)
        device.set_version(3.5)
        device.set_socketPersistent(True)
        devices.append(device)

    controller = TuyaLampController(devices)

    async def test_colors():
        while True:
            # Красный
            colors = {dev.device.id: (255, 0, 0) for dev in controller.async_devices}
            await controller.set_colors(colors)
            await asyncio.sleep(1)
            # Зеленый
            colors = {dev.device.id: (0, 255, 0) for dev in controller.async_devices}
            await controller.set_colors(colors)
            await asyncio.sleep(1)
            # Синий
            colors = {dev.device.id: (0, 0, 255) for dev in controller.async_devices}
            await controller.set_colors(colors)
            await asyncio.sleep(1)

    asyncio.run(test_colors())
