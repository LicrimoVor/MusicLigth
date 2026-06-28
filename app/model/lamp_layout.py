import math

from core.const import LAMP_POSITION


def build_lamp_positions(device_ids):
    positions = {lamp_id: LAMP_POSITION[lamp_id] for lamp_id in device_ids if lamp_id in LAMP_POSITION}
    missing = [lamp_id for lamp_id in device_ids if lamp_id not in positions]

    if missing:
        radius = 0.42
        total = len(missing)
        for index, lamp_id in enumerate(missing):
            angle = (math.tau * index / max(1, total)) - math.pi / 2
            positions[lamp_id] = (
                0.5 + math.cos(angle) * radius,
                0.5 + math.sin(angle) * radius,
            )

    return positions
