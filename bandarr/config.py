"""Runtime configuration, read from environment variables (see .env.example).

Paths are made absolute: external tools (ffmpeg in a container, say) may not share our cwd."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Config:
    bandcamp_user: str
    bandcamp_identity: str | None
    slskd_url: str
    slskd_api_key: str
    downloads_dir: Path  # slskd's download dir, as seen from this process
    master_dir: Path  # flat folder of finished tracks, imported into rekordbox
    review_dir: Path  # refused files (gate), never deleted
    state_db: Path
    limit: int | None = None  # max tracks processed per run (None = all)
    keep_originals: bool = False  # keep the intermediate FLAC after a successful AIFF
    retry_not_found: bool = False  # put not_found tracks back to pending at start of run
    interval_s: int | None = None  # None = run once
    max_source_tries: int = 3  # different peers tried when a copy is refused by the gate
    covers_dir: Path = Path("data/covers")
    # --- upgrade loop (CLAUDE.md section 7): replace Soulseek MP3 fallbacks by lossless ---
    upgrade: bool = True
    upgrade_limit: int = 10  # tracks searched per run
    upgrade_interval_h: float = 24  # do not search the same track again within this time
    blocked_peers: frozenset = frozenset()  # Soulseek users never used as a source (lower case)
    not_found_retry_h: float = 7  # a track not found is searched again after this long (not a divisor of 24: shifts the hour)
    upgrade_queue_timeout_s: float = 30  # how long an upgrade waits for a transfer to start
    consensus_min: int = 3  # passed tracks in the same peer folder that rescue a refused one (0: off)
    queue_max_age_days: float = 7  # a transfer waiting longer than this is given up on
    queue_timeout_s: float = 180  # first-pass wait before falling back to another source
    replaced_dir: Path = Path("data/replaced")  # replaced files are moved here, not deleted
    originals_dir: Path = Path("data/originals")  # intermediate FLACs when keep_originals
    delete_replaced: bool = False  # True: delete instead (rekordbox cues are tied to the file)
    retry_base_h: float = 1  # a track that failed without result waits base * 2**failures hours...
    retry_max_h: float = 24  # ...at most this long
    download_workers: int = 3  # tracks handled in parallel (downloads overlap; searches stay one at a time)
    search_gap_s: float = 3  # minimum pause between two Soulseek searches, across all workers

    @classmethod
    def from_env(cls, env=os.environ) -> "Config":
        def need(k: str) -> str:
            v = env.get(k, "").strip()
            if not v:
                raise SystemExit(f"missing environment variable {k} (see .env.example)")
            return v

        data = Path(env.get("DATA_DIR", "data"))
        return cls(
            bandcamp_user=need("BANDCAMP_USER"),
            bandcamp_identity=env.get("BANDCAMP_IDENTITY") or None,
            slskd_url=env.get("SLSKD_URL", "http://127.0.0.1:5030"),
            slskd_api_key=need("SLSKD_API_KEY"),
            downloads_dir=Path(env.get("DOWNLOADS_DIR", data / "slskd" / "downloads")).resolve(),
            master_dir=Path(env.get("MASTER_DIR", data / "master")).resolve(),
            review_dir=Path(env.get("REVIEW_DIR", data / "review")).resolve(),
            state_db=Path(env.get("STATE_DB", data / "state.db")).resolve(),
            limit=int(env["SYNC_LIMIT"]) if env.get("SYNC_LIMIT") else None,
            keep_originals=env.get("KEEP_ORIGINALS", "").lower() in ("1", "true", "yes"),
            retry_not_found=env.get("RETRY_NOT_FOUND", "").lower() in ("1", "true", "yes"),
            interval_s=int(env["SCRIPT_INTERVAL"]) if env.get("SCRIPT_INTERVAL") else None,
            max_source_tries=int(env.get("MAX_SOURCE_TRIES", 3)),
            covers_dir=Path(env.get("COVERS_DIR", data / "covers")).resolve(),
            upgrade=env.get("UPGRADE", "1").lower() in ("1", "true", "yes"),
            upgrade_limit=int(env.get("UPGRADE_LIMIT", 10)),
            upgrade_interval_h=float(env.get("UPGRADE_INTERVAL_H", 24)),
            not_found_retry_h=float(env.get("NOT_FOUND_RETRY_H", 7)),
            blocked_peers=frozenset(p.strip().lower() for p in env.get("BLOCKED_PEERS", "").split(",") if p.strip()),
            upgrade_queue_timeout_s=float(env.get("UPGRADE_QUEUE_TIMEOUT_S", 30)),
            consensus_min=int(env.get("CONSENSUS_MIN", 3)),
            queue_max_age_days=float(env.get("QUEUE_MAX_AGE_DAYS", 7)),
            queue_timeout_s=float(env.get("QUEUE_TIMEOUT_S", 180)),
            replaced_dir=Path(env.get("REPLACED_DIR", data / "replaced")).resolve(),
            originals_dir=Path(env.get("ORIGINALS_DIR", data / "originals")).resolve(),
            delete_replaced=env.get("DELETE_REPLACED", "").lower() in ("1", "true", "yes"),
            retry_base_h=float(env.get("RETRY_BASE_H", 1)),
            retry_max_h=float(env.get("RETRY_MAX_H", 24)),
            download_workers=max(1, int(env.get("DOWNLOAD_WORKERS", 3))),
            search_gap_s=float(env.get("SEARCH_GAP_S", 3)),
        )
