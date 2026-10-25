"""Bandcamp wishlist -> flat list of tracks.

Bandcamp sits behind a Fastly JS challenge that blocks plain requests/curl on
TLS fingerprint, so everything goes through curl_cffi impersonating Chrome.
A public wishlist needs no cookie; a private one needs BANDCAMP_IDENTITY.
"""
from __future__ import annotations

import html
import json
import logging
import re
import time
from dataclasses import dataclass, field

from curl_cffi import requests

log = logging.getLogger(__name__)

BASE = "https://bandcamp.com"
WISHLIST_API = f"{BASE}/api/fancollection/1/wishlist_items"
IMPERSONATE = "chrome"
PAGE_SIZE = 20
VARIOUS = {"various artists", "various", "va", "v/a", "v.a."}


class BandcampError(RuntimeError):
    pass


class WishlistItems(list):
    """The wishlist's items, plus whether the whole wishlist was read.

    An incomplete read (pagination cut short) must never be taken for "these albums
    left the wishlist".
    """

    complete: bool = True


@dataclass
class Track:
    wishlist_key: str  # stable id of the track: "<item_id>:<track_id>"
    artist: str  # track artist if Bandcamp gives one, else the album/label artist
    title: str  # raw title as published (may embed "Artist - Title" on V/A)
    album: str
    album_artist: str
    track_num: int | None
    duration: float | None
    item_type: str  # album | track
    url: str
    # Set when the raw title looks like "Artist - Title" and Bandcamp gave no
    # track artist (typical of V/A and label compilations). Search both forms.
    alt_artist: str | None = None
    alt_title: str | None = None
    art_id: str | None = None  # Bandcamp cover id: https://f4.bcbits.com/img/a<art_id>_10.jpg (1200 px)
    extra: dict = field(default_factory=dict)


def _session(identity: str | None) -> requests.Session:
    s = requests.Session(impersonate=IMPERSONATE)
    if identity:
        s.cookies.set("identity", identity, domain=".bandcamp.com")
    return s


def _get(s: requests.Session, url: str, retries: int = 3) -> str:
    for attempt in range(1, retries + 1):
        r = s.get(url, timeout=30)
        if r.status_code == 200 and "<title>Client Challenge" not in r.text:
            return r.text
        log.warning("GET %s -> %s (attempt %d/%d)", url, r.status_code, attempt, retries)
        time.sleep(2 * attempt)
    raise BandcampError(f"cannot fetch {url} (blocked or failing)")


def _attr_json(page: str, attr: str) -> dict:
    m = re.search(rf'{attr}="([^"]*)"', page)
    if not m:
        raise BandcampError(f"{attr} not found in page")
    return json.loads(html.unescape(m.group(1)))


def fetch_wishlist_items(user: str, identity: str | None = None) -> list[dict]:
    """All wishlist items (albums and tracks), newest first."""
    s = _session(identity)
    blob = _attr_json(_get(s, f"{BASE}/{user}/wishlist"), "data-blob")

    fan_id = blob.get("fan_data", {}).get("fan_id")
    wl = blob.get("wishlist_data") or {}
    if wl.get("hidden") and not identity:
        raise BandcampError("wishlist is private: set BANDCAMP_IDENTITY")
    if not fan_id:
        raise BandcampError("fan_id not found (private wishlist or layout change)")
    if "wishlist_data" not in blob:  # an empty wishlist says so (item_count 0); a missing block is a changed page
        raise BandcampError("wishlist_data missing from the page (layout change?)")

    cache = blob.get("item_cache", {}).get("wishlist", {})
    items = [cache[k] for k in wl.get("sequence", []) if k in cache]
    total, token = wl.get("item_count", len(items)), wl.get("last_token")

    while len(items) < total and token:
        r = s.post(
            WISHLIST_API,
            json={"fan_id": fan_id, "older_than_token": token, "count": PAGE_SIZE},
            timeout=30,
        )
        if r.status_code != 200:
            raise BandcampError(f"wishlist_items -> HTTP {r.status_code}")
        page = r.json()
        batch = page.get("items", [])
        items += batch
        token = page.get("last_token")
        if not batch or not page.get("more_available"):
            break
        time.sleep(1)

    result = WishlistItems(items)
    result.complete = len(items) >= total
    if not result.complete:
        log.warning("wishlist: got %d of %d items", len(items), total)
    return result


_VA_SPLIT = re.compile(r"^(.+?)\s+-\s+(.+)$")


def _tracks_of(item: dict, tralbum: dict) -> list[Track]:
    cur = tralbum.get("current", {})
    album = cur.get("title") or item.get("item_title", "")
    album_artist = tralbum.get("artist") or item.get("band_name", "")
    tracks = []
    for t in tralbum.get("trackinfo", []):
        raw = (t.get("title") or "").strip()
        if not raw:
            continue
        own_artist = (t.get("artist") or "").strip()
        alt_artist = alt_title = None
        if not own_artist and (m := _VA_SPLIT.match(raw)):
            alt_artist, alt_title = m[1].strip(), m[2].strip()
        tid = t.get("track_id") or t.get("id")
        tracks.append(
            Track(
                wishlist_key=f"{item.get('item_id')}:{tid}",
                artist=own_artist or album_artist,
                title=raw,
                album=album,
                album_artist=album_artist,
                track_num=t.get("track_num"),
                duration=t.get("duration"),
                item_type=item.get("item_type", "album"),
                url=item.get("item_url", ""),
                alt_artist=alt_artist,
                alt_title=alt_title,
                art_id=str(item.get("item_art_id") or tralbum.get("art_id") or "") or None,
                extra={
                    "streamable": bool(t.get("file")),
                    "unreleased": bool(t.get("unreleased_track")),
                },
            )
        )
    return tracks


def fetch_tracks(
    user: str,
    identity: str | None = None,
    delay: float = 1.0,
    known_item_ids: set[str] | None = None,
    items: list[dict] | None = None,
) -> tuple[list[Track], list[dict]]:
    """Expand wishlist items to individual tracks.

    Items whose item_id is in known_item_ids (already expanded on a previous
    run) are skipped, so only new items cost a page fetch. Returns
    (tracks, failed_items). A failing album never aborts the run: it is
    returned in failed_items, and since the caller never records it as known,
    it is retried on the next run.
    """
    known = known_item_ids or set()
    items = [i for i in (items if items is not None else fetch_wishlist_items(user, identity))
             if str(i.get("item_id")) not in known]
    log.info("%d new wishlist items to expand (%d known)", len(items), len(known))
    s = _session(identity)
    tracks: list[Track] = []
    failed: list[dict] = []
    for i, item in enumerate(items):
        url = item.get("item_url")
        try:
            tralbum = _attr_json(_get(s, url), "data-tralbum")
            got = _tracks_of(item, tralbum)
            if not got:
                raise BandcampError("no tracks in trackinfo")
            tracks += got
        except Exception as e:  # noqa: BLE001 - keep going, report below
            log.error("skip %s: %s", url, e)
            failed.append(item)
        if i < len(items) - 1:
            time.sleep(delay)
    return tracks, failed
