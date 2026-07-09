from __future__ import annotations

import asyncio
import math
import random
import sys
import time
from threading import Event, RLock, Thread
from typing import Any

from config import APP_DIR


def hex_to_rgb(value: str) -> tuple[int, int, int]:
    text = value.strip().lstrip("#")
    return (int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16))


def clamp_channel(value: float) -> int:
    return max(0, min(255, int(value)))


def scale_rgb(color: tuple[int, int, int], factor: float) -> tuple[int, int, int]:
    return tuple(clamp_channel(channel * factor) for channel in color)


class LampRuntime:
    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run
        self._controller = None
        self._init_error = ""
        self._lock = RLock()
        self._send_lock = RLock()
        self._animation_lock = RLock()
        self._animation_stop: Event | None = None
        self._animation_thread: Thread | None = None
        self._animation_effect = ""

    def status(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "ready": self.dry_run or self._controller is not None,
            "init_error": self._init_error,
            "animation": {
                "running": bool(self._animation_thread and self._animation_thread.is_alive()),
                "effect": self._animation_effect,
            },
        }

    def apply_preset(self, preset: dict[str, Any], lamps: list[dict[str, Any]]) -> dict[str, Any]:
        self.stop_animation()
        brightness_ratio = max(0.0, min(1.0, float(preset.get("brightness", 100)) / 100))
        enabled_lamp_ids = [lamp["id"] for lamp in lamps if lamp.get("enabled", True)]
        enabled_lamps = set(enabled_lamp_ids)
        colors = {}
        for lamp_id, hex_value in preset.get("colors", {}).items():
            if lamp_id not in enabled_lamps:
                continue
            rgb = hex_to_rgb(str(hex_value))
            colors[lamp_id] = tuple(int(channel * brightness_ratio) for channel in rgb)

        if not colors:
            return {
                "ok": False,
                "dry_run": self.dry_run,
                "lamp_count": 0,
                "brightness": preset.get("brightness", 100),
                "effect": preset.get("effect", "static"),
                "animation": False,
                "error": "Preset has no enabled lamps",
            }

        command_colors = {lamp_id: colors.get(lamp_id, (0, 0, 0)) for lamp_id in enabled_lamp_ids}
        off_lamp_count = len(command_colors) - len(colors)
        effect = str(preset.get("effect") or "static").lower()
        if effect in {"fire", "pulse", "wave"}:
            return self._apply_animated(
                effect,
                colors,
                command_colors,
                brightness_ratio,
                lamps,
                preset,
                active_lamp_count=len(colors),
                off_lamp_count=off_lamp_count,
            )

        return self._send_colors(
            command_colors,
            brightness_ratio,
            preset,
            active_lamp_count=len(colors),
            off_lamp_count=off_lamp_count,
        )

    def stop_animation(self) -> None:
        with self._animation_lock:
            stop_event = self._animation_stop
            thread = self._animation_thread
            self._animation_stop = None
            self._animation_thread = None
            self._animation_effect = ""

        if stop_event:
            stop_event.set()
        if thread and thread.is_alive():
            thread.join(timeout=2)

    def _send_colors(
        self,
        colors: dict[str, tuple[int, int, int]],
        brightness_ratio: float,
        preset: dict[str, Any],
        active_lamp_count: int | None = None,
        off_lamp_count: int = 0,
    ) -> dict[str, Any]:
        active_lamp_count = len(colors) if active_lamp_count is None else active_lamp_count
        if self.dry_run:
            return {
                "ok": True,
                "dry_run": True,
                "lamp_count": active_lamp_count,
                "command_lamp_count": len(colors),
                "off_lamp_count": off_lamp_count,
                "brightness": preset.get("brightness", 100),
                "effect": preset.get("effect", "static"),
                "animation": False,
                "error": "",
            }

        controller = self._get_controller()
        if controller is None:
            return {
                "ok": False,
                "dry_run": False,
                "lamp_count": active_lamp_count,
                "command_lamp_count": len(colors),
                "off_lamp_count": off_lamp_count,
                "brightness": preset.get("brightness", 100),
                "effect": preset.get("effect", "static"),
                "animation": False,
                "error": self._init_error or "Lamp controller is not initialized",
            }

        try:
            with self._send_lock:
                asyncio.run(controller.set_colors(colors, brightness_ratio))
        except Exception as exc:
            return {
                "ok": False,
                "dry_run": False,
                "lamp_count": active_lamp_count,
                "command_lamp_count": len(colors),
                "off_lamp_count": off_lamp_count,
                "brightness": preset.get("brightness", 100),
                "effect": preset.get("effect", "static"),
                "animation": False,
                "error": str(exc),
            }

        errors = [
            {"id": device.device.id, "error": device.last_error}
            for device in controller.async_devices
            if getattr(device, "last_error", "")
        ]
        return {
            "ok": not errors,
            "dry_run": False,
            "lamp_count": active_lamp_count,
            "command_lamp_count": len(colors),
            "off_lamp_count": off_lamp_count,
            "brightness": preset.get("brightness", 100),
            "effect": preset.get("effect", "static"),
            "animation": False,
            "errors": errors,
            "error": "" if not errors else "Some lamps returned errors",
        }

    def _apply_animated(
        self,
        effect: str,
        active_colors: dict[str, tuple[int, int, int]],
        startup_colors: dict[str, tuple[int, int, int]],
        brightness_ratio: float,
        lamps: list[dict[str, Any]],
        preset: dict[str, Any],
        active_lamp_count: int | None = None,
        off_lamp_count: int = 0,
    ) -> dict[str, Any]:
        first_frame = dict(startup_colors)
        first_frame.update(self._animation_frame(effect, active_colors, lamps, 0))
        result = self._send_colors(
            first_frame,
            brightness_ratio,
            preset,
            active_lamp_count=active_lamp_count,
            off_lamp_count=off_lamp_count,
        )
        result["animation"] = bool(result.get("ok"))
        result["effect"] = effect

        if not result.get("ok") or self.dry_run:
            return result

        stop_event = Event()
        thread = Thread(
            target=self._animation_loop,
            args=(
                stop_event,
                effect,
                active_colors,
                brightness_ratio,
                lamps,
                preset,
                active_lamp_count,
            ),
            daemon=True,
        )
        with self._animation_lock:
            self._animation_stop = stop_event
            self._animation_thread = thread
            self._animation_effect = effect
        thread.start()
        return result

    def _animation_loop(
        self,
        stop_event: Event,
        effect: str,
        base_colors: dict[str, tuple[int, int, int]],
        brightness_ratio: float,
        lamps: list[dict[str, Any]],
        preset: dict[str, Any],
        active_lamp_count: int | None,
    ) -> None:
        started = time.perf_counter()
        interval = 0.18 if effect == "fire" else 0.24
        while not stop_event.wait(interval):
            elapsed = time.perf_counter() - started
            frame = self._animation_frame(effect, base_colors, lamps, elapsed)
            self._send_colors(
                frame,
                brightness_ratio,
                preset,
                active_lamp_count=active_lamp_count,
                off_lamp_count=0,
            )

    def _animation_frame(
        self,
        effect: str,
        base_colors: dict[str, tuple[int, int, int]],
        lamps: list[dict[str, Any]],
        elapsed: float,
    ) -> dict[str, tuple[int, int, int]]:
        lamp_positions = {lamp["id"]: lamp.get("position") or (0.5, 0.5) for lamp in lamps}

        if effect == "pulse":
            factor = 0.55 + 0.45 * ((math.sin(elapsed * 3.0) + 1) / 2)
            return {lamp_id: scale_rgb(color, factor) for lamp_id, color in base_colors.items()}

        if effect == "wave":
            frame = {}
            for index, (lamp_id, color) in enumerate(base_colors.items()):
                x, y = lamp_positions.get(lamp_id, (0.5, 0.5))
                phase = (x * 3.0 + y * 2.0 + index * 0.35) * math.pi
                factor = 0.38 + 0.62 * ((math.sin(elapsed * 2.4 + phase) + 1) / 2)
                frame[lamp_id] = scale_rgb(color, factor)
            return frame

        if effect == "fire":
            frame = {}
            for lamp_id, color in base_colors.items():
                if max(color) <= 0:
                    frame[lamp_id] = color
                    continue
                flicker = random.uniform(0.62, 1.12)
                heat = random.uniform(0.0, 1.0)
                frame[lamp_id] = (
                    clamp_channel(color[0] * flicker + 24 * heat),
                    clamp_channel(color[1] * random.uniform(0.45, 0.95)),
                    clamp_channel(color[2] * random.uniform(0.12, 0.42)),
                )
            return frame

        return base_colors

    def diagnose(self, lamps: list[dict[str, Any]]) -> dict[str, Any]:
        enabled_lamps = [lamp for lamp in lamps if lamp.get("enabled", True)]
        if self.dry_run:
            rows = [
                {
                    "id": lamp["id"],
                    "label": lamp.get("label", ""),
                    "ip": lamp.get("ip", ""),
                    "enabled": lamp.get("enabled", True),
                    "ok": None,
                    "latency_ms": None,
                    "status": "dry-run",
                    "error": "",
                }
                for lamp in lamps
            ]
            return {
                "ok": True,
                "dry_run": True,
                "lamp_count": len(enabled_lamps),
                "ok_count": 0,
                "failed_count": 0,
                "rows": rows,
                "error": "",
            }

        controller = self._get_controller()
        if controller is None:
            rows = [
                {
                    "id": lamp["id"],
                    "label": lamp.get("label", ""),
                    "ip": lamp.get("ip", ""),
                    "enabled": lamp.get("enabled", True),
                    "ok": False if lamp.get("enabled", True) else None,
                    "latency_ms": None,
                    "status": None,
                    "error": "" if not lamp.get("enabled", True) else self._init_error,
                }
                for lamp in lamps
            ]
            return {
                "ok": False,
                "dry_run": False,
                "lamp_count": len(enabled_lamps),
                "ok_count": 0,
                "failed_count": len(enabled_lamps),
                "rows": rows,
                "error": self._init_error or "Lamp controller is not initialized",
            }

        try:
            raw_rows = asyncio.run(controller.diagnose_devices())
        except Exception as exc:
            rows = [
                {
                    "id": lamp["id"],
                    "label": lamp.get("label", ""),
                    "ip": lamp.get("ip", ""),
                    "enabled": lamp.get("enabled", True),
                    "ok": False if lamp.get("enabled", True) else None,
                    "latency_ms": None,
                    "status": None,
                    "error": "" if not lamp.get("enabled", True) else str(exc),
                }
                for lamp in lamps
            ]
            return {
                "ok": False,
                "dry_run": False,
                "lamp_count": len(enabled_lamps),
                "ok_count": 0,
                "failed_count": len(enabled_lamps),
                "rows": rows,
                "error": str(exc),
            }

        by_id = {str(row.get("id")): row for row in raw_rows if isinstance(row, dict)}
        rows = []
        ok_count = 0
        failed_count = 0
        for lamp in lamps:
            raw = by_id.get(lamp["id"])
            enabled = lamp.get("enabled", True)
            if raw:
                ok = bool(raw.get("ok"))
                if ok:
                    ok_count += 1
                elif enabled:
                    failed_count += 1
                rows.append(
                    {
                        "id": lamp["id"],
                        "label": lamp.get("label", ""),
                        "ip": raw.get("ip") or lamp.get("ip", ""),
                        "enabled": enabled,
                        "ok": ok,
                        "latency_ms": raw.get("latency_ms"),
                        "status": raw.get("status"),
                        "error": raw.get("error", ""),
                    }
                )
            else:
                if enabled:
                    failed_count += 1
                rows.append(
                    {
                        "id": lamp["id"],
                        "label": lamp.get("label", ""),
                        "ip": lamp.get("ip", ""),
                        "enabled": enabled,
                        "ok": False if enabled else None,
                        "latency_ms": None,
                        "status": "disabled" if not enabled else None,
                        "error": "" if not enabled else "Lamp is not initialized",
                    }
                )

        return {
            "ok": failed_count == 0,
            "dry_run": False,
            "lamp_count": len(enabled_lamps),
            "ok_count": ok_count,
            "failed_count": failed_count,
            "rows": rows,
            "error": "" if failed_count == 0 else "Some lamps are unavailable",
        }

    def _get_controller(self):
        with self._lock:
            if self._controller is not None:
                return self._controller

            try:
                app_path = str(APP_DIR)
                if app_path not in sys.path:
                    sys.path.insert(0, app_path)
                from libs.controller import TuyaLampController
                from libs.init_lamp import init_lamp

                devices = init_lamp()
                if not devices:
                    self._init_error = "No enabled lamps with IP and local_key were found"
                    return None

                self._controller = TuyaLampController(devices)
                self._init_error = ""
                return self._controller
            except Exception as exc:
                self._init_error = str(exc)
                return None
