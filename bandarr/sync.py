"""bandcamp-sync: the orchestrator (CLAUDE.md sections 5 and 7).

    python -m bandarr.sync [--once] [--limit N] [--dry-run] [--no-upgrade]

One run:
  1. collect lossless transfers that were left waiting in a peer's queue by earlier runs;
  2. wishlist -> state -> for each pending track: search slskd, download (next peer when a
     copy is refused), gate (lossless from Soulseek only), convert FLAC -> AIFF, tag, place
     in the master folder, record the outcome;
  3. upgrade loop: look for lossless copies of the Soulseek MP3 fallbacks and replace them.
A failing track never stops the run.
"""
from __future__ import annotations

import argparse
import functools
import logging
import queue
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

from . import convert, covers, gate, tagger
from .bandcamp import fetch_tracks, fetch_wishlist_items
from .config import Config
from .downloader import DownloadFailed, Downloaded, download_first_working, locate, peer_dest, safe_dir
from .finder import find_best
from .scoring import LOSSLESS_TIER, Wanted
from .slskd import SearchGate, Slskd, SlskdError
from . import missing
from .state import State, dedup_key

log = logging.getLogger(__name__)

PLAYABLE_RATES = (44100, 48000)  # Pioneer XDJ-700 AIFF/WAV limit
_REPLACED_LOCK = threading.Lock()  # name choice + move into replaced_dir must not interleave


@dataclass
class Deps:
    """The moving parts, replaceable in tests."""
    fetch_tracks: Callable = fetch_tracks
    # the SearchGate of the slskd client already spaces searches, across workers
    find_best: Callable = functools.partial(find_best, gap=0)
    download: Callable = download_first_working
    locate: Callable = locate
    gate_check: Callable = gate.check
    quarantine: Callable = gate.quarantine
    flac_to_aiff: Callable = convert.flac_to_aiff
    probe: Callable = convert.probe
    tag_and_place: Callable = tagger.tag_and_place
    get_cover: Callable = covers.get_cover
    fetch_items: Callable = fetch_wishlist_items


@dataclass
class Report:
    done: int = 0
    in_review: int = 0
    consensus: int = 0  # refused copies accepted because their folder passed
    not_found: int = 0
    waiting: int = 0  # only a lossless copy, still queued at a peer
    collected: int = 0  # queued transfers that finished and were installed
    upgraded: int = 0  # MP3 fallbacks replaced by lossless
    failed: list[str] = field(default_factory=list)  # left pending, retried next run
    new_tracks: int = 0
    expand_failures: int = 0
    off_wishlist: int = 0  # pending tracks skipped: their album left the wishlist

    def __str__(self) -> str:
        return (f"{self.new_tracks} new tracks | done {self.done}, in review {self.in_review}, "
                f"not found {self.not_found}, waiting in a queue {self.waiting}, "
                f"retry later {len(self.failed)} | queued transfers collected {self.collected}, "
                f"upgraded to lossless {self.upgraded}, accepted by album consensus {self.consensus} | albums not expanded {self.expand_failures}"
                f" | skipped, off the wishlist {self.off_wishlist}")


# --- helpers ---------------------------------------------------------------------------

def _run_tasks(st: State, client, tasks, workers: int, fn: Callable) -> list:
    """fn(state, client, task) for every task, `workers` at a time; results in task order.

    Each worker gets its own state connection and slskd client (neither may be shared
    between threads). With one worker, or one task, everything runs in the caller's thread
    on the objects it passed in.
    """
    tasks = list(tasks)
    n = min(max(1, workers), len(tasks))
    if n <= 1:
        return [fn(st, client, t) for t in tasks]
    make_client = getattr(client, "clone", None)  # test doubles have none: they are shared
    pool: queue.Queue = queue.Queue()
    states = []
    for _ in range(n):
        s = st.clone()
        states.append(s)
        pool.put((s, make_client() if make_client else client))

    def run(task):
        s, c = pool.get()
        try:
            return fn(s, c, task)
        finally:
            pool.put((s, c))

    try:
        with ThreadPoolExecutor(max_workers=n, thread_name_prefix="dl") as ex:
            return list(ex.map(run, tasks))
    finally:
        for s in states:
            s.close()


