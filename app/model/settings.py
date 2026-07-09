import copy
import json

from core.const import BASE_PATH


SETTINGS_PATH = BASE_PATH / "settings.json"

DEFAULT_COLOR_PROFILES = [
    {
        "name": "Неон",
        "quiet_color": [20, 32, 60],
        "medium_color": [0, 210, 255],
        "loud_color": [255, 42, 116],
        "quiet_percent": 0,
        "medium_percent": 50,
        "loud_percent": 100,
    },
    {
        "name": "Огонь",
        "quiet_color": [42, 18, 8],
        "medium_color": [255, 126, 24],
        "loud_color": [255, 235, 105],
        "quiet_percent": 0,
        "medium_percent": 50,
        "loud_percent": 100,
    },
    {
        "name": "Лед",
        "quiet_color": [12, 28, 54],
        "medium_color": [84, 214, 255],
        "loud_color": [238, 252, 255],
        "quiet_percent": 0,
        "medium_percent": 50,
        "loud_percent": 100,
    },
]

DEFAULT_SETTINGS = {
    "mode": "rms",
    "mic_name": "",
    "quiet_color": [0, 96, 255],
    "medium_color": [255, 180, 80],
    "loud_color": [255, 40, 40],
    "quiet_percent": 0,
    "medium_percent": 50,
    "loud_percent": 100,
    "static_color": [255, 180, 80],
    "min_brightness": 25,
    "max_brightness": 100,
    "smoothing": 0.28,
    "beat_scale": 1.0,
    "beat_threshold": 0.08,
    "beat_decay": 0.08,
    "selected_lamps": [],
    "color_profiles": DEFAULT_COLOR_PROFILES,
}


def clamp_number(value, low, high, fallback):
    try:
        return max(low, min(high, float(value)))
    except (TypeError, ValueError):
        return fallback


def normalize_color(value, fallback):
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        return list(fallback)
    return [int(clamp_number(channel, 0, 255, fallback[index])) for index, channel in enumerate(value)]


def normalize_profile(profile):
    if not isinstance(profile, dict):
        return None

    default = DEFAULT_COLOR_PROFILES[0]
    name = str(profile.get("name") or "").strip()
    if not name:
        return None

    quiet_percent = int(clamp_number(profile.get("quiet_percent"), 0, 100, 0))
    medium_percent = int(clamp_number(profile.get("medium_percent"), quiet_percent, 100, 50))
    loud_percent = int(clamp_number(profile.get("loud_percent"), medium_percent, 100, 100))
    return {
        "name": name[:48],
        "quiet_color": normalize_color(profile.get("quiet_color"), default["quiet_color"]),
        "medium_color": normalize_color(profile.get("medium_color"), default["medium_color"]),
        "loud_color": normalize_color(profile.get("loud_color"), default["loud_color"]),
        "quiet_percent": quiet_percent,
        "medium_percent": medium_percent,
        "loud_percent": loud_percent,
    }


def normalized_settings(data):
    settings = copy.deepcopy(DEFAULT_SETTINGS)
    if isinstance(data, dict):
        settings.update(data)

    # Backward compatibility with the previous two-color settings file.
    if isinstance(data, dict):
        if "quiet_color" not in data and "min_color" in data:
            settings["quiet_color"] = data["min_color"]
        if "loud_color" not in data and "max_color" in data:
            settings["loud_color"] = data["max_color"]

    settings["quiet_color"] = normalize_color(settings.get("quiet_color"), DEFAULT_SETTINGS["quiet_color"])
    settings["medium_color"] = normalize_color(settings.get("medium_color"), DEFAULT_SETTINGS["medium_color"])
    settings["loud_color"] = normalize_color(settings.get("loud_color"), DEFAULT_SETTINGS["loud_color"])
    settings["static_color"] = normalize_color(settings.get("static_color"), DEFAULT_SETTINGS["static_color"])

    quiet_percent = int(clamp_number(settings.get("quiet_percent"), 0, 100, 0))
    medium_percent = int(clamp_number(settings.get("medium_percent"), quiet_percent, 100, 50))
    loud_percent = int(clamp_number(settings.get("loud_percent"), medium_percent, 100, 100))
    settings["quiet_percent"] = quiet_percent
    settings["medium_percent"] = medium_percent
    settings["loud_percent"] = loud_percent

    settings["min_brightness"] = int(clamp_number(settings.get("min_brightness"), 0, 100, 25))
    settings["max_brightness"] = int(clamp_number(settings.get("max_brightness"), 0, 100, 100))
    if settings["min_brightness"] > settings["max_brightness"]:
        settings["max_brightness"] = settings["min_brightness"]

    settings["smoothing"] = clamp_number(settings.get("smoothing"), 0.05, 0.80, 0.28)
    settings["beat_scale"] = clamp_number(settings.get("beat_scale"), 0.0, 2.0, 1.0)
    settings["beat_threshold"] = clamp_number(settings.get("beat_threshold"), 0.0, 0.80, 0.08)
    settings["beat_decay"] = clamp_number(settings.get("beat_decay"), 0.01, 0.40, 0.08)

    profiles = []
    seen_names = set()
    for profile in settings.get("color_profiles") or []:
        normalized = normalize_profile(profile)
        if not normalized:
            continue
        profiles.append(normalized)
        seen_names.add(normalized["name"].lower())

    for profile in DEFAULT_COLOR_PROFILES:
        if profile["name"].lower() not in seen_names:
            profiles.append(copy.deepcopy(profile))

    settings["color_profiles"] = profiles
    return settings


def load_settings():
    if not SETTINGS_PATH.exists():
        return normalized_settings({})

    try:
        with SETTINGS_PATH.open("r", encoding="utf-8") as file:
            data = json.load(file)
        return normalized_settings(data)
    except Exception:
        return normalized_settings({})


def save_settings(data):
    with SETTINGS_PATH.open("w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
    return SETTINGS_PATH
