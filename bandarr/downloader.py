"""Download the best candidate through slskd, falling back to the next one."""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .scoring import Scored
from .slskd import Slskd, SlskdError

log = logging.getLogger(__name__)

AUDIO_EXT = {".flac", ".aiff", ".aif", ".mp3"}


class DownloadFailed(RuntimeError):
    pass


class StillQueued(RuntimeError):
    """The transfer is waiting in the peer's queue (not started, not failed)."""

    def __init__(self, batch_id: str, state: str, place: int | None):
        super().__init__(f"still {state!r} (queue place {place})")
        self.batch_id = batch_id


@dataclass
class Downloaded:
    path: Path
    scored: Scored


def safe_dir(key: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", key)


def peer_dest(key: str, username: str) -> str:
    """Sub-folder of slskd's downloads dir for one copy. One per (track, peer), flat, so
    cleaning one attempt never touches a transfer still waiting in another peer's queue."""
    return f"{safe_dir(key)}__{safe_dir(username)}"


def wait_for(
    client: Slskd, batch_id: str, queue_timeout: float, total_timeout: float, poll: float
) -> dict:
    """Wait for the single transfer of a batch. Returns it on success.

    Raises StillQueued when it is still waiting in a queue after queue_timeout (the
    caller decides whether to keep waiting in later runs) and DownloadFailed on any
    failure or if a started transfer exceeds total_timeout.
    """
    start = time.monotonic()
    started_at = None
    while True:
        time.sleep(poll)
        transfers = client.batch(batch_id).get("transfers") or []
        if not transfers:
            raise DownloadFailed("batch has no transfer")
        t = transfers[0]
        state = (t.get("state") or "").lower()
        if state.startswith("completed"):
            if "succeeded" in state:
                return t
            raise DownloadFailed(f"{t.get('state')}: {t.get('exception') or ''}".strip(": "))
        if "inprogress" in state and started_at is None:
            started_at = time.monotonic()
        elapsed = time.monotonic() - start
        if started_at is None and elapsed > queue_timeout:
            if "queued" in state:
                raise StillQueued(batch_id, t.get("state"), t.get("placeInQueue"))
            raise DownloadFailed(f"still {t.get('state')!r} after {queue_timeout:.0f}s")
        if elapsed > total_timeout:
            raise DownloadFailed(f"not finished after {total_timeout:.0f}s ({t.get('state')})")


def locate(root: Path, dest: str, filename: str) -> Path:
    """Find the downloaded file under <root>/<dest> (slskd may nest remote folders)."""
    base = filename.replace("\\", "/").rsplit("/", 1)[-1]
    hits = [p for p in (root / dest).rglob("*") if p.is_file() and p.name == base]
    if not hits:
        hits = [p for p in (root / dest).rglob("*") if p.suffix.lower() in AUDIO_EXT]
    if len(hits) != 1:
        raise DownloadFailed(f"expected 1 file under {root / dest}, found {len(hits)}")
    return hits[0]


def download_first_working(
    client: Slskd,
    ranked: list[Scored],
    key: str,
    downloads_root: Path,
    max_tries: int = 5,
    queue_timeout: float = 180,
    total_timeout: float = 900,
    poll: float = 2,
    exclude: frozenset | set = frozenset(),
    on_queued: Callable[[Scored, str, str], bool] | None = None,
    on_failed: Callable[[Scored, str], None] | None = None,
) -> Downloaded:
    """Try candidates best-first until one downloads. Raises DownloadFailed if none.

    exclude: (username, filename) sources already tried, skipped.
    on_queued(scored, batch_id, dest): called when a transfer is still queued after
        queue_timeout; return True to leave it queued (a later run collects it), False
        to cancel it. Without the callback it is cancelled.
    on_failed(scored, reason): called for each source that failed or was refused by the peer.
    """
    tried_users: set[str] = set()
    errors: list[str] = []
    for s in ranked:
        c = s.candidate
        if (c.username, c.filename) in exclude:
            continue
        if c.username in tried_users:
            continue  # a user that just failed/stalled will not be better a minute later
        if len(tried_users) >= max_tries:
            break
        tried_users.add(c.username)
        dest = peer_dest(key, c.username)
        log.info("downloading from %s (score %.1f): %s", c.username, s.score, c.filename)
        batch_id = None
        try:
            batch = client.enqueue(c.username, [{"filename": c.filename, "size": c.size}], dest, external_id=key)
            batch_id = batch["id"]
            wait_for(client, batch_id, queue_timeout, total_timeout, poll)
            return Downloaded(locate(Path(downloads_root), dest, c.filename), s)
        except StillQueued as e:
            if on_queued and on_queued(s, batch_id, dest):
                log.info("%s: left in %s's queue for a later run", key, c.username)
                errors.append(f"{c.username}: {e} (kept queued)")
                continue
            log.warning("%s failed: %s", c.username, e)
            errors.append(f"{c.username}: {e}")
            cancel_batch(client, batch_id)
        except (DownloadFailed, SlskdError) as e:
            log.warning("%s failed: %s", c.username, e)
            errors.append(f"{c.username}: {e}")
            if on_failed:
                on_failed(s, str(e))
            if batch_id:
                cancel_batch(client, batch_id)
    raise DownloadFailed("; ".join(errors) or "no candidate")


def cancel_batch(client: Slskd, batch_id: str) -> None:
    try:
        for t in client.batch(batch_id).get("transfers") or []:
            client.cancel(t["username"], t["id"], remove=True)
    except (SlskdError, KeyError):
        log.debug("could not cancel batch %s", batch_id)