def _find(cfg: Config, client, d: Deps, row):
    """Ranked sources for a track, without the peers the user blocked."""
    ranked = d.find_best(client, Wanted.from_mapping(row))
    if cfg.blocked_peers:
        ranked = [s for s in ranked if s.candidate.username.lower() not in cfg.blocked_peers]
    return ranked


def _album_rows(st: State, item_id: str):
    return st.db.execute("SELECT * FROM tracks WHERE item_id = ?", (item_id,)).fetchall()


def _clean_scratch(cfg: Config, st: State, key: str) -> None:
    """Remove this track's download scratch folders, except those of transfers still queued."""
    keep = st.queued_dests(key)
    for p in cfg.downloads_dir.glob(f"{safe_dir(key)}__*"):
        if p.is_dir() and p.name not in keep:
            shutil.rmtree(p, ignore_errors=True)


def _remove_scratch(cfg: Config, key: str, peer: str) -> None:
    shutil.rmtree(cfg.downloads_dir / peer_dest(key, peer), ignore_errors=True)


def _callbacks(st: State, key: str):
    def on_queued(s, batch_id, dest) -> bool:
        if s.tier != LOSSLESS_TIER:
            return False  # a queued MP3 is not worth waiting for
        c = s.candidate
        st.add_queued(key, batch_id, c.username, c.filename, c.size, dest)
        st.log_source(key, c.username, c.filename, "queued")
        return True

    def on_failed(s, reason) -> None:
        st.log_source(key, s.candidate.username, s.candidate.filename, "failed")

    return on_queued, on_failed


@dataclass
class _Refused:
    res: object  # gate.GateResult
    fmt: str | None
    dest: Path  # where the refused copy now lives (review folder)


def _refuse(st: State, d: Deps, cfg: Config, key: str, res, got, fmt: str | None) -> _Refused:
    """Park a refused copy in the review folder (never deleted) and remember the source."""
    c = got.scored.candidate
    res.notes.append(f"copy from peer {c.username}")
    log.warning("copy from %s refused (%s): %s", c.username, res.verdict, res.reason)
    st.log_source(key, c.username, c.filename, "refused")
    return _Refused(res, fmt, d.quarantine(got.path, res, cfg.review_dir))


def _handle_copy(st: State, cfg: Config, d: Deps, row, got, upgrading: bool):
    """Gate, convert, tag and place one downloaded copy.

    upgrading=True leaves the track's state untouched until the copy is installed (the
    track may already have a good MP3 in master that must not be lost on a refusal).
    Returns ("done", (placed, fmt, res)) or ("refused", _Refused).
    """
    key = row["wishlist_key"]
    src: Path = got.path
    if not upgrading:
        st.update(key, status="downloaded", source="soulseek", path=str(src))

    ext = src.suffix.lower()
    res = d.gate_check(src, "soulseek")  # n/a for MP3
    check_id = None
    if ext in gate.LOSSLESS_EXT and res.rule != gate.NO_GATE:  # nothing measured without a gate
        c = got.scored.candidate  # every lossless copy is a measurement
        check_id = st.record_gate(key, str(src), res, peer=c.username, remote_dir=remote_dir(c.filename))
    if not res.ok:
        refused = _refuse(st, d, cfg, key, res, got, "flac" if ext == ".flac" else "aiff")
        if check_id:
            st.set_gate_review(check_id, str(refused.dest))  # the album consensus may rescue it later
        return "refused", refused

    final, fmt = src, ext.lstrip(".")
    try:
        if ext == ".flac":
            # INVARIANT: only a FLAC that passed the gate reaches the converter, which
            # also re-checks the real codec (NotFlac) before touching anything.
            final, fmt = d.flac_to_aiff(src), "aiff"
        elif ext in (".aiff", ".aif"):
            rate = d.probe(src).sample_rate
            if rate not in PLAYABLE_RATES:
                res.verdict, res.reason = "suspect", f"AIFF at {rate} Hz is not playable on the XDJ-700"
                return "refused", _refuse(st, d, cfg, key, res, got, "aiff")
            fmt = "aiff"
        elif ext != ".mp3":
            raise tagger.TagError(f"unexpected format {ext}")
    except convert.NotFlac as e:
        res.verdict, res.reason = "fail", str(e)
        return "refused", _refuse(st, d, cfg, key, res, got, "flac")

    compilation = tagger.is_compilation(_album_rows(st, row["item_id"]))
    cover = d.get_cover(row["art_id"], cfg.covers_dir)  # None if unavailable: never blocks a track
    placed = d.tag_and_place(final, row, cfg.master_dir, "soulseek", compilation, cover)
    if final != src:  # the intermediate FLAC; the AIFF is safely in master
        if cfg.keep_originals:
            cfg.originals_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), cfg.originals_dir / src.name)
        else:
            src.unlink(missing_ok=True)
    return "done", (placed, fmt, res)


