import hashlib
import json
import math
import os
from pathlib import Path
import uuid

import numpy as np
from scipy import ndimage

from music.analysis import ANALYSIS_VERSION, AnalysisData, remove_work_directory
from music.storage import atomic_json, check_cancel
from model.settings import normalize_color


TIMELINE_VERSION = 3
DEFAULT_PALETTE = ((255, 77, 38), (51, 166, 255), (184, 89, 242))
TIMELINE_HZ = 20
EVENT_TYPES = ("Energy Glow", "Frequency Colors", "Beat Flash", "Bass Pulse", "Color Wave", "Build Up", "Drop")
EVENT_DTYPE = np.dtype([("timestamp", "<f8"), ("type", "u1"), ("target", "<i2"),
                        ("rgb", "u1", (3,)), ("brightness", "<f4"), ("duration", "<f4"),
                        ("attack", "<f4"), ("release", "<f4")])
PRESETS = {
    "Calm": {"beat": 0.08, "bass": 0.10, "wave": 0.035, "frequency": 0.35, "release": 0.5},
    "Rhythm": {"beat": 0.18, "bass": 0.18, "wave": 0.07, "frequency": 0.5, "release": 0.35},
    "Spectrum": {"beat": 0.06, "bass": 0.08, "wave": 0.025, "frequency": 0.9, "release": 0.5},
}


def show_settings(settings, lamp_ids):
    selected = settings.get("lamp_ids", lamp_ids)
    selected = [item for item in lamp_ids if item in selected]
    palette = settings.get("palette")
    palette = palette if isinstance(palette, (list, tuple)) and len(palette) == 3 else DEFAULT_PALETTE
    return {"preset": settings.get("preset") if settings.get("preset") in PRESETS else "Calm",
            "color_mode": "custom" if settings.get("color_mode") == "custom" else "auto",
            "palette": [normalize_color(color, DEFAULT_PALETTE[index]) for index, color in enumerate(palette)],
            "intensity": max(0.0, min(1.5, float(settings.get("intensity", 0.7)))),
            "max_brightness": max(0.0, min(1.0, float(settings.get("max_brightness", 0.8)))),
            "update_hz": max(1, min(30, int(settings.get("update_hz", 10)))),
            "latency_ms": max(-2000, min(2000, int(settings.get("latency_ms", 0)))),
            "lamp_offsets": {key: max(-2000, min(2000, int(value)))
                             for key, value in (settings.get("lamp_offsets") or {}).items() if key in lamp_ids},
            "lamp_ids": selected}


def timeline_key(content_identifier, settings):
    generation = {key: settings[key] for key in ("preset", "intensity", "max_brightness", "lamp_ids", "color_mode", "palette")}
    payload = json.dumps([content_identifier, ANALYSIS_VERSION, TIMELINE_VERSION, generation], sort_keys=True).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def pulse(seconds, timestamps, duration=0.28):
    if not len(timestamps):
        return 0.0
    index = int(np.searchsorted(timestamps, seconds))
    candidates = timestamps[max(0, index - 1):min(len(timestamps), index + 1)]
    distance = float(np.min(np.abs(candidates - seconds)))
    width = max(0.02, duration * 0.35)
    if distance > width * 4:
        return 0.0
    # Offline analysis permits a short lead-in, so the accent peaks on the beat.
    return float(math.exp(-0.5 * (distance / width) ** 2))


def hue_rgb(hue):
    # Vector-free HSV conversion avoids another dependency in the generation loop.
    import colorsys
    return np.asarray(colorsys.hsv_to_rgb(hue % 1.0, 0.72, 1.0), dtype=np.float32)


