"""Accept a track the gate sent to review (you judged it genuine): convert, tag, place in master.

    python tools/accept_review.py                    # lists the tracks waiting in review
    python tools/accept_review.py KEY                # shows what would be done
    python tools/accept_review.py KEY --apply        # does it
    python tools/accept_review.py KEY --file PATH    # use another review copy than the one in the state

KEY is the wishlist_key shown by the list (e.g. 3374573026:3374573026). The gate is skipped on purpose
(a human decision), but the converter still refuses anything that is not a real FLAC (a renamed MP3
never becomes AIFF) and an AIFF must be 44.1/48 kHz. The checks stay in `gate_checks`, labelled
'genuine', so the gate rules can later be compared on your own judgments. The accepted file moves to
<review>/accepted; other refused copies stay where they are. Never touches source=bandcamp tracks.
Data directory: $DATA_DIR (the container sets /data), else <repo>/data.
"""
import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DATA_DIR", str(ROOT / "data"))
os.environ.setdefault("BANDCAMP_USER", "-")  # unused here, required by Config.from_env
os.environ.setdefault("SLSKD_API_KEY", "-")
from bandarr import convert, tagger  # noqa: E402
from bandarr.config import Config  # noqa: E402
from bandarr.gate import GateResult  # noqa: E402
from bandarr.state import State  # noqa: E402
from bandarr.sync import PLAYABLE_RATES, Deps, accept_refused  # noqa: E402

ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
ap.add_argument("key", nargs="?")
ap.add_argument("--file", type=Path)
ap.add_argument("--apply", action="store_true")
a = ap.parse_args()

cfg = Config.from_env()
st = State(cfg.state_db)


def review_file(row, given: Path | None) -> Path | None:
    p = given or Path(row["path"] or "")
    if not p.is_file() and not given:
        hits = list(cfg.review_dir.glob(f"*/{p.name}"))  # state paths come from the host or the container
        p = hits[0] if hits else p
    return p if p.is_file() else None


if not a.key:
    rows = st.db.execute("SELECT * FROM tracks WHERE status = 'in_review' ORDER BY item_id, track_num").fetchall()
    for r in rows:
        f = review_file(r, None)
        print(f"{r['wishlist_key']}  {r['artist']} - {r['title']}  [{f.name if f else 'file missing'}]")
    print(f"{len(rows)} track(s) in review")
    sys.exit(0)

row = st.get_track(a.key)
if row is None:
    sys.exit(f"unknown track {a.key}")
if row["source"] == "bandcamp":
    sys.exit("this track is a Bandcamp purchase: never replaced")
src = review_file(row, a.file)
if src is None:
    sys.exit("review file not found (give it with --file)")
ext = src.suffix.lower()
if ext not in (".flac", ".aiff", ".aif"):
    sys.exit(f"only FLAC/AIFF copies are accepted, not {ext}")
if ext != ".flac" and convert.probe(src).sample_rate not in PLAYABLE_RATES:
    sys.exit("this AIFF is not 44.1/48 kHz: not playable on the XDJ-700")

print(f"{'accepting' if a.apply else 'would accept'}: {row['artist']} - {row['title']}  <-  {src}")
if not a.apply:
    sys.exit(0)

res = GateResult("pass", "accepted manually after review", rule="manual")
try:
    placed = accept_refused(st, cfg, Deps(), row, src, res)  # also moves the copy to <review>/accepted
except (convert.NotFlac, tagger.TagError) as e:
    sys.exit(f"refused: {e}")
with st.db:
    st.db.execute("UPDATE gate_checks SET label = 'genuine' WHERE wishlist_key = ?", (a.key,))
print(f"placed: {placed.name}")