def remote_dir(filename: str) -> str:
    """The folder a file sits in on the peer's disk (Soulseek paths use backslashes), lower case."""
    return filename.replace("/", "\\").rpartition("\\")[0].lower()


def _inside(path: Path, folder: Path) -> bool:
    try:
        path.resolve().relative_to(folder.resolve())
        return True
    except ValueError:
        return False


def _old_file(row, cfg: Config) -> Path | None:
    """The master file this track currently points at, if any.

    Paths in the state are absolute and may come from another mount (a run on the host, then
    one in the container): when the stored path is gone, look for the same file name in master.
    """
    if not row["path"]:
        return None
    p = Path(row["path"])
    if not p.is_file():
        # Only a master file whose recorded folder was the master folder (seen from the host): a path
        # that pointed into review/ or the downloads must never resolve to a master file of that name.
        if row["status"] != "done" or p.parent.name != cfg.master_dir.name:
            return None
        p = cfg.master_dir / p.name
    return p if p.is_file() and _inside(p, cfg.master_dir) else None


def _finish(st: State, cfg: Config, row, placed: Path, fmt: str, res) -> None:
    """Record a successful install and retire the file it replaces (MP3 -> lossless).

    The old file is moved to replaced_dir, not deleted: rekordbox ties cue points and
    beatgrids to the file, so the user may want it back. Only a file inside master is touched.
    """
    key = row["wishlist_key"]
    old = _old_file(row, cfg)
    if old and old.resolve() != Path(placed).resolve():
        if cfg.delete_replaced:
            old.unlink()
        else:
            cfg.replaced_dir.mkdir(parents=True, exist_ok=True)
            with _REPLACED_LOCK:
                dest, n = cfg.replaced_dir / old.name, 2
                while dest.exists():
                    dest = cfg.replaced_dir / f"{old.stem} ({n}){old.suffix}"
                    n += 1
                shutil.move(str(old), dest)
        log.info("%s: replaced %s", key, old.name)
    st.update(key, status="done", source="soulseek", format=fmt, gate_verdict=res.verdict, path=str(placed))
    log.info("%s -> %s", key, Path(placed).name)


def _acquire(st: State, cfg: Config, client, d: Deps, row, ranked, upgrading: bool, queue_timeout: float):
    """Try up to cfg.max_source_tries different peers until one copy is accepted.

    Returns (data, refused): data = (placed, fmt, res) on success, else None; refused =
    the _Refused copies met on the way. Copies waiting in a queue are kept for a later run.
    """
    key = row["wishlist_key"]
    on_queued, on_failed = _callbacks(st, key)
    refused: list[_Refused] = []
    remaining = ranked
    for _ in range(cfg.max_source_tries):
        _clean_scratch(cfg, st, key)
        try:
            got = d.download(client, remaining, key, cfg.downloads_dir, queue_timeout=queue_timeout,
                             exclude=st.tried_sources(key), on_queued=on_queued, on_failed=on_failed)
        except DownloadFailed as e:
            log.warning("%s: no (further) source could be downloaded (%s)", key, e)
            break
        peer = got.scored.candidate.username
        outcome, data = _handle_copy(st, cfg, d, row, got, upgrading)
        _remove_scratch(cfg, key, peer)
        if outcome == "done":
            return data, refused
        refused.append(data)
        remaining = [x for x in remaining if x.candidate.username != peer]
        if not remaining:
            break
    return None, refused


