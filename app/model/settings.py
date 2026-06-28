import json

from core.const import BASE_PATH


SETTINGS_PATH = BASE_PATH / "settings.json"

DEFAULT_SETTINGS = {
    "mode": "rms",
    "mic_name": "",
    "min_color": [0, 96, 255],
    "max_color": [255, 40, 40],
    "static_color": [255, 180, 80],
    "min_brightness": 25,
    "max_brightness": 100,
    "smoothing": 0.28,
    "beat_scale": 1.0,
    "selected_lamps": [],
}


def load_settings():
    if not SETTINGS_PATH.exists():
        return dict(DEFAULT_SETTINGS)

    try:
        with SETTINGS_PATH.open("r", encoding="utf-8") as file:
            data = json.load(file)
        settings = dict(DEFAULT_SETTINGS)
        settings.update(data)
        return settings
    except Exception:
        return dict(DEFAULT_SETTINGS)


def save_settings(data):
    with SETTINGS_PATH.open("w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
    return SETTINGS_PATH
