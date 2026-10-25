"""Find the same song in master twice (an album and a single track of it, both on the wishlist).

    python tools/dedupe.py            # only shows what would change
    python tools/dedupe.py --apply    # moves the extra copies to <data>/replaced and marks them duplicate

Kept: source=bandcamp first, then lossless over MP3, then the shorter file name. Extra copies are
moved, never deleted. rekordbox: a moved file disappears from its library on the next import.
Data directory: $DATA_DIR (the container sets /data), else <repo>/data.
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from bandarr.state import State, dedup_key  # noqa: E402

DATA = Path(os.environ.get("DATA_DIR", ROOT / "data"))
MASTER, REPLACED = DATA / "master", DATA / "replaced"
apply = "--apply" in sys.argv
st = State(DATA / "state.db")


def file_of(row):
    p = Path(row["path"] or "")
    if not p.is_file():
        p = MASTER / p.name  # state paths were written on the host or in the container
    return p if p.is_file() else None


groups = {}
for row in st.db.execute("SELECT * FROM tracks WHERE status = 'done' AND path IS NOT NULL"):
    groups.setdefault(dedup_key(row), []).append(row)

moved = 0
for rows in groups.values():
    if len(rows) < 2:
        continue
    rows.sort(key=lambda r: (r["source"] != "bandcamp", r["format"] == "mp3", len(Path(r["path"]).name), r["wishlist_key"]))
    keep, extras = rows[0], rows[1:]
    for r in extras:
        if r["source"] == "bandcamp":
            continue  # a purchase is never touched
        f = file_of(r)
        print(f"{'moving' if apply else 'would move'}: {Path(r['path']).name}  (same song as {Path(keep['path']).name})")
        moved += 1
        if apply:
            if f:
                REPLACED.mkdir(parents=True, exist_ok=True)
                dest, n = REPLACED / f.name, 1
                while dest.exists():
                    dest = REPLACED / f"{f.stem}-{n}{f.suffix}"
                    n += 1
                f.rename(dest)
            st.mark_duplicate(r["wishlist_key"], keep["wishlist_key"])
print(f"{moved} extra cop{'y' if moved == 1 else 'ies'} {'moved' if apply else 'found'}")