def accept_refused(st: State, cfg: Config, d: Deps, row, src: Path, res) -> Path:
    """Install a copy the gate refused but that was judged genuine (by hand or by consensus).

    The gate is skipped on purpose; the converter still refuses a file that is not a real FLAC
    (NotFlac) and an AIFF must be 44.1/48 kHz. The copy then moves to <review>/accepted.
    Never call it for a source=bandcamp track.
    """
    ext = src.suffix.lower()
    if ext not in gate.LOSSLESS_EXT:
        raise tagger.TagError(f"only FLAC/AIFF copies are accepted, not {ext}")
    if ext != ".flac" and d.probe(src).sample_rate not in PLAYABLE_RATES:
        raise tagger.TagError("this AIFF is not 44.1/48 kHz: not playable on the XDJ-700")
    final = d.flac_to_aiff(src) if ext == ".flac" else src  # raises NotFlac for a renamed MP3
    compilation = tagger.is_compilation(_album_rows(st, row["item_id"]))
    cover = d.get_cover(row["art_id"], cfg.covers_dir)
    placed = d.tag_and_place(final, row, cfg.master_dir, "soulseek", compilation, cover)
    _finish(st, cfg, row, placed, "aiff", res)
    st.clear_retry(row["wishlist_key"])
    done_dir = cfg.review_dir / "accepted"
    done_dir.mkdir(parents=True, exist_ok=True)
    for f in (src, src.with_name(src.name + ".verdict.json")):
        if f.exists():
            shutil.move(str(f), done_dir / f.name)
    return Path(placed)


def consensus_rescue(st: State, cfg: Config, client, d: Deps, rep: Report) -> None:
    """Accept refused copies whose own folder (same peer, same directory) passed the gate.

    A transcoded release fails on all its tracks; one flagged track among passing ones is far more
    likely a false positive of the detectors (ambient/techno). Only the SAME folder of the SAME peer
    counts, never the rest of what a peer shares, and only the gate's own PASS is a witness.
    Needs cfg.consensus_min passed tracks and a majority of the folder's tracks checked.
    """
    if cfg.consensus_min <= 0:
        return
    for c in st.consensus_candidates():
        key = c["wishlist_key"]
        row = st.get_track(key)
        if (row is None or row["source"] == "bandcamp" or row["status"] == "duplicate"
                or (row["status"] == "done" and row["format"] in ("aiff", "flac"))):
            continue
        src = Path(c["review_path"])
        if not src.is_file():
            hits = list(cfg.review_dir.glob(f"*/{src.name}"))  # the path may come from the host
            if not hits:
                continue
            src = hits[0]
        passed, total = st.folder_votes(c["peer"], c["remote_dir"], key)
        if passed < cfg.consensus_min or passed * 2 <= total + 1:  # +1: this track is checked too
            continue
        text = f"{passed}/{total + 1}"
        res = gate.GateResult("pass", f"accepted by album consensus: {text} tracks of {c['peer']}'s folder passed",
                              rule="consensus")
        try:
            accept_refused(st, cfg, d, row, src, res)
        except (convert.NotFlac, tagger.TagError, OSError) as e:
            log.warning("%s: consensus accept failed: %s", key, e)
            continue
        st.set_gate_consensus(c["id"], text)
        _release_queued(st, client, key)
        rep.consensus += 1
        log.info("%s: refused copy from %s accepted by album consensus (%s)", key, c["peer"], text)


# --- the three phases ----------------------------------------------------------------

def process_track(st: State, cfg: Config, client, d: Deps, row) -> str:
    """Handle one pending track. Returns done | in_review | not_found | waiting | retry.

    A copy refused by the gate does not end the track: the next peer is tried. Only when
    every copy is refused does the track go to review. A lossless copy still queued at a
    peer is left there; if nothing else works the track waits for it ("waiting").
    """
    key = row["wishlist_key"]
    ranked = _find(cfg, client, d, row)
    if not ranked:
        st.update(key, status="not_found")
        st.mark_upgrade_checked(key)  # searched again after upgrade_interval_h
        log.info("%s: not found on Soulseek", key)
        return "not_found"

    data, refused = _acquire(st, cfg, client, d, row, ranked, upgrading=False, queue_timeout=cfg.queue_timeout_s)
    if data:
        placed, fmt, res = data
        _finish(st, cfg, row, placed, fmt, res)
        if fmt != "mp3":
            _release_queued(st, client, key)  # a lossless copy is in place: free the other peers' queues
        return "done"
    if refused:
        first = refused[0]  # best-ranked refused copy is the one the state points at
        st.update(key, status="in_review", source="soulseek", format=first.fmt,
                  gate_verdict=first.res.verdict, path=str(first.dest))
        log.warning("%s -> review: %d refused copies, no acceptable source", key, len(refused))
        return "in_review"
    if key in st.queued_keys():
        log.info("%s: waiting for a queued lossless copy", key)
        return "waiting"
    return "retry"


