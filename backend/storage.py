from __future__ import annotations

import json
import math
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any

from config import LAMP_CONFIG_PATH, data_dir

HEX_RE = re.compile(r"^#?[0-9a-fA-F]{6}$")


class ValidationError(ValueError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as file:
            loaded = json.load(file)
    except (json.JSONDecodeError, OSError):
        return default
    return loaded if isinstance(loaded, dict) else default


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
        file.write("\n")
    tmp_path.replace(path)


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def normalize_position(value: Any) -> tuple[float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    try:
        return (clamp(float(value[0])), clamp(float(value[1])))
    except (TypeError, ValueError):
        return None


def normalize_hex(value: Any) -> str:
    text = str(value or "").strip()
    if not HEX_RE.match(text):
        raise ValidationError("Некорректный цвет лампы")
    text = text.lower()
    return text if text.startswith("#") else f"#{text}"


def normalize_brightness(value: Any) -> int:
    try:
        return int(clamp(float(value), 0, 100))
    except (TypeError, ValueError):
        raise ValidationError("Некорректная яркость")


def load_lamps(path: Path = LAMP_CONFIG_PATH) -> list[dict[str, Any]]:
    data = read_json(path, {"version": 1, "devices": []})
    devices = data.get("devices", [])
    if not isinstance(devices, list):
        return []

    lamps: list[dict[str, Any]] = []
    missing_position_indexes: list[int] = []
    for index, device in enumerate(devices, 1):
        if not isinstance(device, dict):
            continue
        lamp_id = str(device.get("id") or "").strip()
        if not lamp_id:
            continue

        position = normalize_position(device.get("position"))
        if position is None:
            missing_position_indexes.append(len(lamps))

        lamps.append(
            {
                "id": lamp_id,
                "label": str(device.get("name") or f"Лампа {index}"),
                "short_id": lamp_id[-6:],
                "ip": str(device.get("ip") or ""),
                "position": position,
                "enabled": bool(device.get("enabled", True)),
            }
        )

    total_missing = len(missing_position_indexes)
    for order, lamp_index in enumerate(missing_position_indexes):
        angle = (math.tau * order / max(1, total_missing)) - math.pi / 2
        lamps[lamp_index]["position"] = (
            0.5 + math.cos(angle) * 0.42,
            0.5 + math.sin(angle) * 0.42,
        )

    return lamps


class PresetStore:
    def __init__(self, presets_path: Path | None = None, state_path: Path | None = None):
        base_dir = data_dir()
        self.presets_path = presets_path or (base_dir / "presets.json")
        self.state_path = state_path or (base_dir / "state.json")
        self._lock = RLock()

    def list_lamps(self) -> list[dict[str, Any]]:
        return load_lamps()

    def list_presets(self) -> list[dict[str, Any]]:
        with self._lock:
            data = self._read_presets_locked()
            lamps = self.list_lamps()
            presets = [self._normalize_preset(preset, lamps) for preset in data.get("presets", [])]
            data["presets"] = presets
            write_json(self.presets_path, data)
            return presets

    def get_preset(self, preset_id: str) -> dict[str, Any] | None:
        return next((preset for preset in self.list_presets() if preset["id"] == preset_id), None)

    def create_preset(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            data = self._read_presets_locked()
            lamps = self.list_lamps()
            preset = self._normalize_preset(payload, lamps, is_new=True)
            data.setdefault("presets", []).insert(0, preset)
            write_json(self.presets_path, data)
            return preset

    def update_preset(self, preset_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        with self._lock:
            data = self._read_presets_locked()
            presets = data.setdefault("presets", [])
            lamps = self.list_lamps()
            for index, existing in enumerate(presets):
                if existing.get("id") != preset_id:
                    continue
                merged = dict(existing)
                merged.update(payload)
                merged["id"] = preset_id
                merged["created_at"] = existing.get("created_at") or utc_now()
                merged["updated_at"] = utc_now()
                presets[index] = self._normalize_preset(merged, lamps)
                write_json(self.presets_path, data)
                return presets[index]
        return None

    def delete_preset(self, preset_id: str) -> bool:
        with self._lock:
            data = self._read_presets_locked()
            presets = data.setdefault("presets", [])
            kept = [preset for preset in presets if preset.get("id") != preset_id]
            if len(kept) == len(presets):
                return False
            data["presets"] = kept
            write_json(self.presets_path, data)
            state = self.get_runtime_state()
            if state.get("current_preset_id") == preset_id:
                self.set_current_preset(None, None)
            return True

    def get_runtime_state(self) -> dict[str, Any]:
        with self._lock:
            state = read_json(self.state_path, self._default_runtime_state())
            return {**self._default_runtime_state(), **state}

    def set_current_preset(
        self, preset_id: str | None, result: dict[str, Any] | None
    ) -> dict[str, Any]:
        with self._lock:
            state = self.get_runtime_state()
            state.update(
                {
                    "current_preset_id": preset_id,
                    "last_applied_at": utc_now() if preset_id else None,
                    "last_result": result,
                }
            )
            write_json(self.state_path, state)
            return state

    def set_diagnostics_running(self, running: bool) -> dict[str, Any]:
        with self._lock:
            state = self.get_runtime_state()
            state["diagnostics_running"] = running
            write_json(self.state_path, state)
            return state

    def set_diagnostics(self, result: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            state = self.get_runtime_state()
            state.update(
                {
                    "last_diagnostics_at": utc_now(),
                    "last_diagnostics_result": result,
                    "diagnostics_running": False,
                }
            )
            write_json(self.state_path, state)
            return state

    def _default_runtime_state(self) -> dict[str, Any]:
        return {
            "current_preset_id": None,
            "last_applied_at": None,
            "last_result": None,
            "last_diagnostics_at": None,
            "last_diagnostics_result": None,
            "diagnostics_running": False,
        }

    def _read_presets_locked(self) -> dict[str, Any]:
        data = read_json(self.presets_path, {"version": 1, "presets": []})
        if not isinstance(data.get("presets"), list):
            data["presets"] = []
        if not data["presets"]:
            data["presets"] = self._default_presets(self.list_lamps())
            write_json(self.presets_path, data)
        return data

    def _normalize_preset(
        self,
        payload: dict[str, Any],
        lamps: list[dict[str, Any]],
        is_new: bool = False,
    ) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValidationError("Некорректный пресет")

        now = utc_now()
        name = str(payload.get("name") or "").strip()
        if not name:
            raise ValidationError("Название пресета обязательно")
        if len(name) > 80:
            raise ValidationError("Название пресета слишком длинное")

        known_lamps = {lamp["id"] for lamp in lamps}
        payload_colors = payload.get("colors") or {}
        if not isinstance(payload_colors, dict):
            raise ValidationError("Некорректные цвета пресета")

        unknown_lamps = [lamp_id for lamp_id in payload_colors if lamp_id not in known_lamps]
        if unknown_lamps:
            raise ValidationError("Пресет содержит неизвестные лампы")

        colors: dict[str, str] = {}
        for lamp_id, value in payload_colors.items():
            colors[lamp_id] = normalize_hex(value)

        if not colors:
            raise ValidationError("В пресете должна участвовать хотя бы одна лампа")

        effect = str(payload.get("effect") or "static").strip().lower()
        if effect not in {"static", "fire", "pulse", "wave"}:
            effect = "static"

        return {
            "id": str(payload.get("id") or uuid.uuid4()),
            "name": name,
            "description": str(payload.get("description") or "").strip()[:240],
            "brightness": normalize_brightness(payload.get("brightness", 100)),
            "effect": effect,
            "colors": colors,
            "created_at": str(payload.get("created_at") or now),
            "updated_at": now if is_new else str(payload.get("updated_at") or now),
        }

    def _default_presets(self, lamps: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not lamps:
            return []

        def filled(color: str) -> dict[str, str]:
            return {lamp["id"]: color for lamp in lamps}

        warm = filled("#ff9f43")
        night = filled("#2446ff")
        fire_palette = ["#ff3b1f", "#ff6f1f", "#ffb000", "#ff4d00", "#ffd166"]
        fire = {
            lamp["id"]: fire_palette[index % len(fire_palette)] for index, lamp in enumerate(lamps)
        }

        return [
            self._normalize_preset(
                {
                    "name": "Теплый свет",
                    "description": "",
                    "brightness": 75,
                    "effect": "static",
                    "colors": warm,
                },
                lamps,
                is_new=True,
            ),
            self._normalize_preset(
                {
                    "name": "Огонь",
                    "description": "",
                    "brightness": 90,
                    "effect": "fire",
                    "colors": fire,
                },
                lamps,
                is_new=True,
            ),
            self._normalize_preset(
                {
                    "name": "Ночной",
                    "description": "",
                    "brightness": 35,
                    "effect": "static",
                    "colors": night,
                },
                lamps,
                is_new=True,
            ),
        ]
