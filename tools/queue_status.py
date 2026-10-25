"""Show the lossless transfers waiting in peers' queues.

    python tools/queue_status.py [--no-position]

--no-position skips asking each peer for our place (slskd waits 5 s per silent peer).
Reads SLSKD_API_KEY and friends from the environment (source .env first).
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from bandarr.config import Config  # noqa: E402
from bandarr.queue_status import format_rows, rows  # noqa: E402
from bandarr.slskd import Slskd  # noqa: E402
from bandarr.state import State  # noqa: E402

cfg = Config.from_env()
print(format_rows(rows(State(cfg.state_db), Slskd(cfg.slskd_url, cfg.slskd_api_key),
                       with_position="--no-position" not in sys.argv)))