class LightTimeline:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.metadata = json.loads((self.directory / "timeline.json").read_text(encoding="utf-8"))
        if self.metadata.get("version") != TIMELINE_VERSION or not self.metadata.get("complete"):
            raise ValueError("Устаревший или неполный световой таймлайн")
        self.lamp_ids = self.metadata["lamp_ids"]
        self.frames = np.load(self.directory / "states.npy", mmap_mode="r", allow_pickle=False)
        self.events = np.load(self.directory / "events.npy", mmap_mode="r", allow_pickle=False)
        if self.frames.ndim != 3 or self.frames.shape[1:] != (len(self.lamp_ids), 4) or not len(self.frames):
            raise ValueError("Повреждённый таймлайн")

    def state_at(self, seconds, lamp_id):
        if lamp_id not in self.lamp_ids:
            return (0, 0, 0)
        seconds = max(0, min(self.metadata["duration"], float(seconds)))
        position = seconds * self.metadata["hz"]
        left = min(int(position), len(self.frames) - 1)
        right = min(left + 1, len(self.frames) - 1)
        index = self.lamp_ids.index(lamp_id)
        value = self.frames[left, index] * (1 - (position - left)) + self.frames[right, index] * (position - left)
        return tuple(np.clip(np.round(value[:3] * value[3] * 255), 0, 255).astype(int).tolist())

    def event(self, index):
        value = self.events[index]
        target = int(value["target"])
        return {"timestamp": float(value["timestamp"]), "type": EVENT_TYPES[int(value["type"])],
                "color": value["rgb"].tolist(), "brightness": float(value["brightness"]),
                "target": self.lamp_ids[target] if target >= 0 else "selected",
                "duration": float(value["duration"]),
                "transition": {"curve": "ease", "attack": float(value["attack"]), "release": float(value["release"])}}

    def events_between(self, start, end):
        if end < start:
            return []
        left = np.searchsorted(self.events["timestamp"], start, side="right")
        right = np.searchsorted(self.events["timestamp"], end, side="right")
        return [self.event(index) for index in range(int(left), int(right))]

    def close(self):
        for value in (self.frames, self.events):
            mapping = getattr(value, "_mmap", None)
            if mapping is not None:
                mapping.close()


