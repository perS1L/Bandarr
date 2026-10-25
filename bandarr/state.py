"""SQLite state store: one row per wishlist track (see CLAUDE.md section 6)."""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from .bandcamp import Track
from .scoring import norm

STATUSES = {"pending", "downloaded", "in_review", "done", "not_found", "duplicate"}
SOURCES = {"bandcamp", "soulseek"}
FORMATS = {"flac", "aiff", "mp3"}
VERDICTS = {"pass", "suspect", "fail", "n/a"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    item_id      TEXT PRIMARY KEY,
    expanded_at  REAL NOT NULL,
    on_wishlist  INTEGER NOT NULL DEFAULT 1,  -- 0: no longer on the wishlist (bought, removed, other account)
    last_seen_at REAL
);
CREATE TABLE IF NOT EXISTS tracks (
    wishlist_key TEXT PRIMARY KEY,          -- "<item_id>:<track_id>"
    item_id      TEXT NOT NULL REFERENCES items(item_id),
    artist       TEXT NOT NULL,
    title        TEXT NOT NULL,
    album        TEXT NOT NULL,
    album_artist TEXT NOT NULL,
    track_num    INTEGER,
    duration     REAL,
    alt_artist   TEXT,
    alt_title    TEXT,
    url          TEXT,
    art_id       TEXT,
    status       TEXT NOT NULL DEFAULT 'pending',
    source       TEXT,
    format       TEXT,
    gate_verdict TEXT,
    path         TEXT,
    duplicate_of TEXT,                      -- status 'duplicate': key of the row that holds the same track
    updated_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS tracks_status ON tracks(status);

-- Raw verdicts of both gate detectors for every Soulseek lossless file, kept so the
-- decision rule can be compared on real data once files are labelled by ear/spectrogram.
CREATE TABLE IF NOT EXISTS gate_checks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    wishlist_key  TEXT NOT NULL REFERENCES tracks(wishlist_key),
    path          TEXT,
    lv_verdict    TEXT,          -- PASS | SUSPECT | FAIL | UNKNOWN
    lv_cutoff_hz  REAL,
    lv_drop_db    REAL,
    sample_rate   INTEGER,
    fc_verdict    TEXT,          -- GENUINE | SUSPICIOUS | TRANSCODED | INCONCLUSIVE | NULL
    fc_confidence REAL,
    rule          TEXT,
    decision      TEXT,          -- gate verdict applied: pass | suspect | fail
    label         TEXT,          -- ground truth set later: genuine | fake
    checked_at    REAL NOT NULL,
    peer          TEXT,          -- Soulseek user the copy came from
    remote_dir    TEXT,          -- its folder on that user's disk (lower case): the "album" of the copy
    review_path   TEXT,          -- where the copy sits once refused
    consensus     TEXT           -- set when accepted because the rest of its folder passed, e.g. "5/7"
);

-- Lossless transfers left waiting in a peer's queue (Soulseek queues can last hours).
-- A later run collects them when they finish.
CREATE TABLE IF NOT EXISTS queued_transfers (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    wishlist_key TEXT NOT NULL REFERENCES tracks(wishlist_key),
    batch_id     TEXT NOT NULL,
    username     TEXT NOT NULL,
    filename     TEXT NOT NULL,
    size         INTEGER,
    dest         TEXT NOT NULL,   -- download sub-folder, relative to slskd's downloads dir
    enqueued_at  REAL NOT NULL
);

