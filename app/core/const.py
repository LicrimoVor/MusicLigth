from __future__ import annotations

import json
from pathlib import Path


BASE_PATH = Path(__file__).parent.parent
CORE_PATH = BASE_PATH / "core"
LAMP_CONFIG_PATH = CORE_PATH / "lamps.json"


def _position(value):
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    return (float(value[0]), float(value[1]))


def load_lamp_config(path: Path = LAMP_CONFIG_PATH) -> dict:
    with path.open("r", encoding="utf-8") as file:
        data = json.load(file)

    devices = []
    for device in data.get("devices", []):
        device_id = device.get("id")
        if not device_id:
            continue
        normalized = dict(device)
        normalized["position"] = _position(device.get("position"))
        normalized["version"] = str(device.get("version") or device.get("ver") or "3.5")
        normalized["local_key"] = device.get("local_key") or device.get("key") or ""
        normalized["enabled"] = bool(device.get("enabled", True))
        devices.append(normalized)

    data["devices"] = devices
    return data


LAMP_CONFIG = load_lamp_config()
LAMP_DEVICES = LAMP_CONFIG["devices"]

DEVICE_IP = {
    device["id"]: device["ip"]
    for device in LAMP_DEVICES
    if device.get("ip")
}

DEVICE_LOCAL = {
    device["id"]: device["local_key"]
    for device in LAMP_DEVICES
    if device.get("local_key")
}

DEVICE_VERSION = {
    device["id"]: device["version"]
    for device in LAMP_DEVICES
}

LAMP_POSITION = {
    device["id"]: device["position"]
    for device in LAMP_DEVICES
    if device.get("position") is not None
}


def get_lamp_devices(enabled_only: bool = False) -> list[dict]:
    devices = LAMP_DEVICES
    if enabled_only:
        devices = [device for device in devices if device.get("enabled", True)]
    return [dict(device) for device in devices]
