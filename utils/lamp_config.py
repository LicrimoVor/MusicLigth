from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Optional


BASE_PATH = Path(__file__).resolve().parents[1]
LAMP_CONFIG_PATH = BASE_PATH / "core" / "lamps.json"


def load_json(path: Path) -> dict:
    if not path.exists():
        return {"version": 1, "devices": []}
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def write_json(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
        file.write("\n")


def extract_scan_devices(scan_result) -> list[dict]:
    if isinstance(scan_result, list):
        return [device for device in scan_result if isinstance(device, dict)]

    if not isinstance(scan_result, dict):
        return []

    devices = scan_result.get("devices")
    if isinstance(devices, list):
        return [device for device in devices if isinstance(device, dict)]

    return [device for device in scan_result.values() if isinstance(device, dict)]


def normalize_scan_device(device: dict) -> Optional[dict]:
    device_id = device.get("id") or device.get("gwId") or device.get("devId")
    ip = device.get("ip")
    if not device_id or not ip:
        return None

    return {
        "id": device_id,
        "ip": ip,
        "version": str(device.get("version") or device.get("ver") or "3.5"),
    }


def merge_lamp_config(scanned_devices: list[dict], existing_config: dict, keep_missing=True) -> dict:
    existing_by_id = {
        device["id"]: device
        for device in existing_config.get("devices", [])
        if isinstance(device, dict) and device.get("id")
    }
    scanned_by_id = {
        device["id"]: device
        for device in scanned_devices
        if isinstance(device, dict) and device.get("id")
    }

    merged = []
    for device_id, scanned in scanned_by_id.items():
        existing = existing_by_id.get(device_id, {})
        merged.append(
            {
                "id": device_id,
                "ip": scanned.get("ip", existing.get("ip", "")),
                "local_key": existing.get("local_key") or existing.get("key") or "",
                "version": str(scanned.get("version") or existing.get("version") or "3.5"),
                "position": existing.get("position"),
                "enabled": bool(existing.get("enabled", True)),
            }
        )

    if keep_missing:
        missing = [device for device_id, device in existing_by_id.items() if device_id not in scanned_by_id]
        merged.extend(missing)

    return {
        "version": int(existing_config.get("version", 1)),
        "devices": sorted(merged, key=lambda device: (device.get("ip") or "", device["id"])),
    }


def scan_devices() -> list[dict]:
    import tinytuya

    raw_scan = tinytuya.scan()
    devices = []
    for device in extract_scan_devices(raw_scan):
        normalized = normalize_scan_device(device)
        if normalized:
            devices.append(normalized)
    return devices


def build_config_from_scan(output_path: Path, existing_path: Optional[Path] = None, keep_missing=True) -> dict:
    existing_path = existing_path or output_path
    existing_config = load_json(existing_path)
    scanned_devices = scan_devices()
    config = merge_lamp_config(scanned_devices, existing_config, keep_missing=keep_missing)
    write_json(output_path, config)
    return config


def build_config_from_snapshot(
    snapshot_path: Path,
    output_path: Path,
    existing_path: Optional[Path] = None,
    keep_missing=True,
) -> dict:
    existing_path = existing_path or output_path
    existing_config = load_json(existing_path)
    snapshot = load_json(snapshot_path)
    scanned_devices = [
        normalized
        for normalized in (normalize_scan_device(device) for device in extract_scan_devices(snapshot))
        if normalized
    ]
    config = merge_lamp_config(scanned_devices, existing_config, keep_missing=keep_missing)
    write_json(output_path, config)
    return config


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Create or update core/lamps.json from tinytuya scan.")
    parser.add_argument(
        "--output",
        type=Path,
        default=LAMP_CONFIG_PATH,
        help="Path to write lamp config JSON.",
    )
    parser.add_argument(
        "--existing",
        type=Path,
        default=None,
        help="Existing config to preserve local_key, position and enabled fields.",
    )
    parser.add_argument(
        "--snapshot",
        type=Path,
        default=None,
        help="Use an existing tinytuya snapshot JSON instead of running a network scan.",
    )
    parser.add_argument(
        "--drop-missing",
        action="store_true",
        help="Do not keep devices that are present in existing config but missing from scan.",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    keep_missing = not args.drop_missing
    if args.snapshot:
        config = build_config_from_snapshot(args.snapshot, args.output, args.existing, keep_missing)
    else:
        config = build_config_from_scan(args.output, args.existing, keep_missing)
    print(f"Wrote {len(config['devices'])} devices to {args.output}")


if __name__ == "__main__":
    main()
