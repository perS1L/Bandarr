"""Find the best Soulseek source for one wanted track."""
from __future__ import annotations

import logging
import time

from .scoring import LOSSLESS_TIER, Scored, Wanted, queries, rank
from .slskd import Slskd

log = logging.getLogger(__name__)
SEARCH_GAP_S = 3.0  # be gentle with the Soulseek server


def find_best(client: Slskd, w: Wanted, gap: float = SEARCH_GAP_S) -> list[Scored]:
    """Ranked candidates across the queries, best first; empty means not found.

    A query that only finds MP3 does not end the search: the remaining queries
    still run, because a better format (lossless) may surface under another
    wording. We stop early only once a lossless candidate is in hand.
    """
    found: dict[tuple[str, str], Scored] = {}
    for i, q in enumerate(queries(w)):
        if i:
            time.sleep(gap)
        ranked = rank(w, client.search(q))
        log.info("search %r: %d acceptable", q, len(ranked))
        for s in ranked:
            found.setdefault((s.candidate.username, s.candidate.filename), s)
        if any(s.tier == LOSSLESS_TIER for s in found.values()):
            break
    return sorted(found.values(), key=lambda s: s.score, reverse=True)
