import tinytuya
from core.const import DEVICE_IP, DEVICE_LOCAL


def init_lamp():
    devices: list[tinytuya.BulbDevice] = []
    for device_id, ip in DEVICE_IP.items():
        local_key = DEVICE_LOCAL.get(device_id)
        device = tinytuya.BulbDevice(device_id, ip, local_key)
        device.set_version(3.5)

        # Инициализация устройства
        print(device.status(), local_key)

        # Теперь можно включать режим музыки
        device.set_mode("music")
        device.set_music_colour(0, 0, 0, 0, 0, None, True)
        device.set_socketPersistent(True)
        devices.append(device)

    return devices
