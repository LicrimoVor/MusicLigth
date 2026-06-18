from pathlib import Path
import json


BASE_PATH = Path(__file__).parent.parent

with open(BASE_PATH.joinpath("core/snapshot.json")) as f:
    d = json.load(f)
DEVICE_IP = {device["id"]: device["ip"] for device in d["devices"]}


DEVICE_LOCAL = {
    "bf7d83acbfbab53cd44ign": "1vjT&upb7d2-2y?+",
    "bfe54d10d2e3ade05dtln0": "z5+_vK9~p!@S/Vq6",
    "bf8848fb5d7b7a568drlsq": "sn-;71NM?F6VXoRf",
    "bff31a95d7f1a6f2d4qtyx": "7-cDK51:&+&gO]rL",
    "bfe331579ebc27528cy2ax": ">gVA==q!@4!nOA(s",
    "bf5e44965ed19fd2c4ppel": "-ubJ0R?A|}Zn>sl=",
    "bf49dd04e681068f35izf9": "Yi:i~C#MAo8;VES;",
    "bf38fd090103ffb48cjkwy": "(kVkTWn|3'<+9lfn",
    "bfa81b616f3eb626de6gja": "4!7]]rC}Z49?ocq)",
    "bfbd4cb669009901e7p2fh": "<A_{R/Lvvui6qvOi",
}


LAMP_POSITION = {
    "bf7d83acbfbab53cd44ign": (0, 0),
    # "bfe54d10d2e3ade05dtln0": (0, 0.33),
    "bf8848fb5d7b7a568drlsq": (0, 0.66),
    # "bff31a95d7f1a6f2d4qtyx": (0, 1),
    # "bfe331579ebc27528cy2ax": (1, 0),
    # "bf5e44965ed19fd2c4ppel": (1, 0.33),
    # "bf49dd04e681068f35izf9": (1, 0.66),
    "bf38fd090103ffb48cjkwy": (1, 1),
    "bfa81b616f3eb626de6gja": (0.5, 0.165),
    # "bfbd4cb669009901e7p2fh": (0.5, 0.825),
}

# DEVICE_IP = {
#     "bfa81b616f3eb626de6gja": "192.168.1.33",
#     "bfe54d10d2e3ade05dtln0": "192.168.1.34",
#     "bf38fd090103ffb48cjkwy": "192.168.1.35",
#     "bf7d83acbfbab53cd44ign": "192.168.1.36",
#     "bf49dd04e681068f35izf9": "192.168.1.37",
#     "bf5e44965ed19fd2c4ppel": "192.168.1.38",
#     "bfbd4cb669009901e7p2fh": "192.168.1.39",
#     "bfe331579ebc27528cy2ax": "192.168.1.40",
#     "bf8848fb5d7b7a568drlsq": "192.168.1.41",
#     "bff31a95d7f1a6f2d4qtyx": "192.168.1.42",
# }
