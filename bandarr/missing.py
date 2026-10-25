"""The list of wishlist tracks nobody shares on Soulseek (yet), for searching by hand."""
from __future__ import annotations

import os
import time
from pathlib import Path

from .state import State


def render(st: State, now: float | None = None) -> str:
    rows = st.db.execute(
        "SELECT t.* FROM tracks t JOIN items i USING (item_id)"
        " WHERE t.status = 'not_found' AND i.on_wishlist = 1 ORDER BY t.album, t.track_num, t.title").fetchall()
    stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(now or time.time()))
    if not rows:
        return f"Not found on Soulseek ({stamp}): nothing, every wishlist track has been found.\n"
    out = [f"Not found on Soulseek ({stamp}): {len(rows)} track(s). They are searched again automatically.", ""]
    album = None
    for r in rows:
        if r["album"] != album:
            album = r["album"]
            out += [f"{album}  ({r['album_artist']})", f"  {r['url']}"]
        last = time.strftime("%Y-%m-%d %H:%M", time.localtime(r["upgrade_checked_at"])) if r["upgrade_checked_at"] else "never"
        artist, title = (r["alt_artist"], r["alt_title"]) if r["alt_artist"] and r["alt_title"] else (r["artist"], r["title"])
        out.append(f"  - {artist} - {title}    (last searched {last})")
    return "\n".join(out) + "\n"


def write(st: State, path: Path) -> None:
    """Atomic write: the file may be open in an editor or synced while a run is in progress."""
    path = Path(path)
    tmp = path.with_name(path.name + ".part")
    tmp.write_text(render(st), encoding="utf-8")
    os.replace(tmp, path)
