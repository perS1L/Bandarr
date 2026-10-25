"""Thin client for the slskd REST API (checked against the 0.26.2 swagger)."""
from __future__ import annotations

import contextlib
import logging
import threading
import time
import uuid
from urllib.parse import quote

import requests

log = logging.getLogger(__name__)

# Transfer states are a flags enum rendered as text, e.g. "Completed, Succeeded".
DONE_OK = "Completed, Succeeded"


def _seg(value) -> str:
    """One URL path segment. Usernames come from other Soulseek users: '/' or '..' must not reshape the URL."""
    return quote(str(value), safe="")


class SlskdError(RuntimeError):
    pass


class SearchGate:
    """One Soulseek search at a time across all workers, with a pause between two searches.

    Downloads run in parallel, searches must not: several workers searching at once would
    multiply the request rate towards the Soulseek server for no benefit (a search takes
    seconds, a download minutes).
    """

    def __init__(self, gap_s: float = 3.0):
        self.gap_s = gap_s
        self._lock = threading.Lock()
        self._last_end = 0.0

    def __enter__(self):
        self._lock.acquire()
        wait = self._last_end + self.gap_s - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        return self

    def __exit__(self, *exc):
        self._last_end = time.monotonic()
        self._lock.release()


class Slskd:
    def __init__(self, url: str, api_key: str, timeout: float = 30, search_gate: SearchGate | None = None):
        self.url, self.api_key = url, api_key
        self.base = f"{url.rstrip('/')}/api/v0"
        self.timeout = timeout
        self.search_gate = search_gate
        self.s = requests.Session()
        self.s.headers["X-API-Key"] = api_key

    def clone(self) -> "Slskd":
        """Another client for another worker thread (a requests Session is not meant to be
        shared), sharing the same search gate."""
        return Slskd(self.url, self.api_key, self.timeout, self.search_gate)

    def _req(self, method: str, path: str, **kw):
        r = self.s.request(method, f"{self.base}{path}", timeout=self.timeout, **kw)
        if r.status_code >= 400:
            raise SlskdError(f"{method} {path} -> HTTP {r.status_code}: {r.text[:200]}")
        return r.json() if r.content else None

    # --- search -------------------------------------------------------------

    def search(
        self,
        text: str,
        timeout_ms: int = 5000,
        response_limit: int = 150,
        poll: float = 1.5,
        max_wait: float = 60,
    ) -> list[dict]:
        """Run a search and return the peer responses (one per user).

        timeout_ms is an inactivity timeout: the search ends that long after the
        last response. The 0.26.2 swagger says "seconds" but it is milliseconds
        (verified: 12 ended the search before any response arrived).
        """
        with self.search_gate or contextlib.nullcontext():
            return self._search(text, timeout_ms, response_limit, poll, max_wait)

    def _search(self, text: str, timeout_ms: int, response_limit: int, poll: float, max_wait: float) -> list[dict]:
        sid = str(uuid.uuid4())
        self._req("POST", "/searches", json={
            "id": sid,
            "searchText": text,
            "searchTimeout": timeout_ms,
            "responseLimit": response_limit,
            "filterResponses": True,
        })
        try:
            deadline = time.monotonic() + max_wait
            while time.monotonic() < deadline:
                time.sleep(poll)
                if self._req("GET", f"/searches/{sid}")["isComplete"]:
                    break
            else:
                self._req("PUT", f"/searches/{sid}")  # stop it, keep what we have
            return self._req("GET", f"/searches/{sid}/responses") or []
        finally:
            try:
                self._req("DELETE", f"/searches/{sid}")
            except SlskdError:
                log.debug("could not delete search %s", sid)

    # --- downloads ----------------------------------------------------------

    def enqueue(self, username: str, files: list[dict], destination: str, external_id: str | None = None) -> dict:
        """Enqueue files (each {filename, size}) from one user. Returns the batch.

        destination is relative to slskd's downloads dir. Raises if any file
        was refused at enqueue time.
        """
        opts = {"destination": destination}
        if external_id:
            opts["externalId"] = external_id
        res = self._req("POST", "/transfers/downloads/batches", json={
            "username": username,
            "files": [{"filename": f["filename"], "size": f["size"]} for f in files],
            "options": opts,
        })
        if res.get("failures"):
            raise SlskdError(f"enqueue refused: {res['failures']}")
        return res["batch"]

    def batch(self, batch_id: str) -> dict:
        return self._req("GET", f"/transfers/downloads/batches/{quote(str(batch_id), safe='')}")

    def queue_position(self, username: str, transfer_id: str) -> int | None:
        """Ask the peer for our place in its queue. None when it does not answer (slskd
        gives up after 5 s with HTTP 500: many clients ignore the request) or gives no number."""
        try:
            r = self._req("GET", f"/transfers/downloads/{_seg(username)}/{_seg(transfer_id)}/position")
        except SlskdError:
            return None
        if isinstance(r, dict):
            r = r.get("placeInQueue")
        return r if isinstance(r, int) else None

    def cancel(self, username: str, transfer_id: str, remove: bool = True) -> None:
        self._req("DELETE", f"/transfers/downloads/{_seg(username)}/{_seg(transfer_id)}", params={"remove": str(remove).lower()})