def collect_queued(st: State, cfg: Config, client, d: Deps, rep: Report) -> None:
    """Install lossless transfers left waiting by earlier runs, now that they are done."""
    for q in st.queued():
        key = q["wishlist_key"]
        try:
            row = st.get_track(key)
            if row is None or row["source"] == "bandcamp":  # bought meanwhile: never replace
                _drop_transfer(st, client, q, "failed")
                continue
            if row["status"] == "done" and row["format"] in ("aiff", "flac"):  # lossless already in place
                _drop_transfer(st, client, q, "failed")
                continue
            if q["username"].lower() in cfg.blocked_peers:  # blocked since it was queued
                log.info("%s: %s is blocked, dropping its queued transfer", key, q["username"])
                _drop_transfer(st, client, q, "failed")
                continue
            if not st.is_on_wishlist(row["item_id"]):  # no longer wanted: free the peer's queue
                log.info("%s: album left the wishlist, dropping its queued transfer", key)
                _drop_transfer(st, client, q, "failed")
                continue
            try:
                batch = client.batch(q["batch_id"])
            except SlskdError as e:
                log.warning("%s: queued transfer lost (%s)", key, e)
                _drop_transfer(st, client, q, "failed", cancel=False)
                continue
            t = (batch.get("transfers") or [{}])[0]
            state = (t.get("state") or "").lower()
            if state.startswith("completed"):
                if "succeeded" not in state:
                    log.warning("%s: queued transfer from %s ended: %s", key, q["username"], t.get("state"))
                    _drop_transfer(st, client, q, "failed", cancel=False)
                    continue
                _install_collected(st, cfg, d, row, q, rep)
            elif time.time() - q["enqueued_at"] > cfg.queue_max_age_days * 86400:
                log.warning("%s: giving up on %s's queue after %.0f days", key, q["username"], cfg.queue_max_age_days)
                _drop_transfer(st, client, q, "failed")
        except Exception:  # noqa: BLE001 - one transfer must not stop the others
            log.exception("%s: collecting queued transfer failed", key)


def _release_queued(st: State, client, key: str) -> None:
    """Cancel the lossless transfers still queued for a track that now has a lossless copy."""
    for q in st.queued():
        if q["wishlist_key"] == key:
            log.info("%s: lossless copy already in place, cancelling the queued one at %s", key, q["username"])
            _drop_transfer(st, client, q, "failed")


def _drop_transfer(st: State, client, q, outcome: str, cancel: bool = True) -> None:
    if cancel:
        try:
            transfer = client.batch(q["batch_id"])["transfers"][0]
            client.cancel(transfer["username"], transfer["id"], remove=True)
        except Exception:  # noqa: BLE001
            log.debug("could not cancel queued transfer %s", q["batch_id"])
    st.log_source(q["wishlist_key"], q["username"], q["filename"], outcome)
    st.drop_queued(q["id"])


def _install_collected(st: State, cfg: Config, d: Deps, row, q, rep: Report) -> None:
    key = row["wishlist_key"]
    scored = SimpleNamespace(candidate=SimpleNamespace(username=q["username"], filename=q["filename"], size=q["size"]),
                             tier=LOSSLESS_TIER, score=0.0)
    path = d.locate(cfg.downloads_dir, q["dest"], q["filename"])
    st.drop_queued(q["id"])  # from here the copy is ours; its folder is no longer "queued"
    outcome, data = _handle_copy(st, cfg, d, row, Downloaded(path, scored), upgrading=True)
    shutil.rmtree(cfg.downloads_dir / q["dest"], ignore_errors=True)
    if outcome == "done":
        placed, fmt, res = data
        was_mp3 = row["format"] == "mp3" and row["status"] == "done"
        _finish(st, cfg, row, placed, fmt, res)
        rep.collected += 1
        rep.upgraded += was_mp3
    else:
        log.warning("%s: the queued copy from %s was refused (%s)", key, q["username"], data.res.verdict)


