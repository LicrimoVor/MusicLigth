import sys
from pathlib import Path


ROOT_PATH = Path(__file__).resolve().parents[1]
if str(ROOT_PATH) not in sys.path:
    sys.path.insert(0, str(ROOT_PATH))

from utils.lamp_config import main


if __name__ == "__main__":
    main()
