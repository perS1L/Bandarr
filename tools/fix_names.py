"""Apply the current naming rules to files already in the master folder (tags + file name).

    python tools/fix_names.py            # only shows what would change
    python tools/fix_names.py --apply    # rewrites tags and renames

Soulseek copies only: source=bandcamp files are never touched. rekordbox ties cue points to
the file, so a track already imported there becomes a new track once renamed.
Data directory: $DATA_DIR (the container sets /data), else <repo>/data.
"""
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from bandarr.state import State  # noqa: E402
from bandarr.tagger import display_names, is_compilation, retag_and_place, safe_filename  # noqa: E402

DATA = Path(os.environ.get("DATA_DIR", ROOT / "data"))
MASTER = DATA / "master"
apply = "--apply" in sys.argv
st = State(DATA / "state.db")

changed = 0
for row in st.db.execute("SELECT * FROM tracks WHERE status='done' AND source='soulseek' AND path IS NOT NULL"):
    p = Path(row["path"])
    if not p.is_file():
        p = MASTER / p.name  # state paths were written on the host or in the container
    if not p.is_file() or MASTER.resolve() not in p.resolve().parents:
        continue
    album = st.db.execute("SELECT * FROM tracks WHERE item_id = ?", (row["item_id"],)).fetchall()
    compilation = is_compilation(album)
    artist, title = display_names(row, compilation)
    stem = safe_filename(f"{artist} - {title}")
    if re.fullmatch(re.escape(stem) + r"( \(\d+\))?", p.stem):
        continue
    changed += 1
    if apply:
        new = retag_and_place(p, row, MASTER, compilation)
        st.update(row["wishlist_key"], path=str(new), source="soulseek")
        print(f"renamed: {p.name}  ->  {new.name}")
    else:
        print(f"would rename: {p.name}  ->  {stem}{p.suffix}")
print(f"{changed} file(s) {'renamed' if apply else 'would change'}")
