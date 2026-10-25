"""Cover art from Bandcamp, cached on disk (one download per album)."""
from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

from curl_cffi import requests

log = logging.getLogger(__name__)
_LOCK = threading.Lock()  # tracks of one album fetch the same cover at the same time
URL = "https://f4.bcbits.com/img/a{art_id}_10.jpg"  # _10 = 1200x1200 JPEG (~220 KB)


def get_cover(art_id: str | None, cache_dir: Path) -> bytes | None:
    """JPEG bytes of an album cover, or None. Never raises: a missing cover must not
    fail a track."""
    if not art_id:
        return None
    cache = Path(cache_dir) / f"{art_id}.jpg"
    try:
        with _LOCK:  # one download per album; the others read the finished file
            if cache.exists() and cache.stat().st_size > 0:
                return cache.read_bytes()
            r = requests.get(URL.format(art_id=art_id), impersonate="chrome", timeout=30)
            if r.status_code != 200 or not r.content.startswith(b"\xff\xd8"):
                log.warning("cover %s: HTTP %s or not a JPEG", art_id, r.status_code)
                return None
            cache.parent.mkdir(parents=True, exist_ok=True)
            tmp = cache.with_name(cache.name + ".part")
            tmp.write_bytes(r.content)
            os.replace(tmp, cache)  # atomic: a reader never sees half a file
            return r.content
    except Exception as e:  # noqa: BLE001 - network, disk, anything
        log.warning("cover %s unavailable: %s", art_id, e)
        return None