def _upgrade_one(st: State, cfg: Config, client, d: Deps, row) -> bool:
    """Try to replace one MP3 by a lossless copy. True if it was replaced."""
    key = row["wishlist_key"]
    try:
        st.mark_upgrade_checked(key)  # first: a crash must not make us loop on this track
        ranked = _find(cfg, client, d, row)
        tried = st.tried_sources(key)
        lossless = [s for s in ranked if s.tier == LOSSLESS_TIER
                    and (s.candidate.username, s.candidate.filename) not in tried]
        if not lossless:
            return False
        log.info("upgrade: %d lossless candidates for %s - %s", len(lossless), row["artist"], row["title"])
        data, _ = _acquire(st, cfg, client, d, row, lossless, upgrading=True,
                           queue_timeout=cfg.upgrade_queue_timeout_s)
        if not data:
            return False
        placed, fmt, res = data
        _finish(st, cfg, row, placed, fmt, res)
        _release_queued(st, client, key)
        return True
    except Exception:  # noqa: BLE001
        log.exception("%s: upgrade failed", key)
        return False


def upgrade_search(st: State, cfg: Config, client, d: Deps, rep: Report) -> None:
    """Look for lossless copies of Soulseek MP3 fallbacks (never source=bandcamp)."""
    rows = st.upgrade_due(cfg.upgrade_interval_h, cfg.upgrade_limit)
    results = _run_tasks(st, client, rows, cfg.download_workers, lambda s, c, row: _upgrade_one(s, cfg, c, d, row))
    rep.upgraded += sum(results)


def run_once(cfg: Config, st: State, client=None, d: Deps | None = None, dry_run: bool = False) -> Report:
    d = d or Deps()
    rep = Report()

    if cfg.retry_not_found:
        with st.db:
            st.db.execute("UPDATE tracks SET status='pending' WHERE status='not_found' AND (source IS NULL OR source != 'bandcamp')")
    else:
        st.requeue_stale_not_found(cfg.not_found_retry_h)
    with st.db:  # a crash between download and import: start those over
        st.db.execute("UPDATE tracks SET status='pending' WHERE status='downloaded'")

    items = d.fetch_items(cfg.bandcamp_user, cfg.bandcamp_identity)
    st.set_art_ids({str(i.get("item_id")): i.get("item_art_id") for i in items})  # covers for older rows
    present = {str(i.get("item_id")) for i in items}
    if getattr(items, "complete", True):
        # An empty wishlist is trusted: the page said item_count 0 (the reader raises on anything
        # unclear). Distrusting it would keep processing the previous account's tracks.
        if not present:
            log.warning("the wishlist of %r is EMPTY: every pending track is skipped "
                        "(wrong BANDCAMP_USER, or a new account?)", cfg.bandcamp_user)
        left, back = st.sync_wishlist(present)
        if left:
            log.info("%d albums are no longer on the wishlist (bought, removed or another account): "
                     "their pending tracks are skipped", left)
        if back:
            log.info("%d albums are back on the wishlist", back)
    else:
        log.warning("wishlist only partly read: which albums are on it is left unchanged")
    tracks, failed_items = d.fetch_tracks(cfg.bandcamp_user, cfg.bandcamp_identity,
                                          known_item_ids=st.known_item_ids(), items=items)
    rep.new_tracks = st.add_tracks(tracks)
    rep.expand_failures = len(failed_items)
    rep.off_wishlist = st.off_wishlist_pending()
    log.info("wishlist: %s%s", st.summary(),
             f" | {rep.off_wishlist} pending tracks skipped (album no longer on the wishlist)" if rep.off_wishlist else "")
    if dry_run:
        _write_missing(st, cfg)
        return rep

    collect_queued(st, cfg, client, d, rep)

    waiting = st.queued_keys()
    if (held := st.deferred_count()):
        log.info("%d pending tracks are held back by their retry delay", held)
    if (dups := st.mark_duplicates()):
        log.info("%d pending tracks are already in master under another wishlist item (album + single): skipped", dups)
    pending, seen = [], set()
    for r in st.to_fetch():
        if r["wishlist_key"] in waiting or dedup_key(r) in seen:
            continue  # the same song twice in one run would race: the second is settled next run
        seen.add(dedup_key(r))
        pending.append(r)
    if cfg.limit is not None:
        pending = pending[: cfg.limit]
    def one(s: State, c, numbered):
        i, row = numbered
        key = row["wishlist_key"]
        log.info("[%d/%d] %s - %s", i, len(pending), row["artist"], row["title"])
        try:
            outcome = process_track(s, cfg, c, d, row)
        except Exception:  # one bad track must not stop the run
            log.exception("%s failed unexpectedly; left pending", key)
            outcome = "retry"
        if outcome == "retry":
            delay = s.schedule_retry(key, cfg.retry_base_h, cfg.retry_max_h)
            log.info("%s: no result; next attempt in %.0f h", key, delay)
        else:
            s.clear_retry(key)
        return key, outcome

    # counters are summed here, in one thread
    for key, outcome in _run_tasks(st, client, list(enumerate(pending, 1)), cfg.download_workers, one):
        if outcome == "retry":
            rep.failed.append(key)
        else:
            setattr(rep, outcome, getattr(rep, outcome) + 1)

    consensus_rescue(st, cfg, client, d, rep)
    if cfg.upgrade:
        upgrade_search(st, cfg, client, d, rep)
    _write_missing(st, cfg)
    return rep


