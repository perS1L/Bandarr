"""Print the wishlist tracks not found on Soulseek (the same list the sync writes to data/not_found.txt).

    python tools/not_found.py
Data directory: $DATA_DIR (the container sets /data), else <repo>/data.
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from bandarr.missing import render  # noqa: E402
from bandarr.state import State  # noqa: E402

print(render(State(Path(os.environ.get("DATA_DIR", ROOT / "data")) / "state.db")), end="")