-- Every source already tried for a track, so a later search never repeats it.
CREATE TABLE IF NOT EXISTS source_log (
    wishlist_key TEXT NOT NULL,
    username     TEXT NOT NULL,
    filename     TEXT NOT NULL,
    outcome      TEXT NOT NULL,   -- refused | failed | queued
    at           REAL NOT NULL,
    PRIMARY KEY (wishlist_key, username, filename)
);
"""


def dedup_key(row) -> tuple[str, str]:
    """Identity of a track across wishlist items: the same song can be on the wishlist as part
    of an album and again as a single track (different item ids, different wishlist keys)."""
    return (norm(row["alt_artist"] or row["artist"]), norm(row["alt_title"] or row["title"]))


class ProtectedTrack(RuntimeError):
    """Raised when something tries to overwrite a source=bandcamp track."""


class State:
    def __init__(self, path: str | Path, handoff: bool = False):
        self.path = path
        # timeout: several workers (one connection each) may write; a lock is waited for, not an error.
        # handoff: the connection is created by one thread and handed to a worker. Safe because the
        # worker pool gives each connection to a single thread at a time and never shares it.
        self.db = sqlite3.connect(path, timeout=60, check_same_thread=not handoff)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(SCHEMA)
        if "art_id" not in {r[1] for r in self.db.execute("PRAGMA table_info(tracks)")}:
            self.db.execute("ALTER TABLE tracks ADD COLUMN art_id TEXT")  # database from before covers
        have_items = {r[1] for r in self.db.execute("PRAGMA table_info(items)")}
        for col, decl in (("on_wishlist", "INTEGER NOT NULL DEFAULT 1"), ("last_seen_at", "REAL")):
            if col not in have_items:  # databases from before wishlist membership was tracked
                self.db.execute(f"ALTER TABLE items ADD COLUMN {col} {decl}")
        have = {r[1] for r in self.db.execute("PRAGMA table_info(tracks)")}
        for col, decl in (("upgrade_checked_at", "REAL"), ("retry_count", "INTEGER NOT NULL DEFAULT 0"),
                          ("retry_after", "REAL"), ("duplicate_of", "TEXT")):  # databases created before these columns
            if col not in have:
                self.db.execute(f"ALTER TABLE tracks ADD COLUMN {col} {decl}")
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(gate_checks)")}
        if "sample_rate" not in cols:  # database created before this column existed
            self.db.execute("ALTER TABLE gate_checks ADD COLUMN sample_rate INTEGER")
        for col in ("peer", "remote_dir", "review_path", "consensus"):  # added with the album consensus
            if col not in cols:
                self.db.execute(f"ALTER TABLE gate_checks ADD COLUMN {col} TEXT")

    def close(self) -> None:
        self.db.close()

    def clone(self) -> "State":
        """Another connection to the same database, for another worker thread (a sqlite3
        connection must not be shared between threads)."""
        return State(self.path, handoff=True)

    # --- wishlist expansion -------------------------------------------------

    def known_item_ids(self) -> set[str]:
        return {r[0] for r in self.db.execute("SELECT item_id FROM items")}

    def add_tracks(self, tracks: list[Track]) -> int:
        """Record newly expanded tracks as pending. Idempotent. Returns rows added."""
        now, added = time.time(), 0
        with self.db:
            for t in tracks:
                item_id = t.wishlist_key.split(":", 1)[0]
                self.db.execute(
                    "INSERT OR IGNORE INTO items(item_id, expanded_at) VALUES (?, ?)",
                    (item_id, now),
                )
                cur = self.db.execute(
                    """INSERT OR IGNORE INTO tracks
                       (wishlist_key, item_id, artist, title, album, album_artist,
                        track_num, duration, alt_artist, alt_title, url, art_id, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (t.wishlist_key, item_id, t.artist, t.title, t.album, t.album_artist,
                     t.track_num, t.duration, t.alt_artist, t.alt_title, t.url, t.art_id, now),
                )
                added += cur.rowcount
        return added

    def set_art_ids(self, art_by_item: dict[str, str]) -> None:
        """Fill in cover ids for tracks stored before covers were supported."""
        with self.db:
            for item_id, art in art_by_item.items():
                if art:
                    self.db.execute("UPDATE tracks SET art_id = ? WHERE item_id = ? AND art_id IS NULL",
                                    (str(art), str(item_id)))

    # --- pipeline transitions -----------------------------------------------

    def update(
        self,
        key: str,
        *,
        status: str | None = None,
        source: str | None = None,
        format: str | None = None,
        gate_verdict: str | None = None,
        path: str | None = None,
    ) -> None:
        """Update a track. Only the given fields change.

        A track already at source=bandcamp can only be rewritten by another
        bandcamp import (never replaced by a Soulseek copy).
        """
        for value, allowed, name in (
            (status, STATUSES, "status"), (source, SOURCES, "source"),
            (format, FORMATS, "format"), (gate_verdict, VERDICTS, "gate_verdict"),
        ):
            if value is not None and value not in allowed:
                raise ValueError(f"invalid {name}: {value!r}")

        row = self.db.execute(
            "SELECT source FROM tracks WHERE wishlist_key = ?", (key,)
        ).fetchone()
        if row is None:
            raise KeyError(key)
        if row["source"] == "bandcamp" and source != "bandcamp":
            raise ProtectedTrack(f"{key} is source=bandcamp and cannot be replaced")

        fields = {"status": status, "source": source, "format": format,
                  "gate_verdict": gate_verdict, "path": path}
        sets = {k: v for k, v in fields.items() if v is not None}
        sets["updated_at"] = time.time()
        cols = ", ".join(f"{k} = ?" for k in sets)
        with self.db:
            self.db.execute(
                f"UPDATE tracks SET {cols} WHERE wishlist_key = ?", (*sets.values(), key)
            )

    # --- upgrade loop: queued transfers, tried sources ---------------------------

    def get_track(self, key: str):
        return self.db.execute("SELECT * FROM tracks WHERE wishlist_key = ?", (key,)).fetchone()

    def add_queued(self, key: str, batch_id: str, username: str, filename: str, size: int, dest: str) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO queued_transfers (wishlist_key, batch_id, username, filename, size, dest, enqueued_at)"
                " VALUES (?,?,?,?,?,?,?)", (key, batch_id, username, filename, size, dest, time.time()))

    def queued(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM queued_transfers ORDER BY id").fetchall()

    def queued_keys(self) -> set[str]:
        return {r[0] for r in self.db.execute("SELECT DISTINCT wishlist_key FROM queued_transfers")}

    def queued_dests(self, key: str) -> set[str]:
        return {r[0] for r in self.db.execute("SELECT dest FROM queued_transfers WHERE wishlist_key = ?", (key,))}

    def drop_queued(self, queued_id: int) -> None:
        with self.db:
            self.db.execute("DELETE FROM queued_transfers WHERE id = ?", (queued_id,))

    def log_source(self, key: str, username: str, filename: str, outcome: str) -> None:
        if outcome not in ("refused", "failed", "queued"):
            raise ValueError(outcome)
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO source_log VALUES (?,?,?,?,?)",
                            (key, username, filename, outcome, time.time()))

    def tried_sources(self, key: str, failed_ttl_days: float = 7) -> set[tuple[str, str]]:
        """(username, filename) pairs not to try again: refused and queued ones for good,
        failed ones only for failed_ttl_days (peers come back online, bans get lifted)."""
        cutoff = time.time() - failed_ttl_days * 86400
        rows = self.db.execute(
            "SELECT username, filename FROM source_log WHERE wishlist_key = ?"
            " AND (outcome != 'failed' OR at > ?)", (key, cutoff))
        return {(r[0], r[1]) for r in rows}

    def mark_upgrade_checked(self, key: str) -> None:
        with self.db:
            self.db.execute("UPDATE tracks SET upgrade_checked_at = ? WHERE wishlist_key = ?", (time.time(), key))

    def upgrade_due(self, interval_h: float, limit: int) -> list[sqlite3.Row]:
        """Soulseek MP3 fallbacks in master that were not searched for lossless within
        interval_h hours and have no transfer waiting. Never source=bandcamp."""
        cutoff = time.time() - interval_h * 3600
        return self.db.execute(
            """SELECT t.* FROM tracks t JOIN items i USING (item_id)
               WHERE t.status = 'done' AND t.source = 'soulseek' AND t.format = 'mp3'
                 AND i.on_wishlist = 1
                 AND (t.upgrade_checked_at IS NULL OR t.upgrade_checked_at < ?)
                 AND t.wishlist_key NOT IN (SELECT wishlist_key FROM queued_transfers)
               ORDER BY COALESCE(t.upgrade_checked_at, 0), t.item_id, t.track_num LIMIT ?""",
            (cutoff, limit)).fetchall()

    def requeue_stale_not_found(self, interval_h: float) -> int:
        """not_found tracks last searched more than interval_h hours ago go back to pending."""
        cutoff = time.time() - interval_h * 3600
        with self.db:
            cur = self.db.execute(
                """UPDATE tracks SET status = 'pending'
                   WHERE status = 'not_found' AND (source IS NULL OR source != 'bandcamp')
                     AND (upgrade_checked_at IS NULL OR upgrade_checked_at < ?)""", (cutoff,))
        return cur.rowcount

    # --- gate measurements ------------------------------------------------------

    def record_gate(self, key: str, path: str, g, peer: str | None = None, remote_dir: str | None = None) -> int:
        """Store both detectors' raw verdicts (g is a gate.GateResult). Returns row id."""
        with self.db:
            cur = self.db.execute(
                """INSERT INTO gate_checks (wishlist_key, path, lv_verdict, lv_cutoff_hz, lv_drop_db, sample_rate,
                       fc_verdict, fc_confidence, rule, decision, checked_at, peer, remote_dir)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (key, path, g.lv_verdict, g.cutoff_hz, g.max_drop_db, g.sample_rate, g.fc_verdict,
                 g.fc_confidence, g.rule, g.verdict, time.time(), peer, remote_dir),
            )
        return cur.lastrowid

    def set_gate_review(self, check_id: int, review_path: str) -> None:
        with self.db:
            self.db.execute("UPDATE gate_checks SET review_path = ? WHERE id = ?", (review_path, check_id))

    def set_gate_consensus(self, check_id: int, text: str) -> None:
        with self.db:
            self.db.execute("UPDATE gate_checks SET consensus = ? WHERE id = ?", (text, check_id))

    def folder_votes(self, peer: str, remote_dir: str, key: str) -> tuple[int, int]:
        """(tracks that passed the gate, tracks checked) among the OTHER tracks of one peer's folder.

        Only the gate's own PASS counts: a copy accepted by consensus or by hand is not a witness
        (otherwise one decision would feed the next). A track later labelled fake does not count.
        """
        rows = self.db.execute(
            """SELECT wishlist_key, MAX(decision = 'pass' AND COALESCE(label, '') != 'fake') AS ok
               FROM gate_checks WHERE peer = ? AND remote_dir = ? AND wishlist_key != ?
               GROUP BY wishlist_key""", (peer, remote_dir, key)).fetchall()
        return sum(r["ok"] for r in rows), len(rows)

    def consensus_candidates(self):
        """Refused copies kept in review that may still be rescued by their folder's other tracks."""
        return self.db.execute(
            """SELECT * FROM gate_checks WHERE decision IN ('suspect', 'fail') AND review_path IS NOT NULL
                 AND label IS NULL AND consensus IS NULL AND peer IS NOT NULL AND remote_dir IS NOT NULL
               ORDER BY id""").fetchall()

    def label_gate(self, check_id: int, label: str) -> None:
        """Set the ground truth of a checked file: 'genuine' or 'fake'."""
        if label not in ("genuine", "fake"):
            raise ValueError("label must be 'genuine' or 'fake'")
        with self.db:
            if self.db.execute("UPDATE gate_checks SET label = ? WHERE id = ?", (label, check_id)).rowcount == 0:
                raise KeyError(check_id)

    def gate_report(self) -> dict[str, dict[str, int]]:
        """For each decision rule, how it would have done on the labelled files:
        {'rule': {'fake_caught', 'fake_missed', 'genuine_refused', 'genuine_passed'}}."""
        from .gate import RULES, decide, effective_lv  # local import: gate needs lossless_verify

        rows = self.db.execute(
            "SELECT lv_verdict, lv_cutoff_hz, sample_rate, fc_verdict, label FROM gate_checks WHERE label IS NOT NULL").fetchall()
        out = {}
        for rule in RULES:
            c = {"fake_caught": 0, "fake_missed": 0, "genuine_refused": 0, "genuine_passed": 0}
            for r in rows:
                lv = effective_lv(r["lv_verdict"] or "UNKNOWN", r["lv_cutoff_hz"] or 0, r["sample_rate"] or 0)
                refused = decide(lv, r["fc_verdict"], rule)
                if r["label"] == "fake":
                    c["fake_caught" if refused else "fake_missed"] += 1
                else:
                    c["genuine_refused" if refused else "genuine_passed"] += 1
            out[rule] = c
        return out

    # --- queries --------------------------------------------------------------

    def by_status(self, *statuses: str) -> list[sqlite3.Row]:
        marks = ",".join("?" * len(statuses))
        return self.db.execute(
            f"SELECT * FROM tracks WHERE status IN ({marks}) ORDER BY item_id, track_num",
            statuses,
        ).fetchall()

    def sync_wishlist(self, present: set[str]) -> tuple[int, int]:
        """Record which known albums are on the wishlist now. Returns (left, came_back).

        Nothing is deleted: a track of an album that left the wishlist keeps its state, and
        is processed again if the album returns.
        """
        now, left, back = time.time(), 0, 0
        with self.db:
            for r in self.db.execute("SELECT item_id, on_wishlist FROM items").fetchall():
                if r["item_id"] in present:
                    back += not r["on_wishlist"]
                    self.db.execute("UPDATE items SET on_wishlist = 1, last_seen_at = ? WHERE item_id = ?",
                                    (now, r["item_id"]))
                elif r["on_wishlist"]:
                    left += 1
                    self.db.execute("UPDATE items SET on_wishlist = 0 WHERE item_id = ?", (r["item_id"],))
        return left, back

    def is_on_wishlist(self, item_id: str) -> bool:
        r = self.db.execute("SELECT on_wishlist FROM items WHERE item_id = ?", (item_id,)).fetchone()
        return bool(r is None or r[0])

    def off_wishlist_pending(self) -> int:
        """Pending tracks of albums that are no longer on the wishlist (skipped)."""
        return self.db.execute("SELECT COUNT(*) FROM tracks t JOIN items i USING (item_id)"
                               " WHERE t.status = 'pending' AND i.on_wishlist = 0").fetchone()[0]

    def to_fetch(self, now: float | None = None) -> list[sqlite3.Row]:
        """Pending tracks of wishlist albums that are due: not deferred by a retry delay."""
        return self.db.execute(
            "SELECT t.* FROM tracks t JOIN items i USING (item_id)"
            " WHERE t.status = 'pending' AND i.on_wishlist = 1 AND (t.retry_after IS NULL OR t.retry_after <= ?)"
            " ORDER BY t.item_id, t.track_num", (now or time.time(),)).fetchall()

    def mark_duplicate(self, key: str, of: str) -> None:
        """The track is the same song as row `of`: nothing is fetched for it, nothing upgraded."""
        with self.db:
            self.db.execute("UPDATE tracks SET status = 'duplicate', duplicate_of = ?, path = NULL, updated_at = ?"
                            " WHERE wishlist_key = ? AND (source IS NULL OR source != 'bandcamp')",
                            (of, time.time(), key))

    def mark_duplicates(self) -> int:
        """Pending tracks whose song is already done under another wishlist key become duplicates."""
        done = {}
        for r in self.db.execute("SELECT * FROM tracks WHERE status = 'done' AND path IS NOT NULL ORDER BY updated_at"):
            done.setdefault(dedup_key(r), r["wishlist_key"])
        n = 0
        for r in self.db.execute("SELECT * FROM tracks WHERE status = 'pending'").fetchall():
            of = done.get(dedup_key(r))
            if of and of != r["wishlist_key"]:
                self.mark_duplicate(r["wishlist_key"], of)
                n += 1
        return n

    def deferred_count(self, now: float | None = None) -> int:
        """Pending wishlist tracks held back by a retry delay."""
        return self.db.execute(
            "SELECT COUNT(*) FROM tracks t JOIN items i USING (item_id)"
            " WHERE t.status = 'pending' AND i.on_wishlist = 1 AND t.retry_after > ?",
            (now or time.time(),)).fetchone()[0]

    def schedule_retry(self, key: str, base_h: float, max_h: float, now: float | None = None) -> float:
        """A track failed without result: do not retry it before base_h * 2**n hours (n = failures
        so far), capped at max_h. Returns the delay in hours."""
        row = self.get_track(key)
        n = (row["retry_count"] or 0) if row else 0
        delay_h = min(base_h * 2 ** n, max_h)
        with self.db:
            self.db.execute("UPDATE tracks SET retry_count = ?, retry_after = ? WHERE wishlist_key = ?",
                            (n + 1, (now or time.time()) + delay_h * 3600, key))
        return delay_h

    def clear_retry(self, key: str) -> None:
        with self.db:
            self.db.execute("UPDATE tracks SET retry_count = 0, retry_after = NULL WHERE wishlist_key = ?"
                            " AND (retry_count != 0 OR retry_after IS NOT NULL)", (key,))

    def upgrade_candidates(self) -> list[sqlite3.Row]:
        """Everything usable that is not source=bandcamp (CLAUDE.md section 7):
        Soulseek copies (MP3 first) and not_found tracks, to retry as FLAC."""
        return self.db.execute(
            """SELECT * FROM tracks
               WHERE (source IS NULL OR source != 'bandcamp')
                 AND status IN ('done', 'not_found', 'in_review')
               ORDER BY (format = 'mp3') DESC, item_id, track_num"""
        ).fetchall()

    def summary(self) -> dict[str, int]:
        return {r[0]: r[1] for r in self.db.execute(
            "SELECT status, COUNT(*) FROM tracks GROUP BY status")}