def _write_missing(st: State, cfg: Config) -> None:
    """data/not_found.txt: the tracks to look for by hand. Never worth failing a run over."""
    try:
        missing.write(st, cfg.state_db.parent / "not_found.txt")
    except OSError as e:
        log.warning("could not write not_found.txt: %s", e)


def preflight(cfg: Config, client) -> list[str]:
    """Problems that would make every track fail. A broken tool must stop the run: left
    alone it would surface as 'suspect' files and flood the review folder."""
    problems = []
    for name, binary in (("ffmpeg", convert.FFMPEG), ("ffprobe", convert.FFPROBE)):
        try:
            subprocess.run([binary, "-version"], capture_output=True, check=True, timeout=120,
                           stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError) as e:
            problems.append(f"{name} ({binary}) is not usable: {e}")
    try:
        state = client._req("GET", "/application")["server"]["state"]
        if "LoggedIn" not in state:
            problems.append(f"slskd is not logged in to Soulseek (state: {state})")
    except (SlskdError, OSError, KeyError) as e:
        problems.append(f"slskd is not reachable at {cfg.slskd_url}: {e}")
    if not cfg.downloads_dir.is_dir():
        problems.append(f"downloads dir {cfg.downloads_dir} does not exist")
    return problems


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true", help="run once even if SCRIPT_INTERVAL is set")
    ap.add_argument("--limit", type=int, help="max tracks to process this run")
    ap.add_argument("--dry-run", action="store_true", help="read the wishlist and update the state only")
    ap.add_argument("--no-upgrade", action="store_true", help="skip the search for lossless copies of MP3s")
    a = ap.parse_args(argv)
    cfg = Config.from_env()
    logging.basicConfig(level=logging.INFO, datefmt="%H:%M:%S",
                        format="%(asctime)s %(levelname)s " + ("[%(threadName)s] " if cfg.download_workers > 1 else "") + "%(message)s")
    if a.limit is not None:
        cfg.limit = a.limit
    if a.no_upgrade:
        cfg.upgrade = False
    cfg.state_db.parent.mkdir(parents=True, exist_ok=True)
    st = State(cfg.state_db)
    client = Slskd(cfg.slskd_url, cfg.slskd_api_key, search_gate=SearchGate(cfg.search_gap_s))

    if not a.dry_run:
        if problems := preflight(cfg, client):
            for p in problems:
                log.error("preflight: %s", p)
            return 2
        if gate.os.environ.get("GATE_RULE", "").strip().lower() == gate.NO_GATE:
            log.warning("GATE_RULE=none: lossless copies from Soulseek are NOT checked for transcodes")
        elif not (gate.os.environ.get("FLACCHECK") or shutil.which("flaccheck")):
            log.warning("flaccheck not found: the gate will rely on lossless-verify alone")

    while True:
        try:
            log.info("run finished: %s", run_once(cfg, st, client, dry_run=a.dry_run))
        except Exception:
            log.exception("run failed")
        if a.once or a.dry_run or not cfg.interval_s:
            return 0
        time.sleep(cfg.interval_s)


if __name__ == "__main__":
    raise SystemExit(main())
