"""Embed the Bandcamp cover in finished master files that have none.

    python tools/add_missing_covers.py            # uses data/state.db
Needs the art ids in the state (a normal `bandarr.sync` run fills them in).
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from bandarr.covers import get_cover  # noqa: E402
from bandarr.state import State  # noqa: E402
from bandarr.tagger import add_cover_if_missing  # noqa: E402

DATA = Path(os.environ.get("DATA_DIR", ROOT / "data"))  # the container sets /data
st = State(DATA / "state.db")
added = skipped = 0
for r in st.db.execute("SELECT artist, title, path, art_id FROM tracks WHERE status='done' AND path IS NOT NULL"):
    p = Path(r["path"])
    if not p.exists() or not r["art_id"]:
        skipped += 1
        continue
    cover = get_cover(r["art_id"], DATA / "covers")
    if cover and add_cover_if_missing(p, cover):
        added += 1
        print("cover added:", p.name)
print(f"{added} added, {skipped} skipped (no file or no art id)")
