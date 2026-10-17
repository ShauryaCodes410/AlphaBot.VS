import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent
for path in (str(BACKEND), str(REPO)):
    if path not in sys.path:
        sys.path.insert(0, path)
