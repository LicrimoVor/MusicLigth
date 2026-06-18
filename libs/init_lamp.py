import tinytuya

from core.const import get_lamp_devices


def init_lamp():
    devices: list[tinytuya.BulbDevice] = []
    for lamp in get_lamp_devices(enabled_only=True):
        device_id = lamp["id"]
        ip = lamp.get("ip")
        local_key = lamp.get("local_key")
        if not ip or not local_key:
            continue

        device = tinytuya.BulbDevice(device_id, ip, local_key)
        device.set_version(float(lamp.get("version", "3.5")))
        device.set_socketPersistent(True)
        devices.append(device)

    return devices