class TimelineGenerator:
    def __init__(self, root):
        self.root = Path(root) / "timelines"
        self.root.mkdir(parents=True, exist_ok=True)

    def generate(self, analysis_path, settings, cancel=None, progress=lambda value, stage: None):
        analysis = AnalysisData(analysis_path)
        key = timeline_key(analysis.metadata["content_id"], settings)
        destination = self.root / key
        if (destination / "timeline.json").exists():
            try:
                cached = LightTimeline(destination)
                cached.close()
                analysis.close()
                return str(destination)
            except (OSError, ValueError, KeyError):
                pass
        work = self.root / (".work-" + uuid.uuid4().hex)
        work.mkdir()
        states = None
        events = None
        try:
            lamps = settings["lamp_ids"]
            preset = PRESETS[settings["preset"]]
            intensity, maximum = settings["intensity"], settings["max_brightness"]
            duration = analysis.duration
            count = math.ceil(duration * TIMELINE_HZ) + 1
            states = np.lib.format.open_memmap(work / "states.npy", mode="w+", dtype="float32",
                                              shape=(count, len(lamps), 4))
            times = analysis.features[:, 0]
            bass_onsets = []
            for onset in analysis.onsets:
                left = int(np.searchsorted(times, max(0, float(onset) - 0.02)))
                right = min(len(times), int(np.searchsorted(times, float(onset) + 0.08)) + 1)
                if right <= left:
                    continue
                peak = left + int(np.argmax(analysis.features[left:right, 2]))
                when = float(times[peak])
                if analysis.features[peak, 2] >= 0.55 and (not bass_onsets or when - bass_onsets[-1] > 0.18):
                    bass_onsets.append(when)
            bass_onsets = np.asarray(bass_onsets)
            custom_colors = settings["color_mode"] == "custom"
            frequency_palette = np.asarray(settings["palette"] if custom_colors else DEFAULT_PALETTE, dtype=float) / 255
            for index in range(count):
                if index % 100 == 0:
                    check_cancel(cancel)
                    progress(int(100 * index / count), "Световой таймлайн")
                seconds = min(duration, index / TIMELINE_HZ)
                feature_index = max(0, min(len(times) - 1, int(np.searchsorted(times, seconds) - 1)))
                frame = analysis.features[feature_index]
                bands = np.asarray(frame[2:5]) + 0.02
                frequency_color = bands @ frequency_palette / bands.sum()
                energy = float(frame[7])
                beat = pulse(seconds, analysis.beats, 0.2)
                bass = pulse(seconds, bass_onsets, 0.3)
                build, drop = 0.0, 0.0
                for transition in analysis.metadata["transitions"]:
                    elapsed = seconds - transition["time"]
                    if transition["type"] == "build_up" and 0 <= elapsed <= transition["duration"]:
                        build = max(build, elapsed / transition["duration"] * transition["strength"])
                    elif transition["type"] == "drop" and abs(elapsed) <= transition["duration"]:
                        drop = max(drop, math.exp(-0.5 * (elapsed / 0.12) ** 2) * transition["strength"])
                local_tempo = analysis.tempo_at(seconds)
                beat_duration = 60 / local_tempo if local_tempo > 0 else 2.0
                for lamp_index in range(len(lamps)):
                    fraction = lamp_index / max(1, len(lamps))
                    hue = 0.57 + seconds / 45 + fraction * 0.12
                    wave = pulse(seconds - fraction * beat_duration, analysis.beats, 0.25)
                    if custom_colors:
                        level = min(2.0, max(0.0, energy * 2))
                        left = min(1, int(level))
                        palette = frequency_palette[left] * (1 - (level - left)) + frequency_palette[left + 1] * (level - left)
                    else:
                        palette = hue_rgb(hue)
                    color = palette * (1 - preset["frequency"]) + frequency_color * preset["frequency"]
                    # Stable glow and modest rhythmic accents, rather than full on/off flashes.
                    brightness = maximum * min(1.0, 0.15 + intensity * (
                        0.6 * energy + preset["beat"] * beat + preset["bass"] * bass
                        + preset["wave"] * wave + 0.1 * build + 0.2 * drop))
                    states[index, lamp_index] = np.r_[color, brightness]

            # Symmetric filtering uses known future frames. Unlike a live EMA it
            # smooths the scene without adding a phase delay to every beat accent.
            for lamp_index in range(len(lamps)):
                for channel in range(4):
                    check_cancel(cancel)
                    seconds = preset["release"] * (0.12 if channel == 3 else 0.25)
                    states[:, lamp_index, channel] = ndimage.gaussian_filter1d(
                        states[:, lamp_index, channel], max(0.5, TIMELINE_HZ * seconds), mode="nearest")

            # Semantic events remain distinct from the dense, smoothly rendered states.
            cues = [(float(time), 0, 0.5) for time in np.arange(0, duration, 0.5)]
            cues += [(float(time), 1, 1.0) for time in np.arange(0, duration, 1)]
            cues += [(float(time), 2, 0.25) for time in analysis.beats]
            cues += [(float(time), 3, 0.35) for time in bass_onsets]
            cues += [(float(time), 4, min(2.0, 60 / max(1, analysis.tempo_at(float(time)))))
                     for time in analysis.beats[::2]]
            cues += [(item["time"], 5 if item["type"] == "build_up" else 6, item["duration"])
                     for item in analysis.metadata["transitions"]]
            cues.sort(key=lambda cue: cue[0])
            events = np.lib.format.open_memmap(work / "events.npy", mode="w+", dtype=EVENT_DTYPE, shape=(len(cues),))
            for index, (when, kind, length) in enumerate(cues):
                if index % 1000 == 0:
                    check_cancel(cancel)
                state = states[min(int(when * TIMELINE_HZ), count - 1), 0] if lamps else np.zeros(4)
                events[index] = (when, kind, -1, np.round(state[:3] * 255).astype("uint8"), state[3],
                                 length, 0.06, preset["release"])
            events.flush()
            events._mmap.close()
            events = None
            states.flush()
            states._mmap.close()
            states = None
            metadata = {"version": TIMELINE_VERSION, "complete": True, "key": key,
                        "content_id": analysis.metadata["content_id"], "duration": duration,
                        "hz": TIMELINE_HZ, "lamp_ids": lamps, "settings": settings,
                        "event_types": list(EVENT_TYPES), "lamp_group": {"id": "selected", "lamps": lamps}}
            atomic_json(work / "timeline.json", metadata)
            check_cancel(cancel)
            if destination.exists():
                destination.rename(destination.with_name(destination.name + ".invalid-" + uuid.uuid4().hex))
            os.replace(work, destination)
            progress(100, "Таймлайн готов")
            return str(destination)
        finally:
            analysis.close()
            if states is not None:
                states._mmap.close()
            if events is not None:
                events._mmap.close()
            if work.exists():
                remove_work_directory(work, self.root)
