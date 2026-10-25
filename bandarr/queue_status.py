"""Lossless transfers waiting in peers' queues (see `queued_transfers` in the state)."""
from __future__ import annotations

import time

from .slskd import SlskdError
from .state import State


def _age(seconds: float) -> str:
    if seconds < 3600:
        return f"{seconds / 60:.0f} min"
    if seconds < 86400:
        return f"{seconds / 3600:.1f} h"
    return f"{seconds / 86400:.1f} days"


def rows(st: State, client, with_position: bool = True, now: float | None = None) -> list[dict]:
    """One entry per waiting transfer: track, peer, age, state at slskd, queue place.

    Never raises on a peer or slskd problem: the entry says what could not be read.
    `place` is None when the peer does not answer (normal for many Soulseek clients).
    """
    now = now or time.time()
    out = []
    for q in st.queued():
        t = st.get_track(q["wishlist_key"])
        entry = {
            "track": f"{t['artist']} - {t['title']}" if t else q["wishlist_key"],
            "peer": q["username"], "age": _age(now - q["enqueued_at"]),
            "state": "?", "place": None, "note": "",
        }
        try:
            transfer = (client.batch(q["batch_id"]).get("transfers") or [{}])[0]
            entry["state"] = transfer.get("state") or "?"
            entry["place"] = transfer.get("placeInQueue")
            if with_position and entry["place"] is None and "queued" in entry["state"].lower():
                entry["place"] = client.queue_position(q["username"], transfer["id"])
                if entry["place"] is None:
                    entry["note"] = "peer did not report its queue"
        except SlskdError as e:
            entry["note"] = f"slskd: {e}"
        out.append(entry)
    return out


def format_rows(entries: list[dict]) -> str:
    if not entries:
        return "no transfer is waiting in a queue"
    lines = [f"{'TRACK':<42} {'PEER':<18} {'WAITING':<9} {'STATE':<18} PLACE"]
    for e in entries:
        place = "-" if e["place"] is None else str(e["place"])
        lines.append(f"{e['track'][:41]:<42} {e['peer'][:17]:<18} {e['age']:<9} {e['state'][:17]:<18} {place}"
                     + (f"  ({e['note']})" if e["note"] else ""))
    return "\n".join(lines)
