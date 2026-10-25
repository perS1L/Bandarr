"""Rank slskd search results for one wanted track (CLAUDE.md sections 5.4, 8).

Pure functions, no network. Ranking is format tier first (lossless > MP3 320 >
other MP3), then how well the file matches the wanted track, then how likely
the download is to actually start (free slot, queue, speed).
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

LOSSLESS = {"flac", "aiff", "aif"}
LOSSY = {"mp3"}
LOSSLESS_TIER = 3
MIN_LOSSY_KBPS = 320  # floor; applies to lossy ONLY (lossless carries no bitrate)
# Pioneer XDJ-700 plays AIFF 16/24-bit at 44.1/48 kHz and slows searching on VBR MP3
# (manual). The AIFF hook keeps 24-bit, so it is worth a small preference; CBR too.
HI_RES_BIT_DEPTH = 24
HI_RES_BONUS = 8  # outweighs availability (max ~9 only when everything else ties)
CBR_BONUS = 4

# Words that mark a different version of a track. Penalised when the file has
# one that the wanted title does not.
VARIANT_WORDS = {
    "remix", "edit", "vip", "dub", "instrumental", "acapella", "acappella", "live",
    "bootleg", "rework", "version", "cover", "remaster", "remastered", "radio",
    "extended", "mix", "mixed", "demo", "reprise", "interlude", "skit",
}

_FEAT_PAREN = re.compile(r"[\(\[]\s*(?:feat|ft|featuring)\b[^\)\]]*[\)\]]", re.I)
_ORIGINAL = re.compile(r"[\(\[]?\s*\boriginal (?:mix|version)\b\s*[\)\]]?", re.I)
_ARTIST_SPLIT = re.compile(r"\s*(?:,|&|\bx\b|\band\b|\bfeat\.?\b|\bft\.?\b|\bvs\.?\b|/)\s*", re.I)


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c)).lower().replace("&", " and ")
    return " ".join(re.sub(r"[\W_]+", " ", s).split())


def title_forms(title: str) -> list[str]:
    """Normalised forms of a title we accept: as published, and without
    '(feat. X)' / '(Original Mix)' decorations that files often drop."""
    forms = [norm(title)]
    bare = norm(_ORIGINAL.sub("", _FEAT_PAREN.sub("", title)))
    if bare and bare not in forms:
        forms.append(bare)
    return [f for f in forms if f]


def artist_parts(artist: str) -> list[str]:
    parts = [norm(p) for p in _ARTIST_SPLIT.split(artist) if norm(p)]
    full = norm(artist)
    return ([full] if full else []) + [p for p in parts if p != full]


def _has(haystack: str, needle: str) -> bool:
    return bool(needle) and f" {needle} " in f" {haystack} "


@dataclass
class Wanted:
    artist: str
    title: str
    duration: float | None = None
    alt_artist: str | None = None
    alt_title: str | None = None

    @classmethod
    def from_mapping(cls, m) -> "Wanted":
        """Build from a Track dict or a sqlite3.Row."""
        keys = m.keys()
        return cls(
            artist=m["artist"], title=m["title"],
            duration=m["duration"] if "duration" in keys else None,
            alt_artist=m["alt_artist"] if "alt_artist" in keys else None,
            alt_title=m["alt_title"] if "alt_title" in keys else None,
        )

    def variants(self) -> list[tuple[str, str]]:
        v = [(self.artist, self.title)]
        if self.alt_artist and self.alt_title:
            v.append((self.alt_artist, self.alt_title))
        return v


@dataclass
class Candidate:
    username: str
    filename: str  # raw remote path, as slskd needs it for the download
    size: int
    ext: str
    bitrate: int | None
    vbr: bool
    bit_depth: int | None
    sample_rate: int | None
    length: float | None
    free_slot: bool
    queue: int
    speed: float

    @property
    def basename(self) -> str:
        return self.filename.replace("\\", "/").rsplit("/", 1)[-1]

    @property
    def folders(self) -> str:
        return self.filename.replace("\\", "/").rsplit("/", 1)[0] if "/" in self.filename.replace("\\", "/") else ""

    @property
    def is_lossless(self) -> bool:
        return self.ext in LOSSLESS


@dataclass
class Scored:
    candidate: Candidate
    score: float
    tier: int
    reasons: list[str] = field(default_factory=list)


def candidates(responses: list[dict]) -> list[Candidate]:
    out = []
    for r in responses:
        for f in r.get("files", []):
            if f.get("isLocked"):
                continue
            name = f["filename"]
            ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
            out.append(Candidate(
                username=r["username"], filename=name, size=f.get("size", 0), ext=ext,
                bitrate=f.get("bitRate"), vbr=bool(f.get("isVariableBitRate")),
                bit_depth=f.get("bitDepth"), sample_rate=f.get("sampleRate"),
                length=f.get("length"),
                free_slot=bool(r.get("hasFreeUploadSlot")),
                queue=r.get("queueLength") or 0, speed=r.get("uploadSpeed") or 0,
            ))
    return out


def _tier(c: Candidate) -> int | None:
    """Format tier, or None when the file is excluded by format/quality."""
    if c.ext in LOSSLESS:
        return LOSSLESS_TIER  # never subject to the bitrate floor: FLAC has no bitrate on Soulseek
    if c.ext in LOSSY:
        if c.bitrate is None or c.bitrate == 0:
            return 1  # unknown bitrate is kept (not rejected), ranked below verified 320
        return 2 if c.bitrate >= MIN_LOSSY_KBPS else None
    return None  # non-audio or formats we do not take (m4a, ogg, wav, jpg, ...)


def _match(c: Candidate, w: Wanted) -> tuple[float, list[str]] | None:
    """Match quality 0..~50 and reasons, or None when it is not this track."""
    base = norm(c.basename.rsplit(".", 1)[0])
    path = norm(c.folders + " " + c.basename.rsplit(".", 1)[0])
    best = None
    for artist, title in w.variants():
        forms = title_forms(title)
        form = next((f for f in forms if _has(base, f)), None)
        if form is None:
            continue
        pts, why = 0.0, ["title"]
        artist_ok = any(_has(path, a) for a in artist_parts(artist))
        if artist_ok:
            pts += 15
            why.append("artist")
        # exact: filename is just track number + artist + title
        rest = re.sub(r"\b\d+\b", " ", base)
        for a in artist_parts(artist):
            rest = rest.replace(a, " ")
        rest = " ".join(rest.replace(form, " ").split())
        if not rest:
            pts += 10
            why.append("exact")
        # decorations the file has and the wanted title does not (remix, edit...)
        wanted_words = set(norm(title).split())
        no_original = re.sub(r"\boriginal (?:mix|version)\b", " ", base)
        extra = (set(no_original.split()) & VARIANT_WORDS) - wanted_words
        if extra:
            pts -= 25
            why.append("variant:" + ",".join(sorted(extra)))
        cand = (pts, why, artist_ok)
        if best is None or cand[0] > best[0]:
            best = cand
    if best is None:
        return None
    pts, why, artist_ok = best

    if w.duration and c.length:
        d = abs(w.duration - c.length)
        if d > 10:
            return None  # different version/edit
        pts += 15 if d <= 2 else 8 if d <= 5 else 0
        why.append(f"dur±{d:.0f}s")
        close = d <= 3
    else:
        close = False
    if not artist_ok and not (close and "exact" in why):
        # Without the artist anywhere in the path, only a file named just "<number> <title>" with a
        # matching length is accepted. Any other word in the name (another artist, a release name)
        # means another track that happens to share a short title and a similar length.
        return None
    return pts, why


def _availability(c: Candidate) -> float:
    pts = 6.0 if c.free_slot else 0.0
    pts -= min(c.queue, 100) / 25  # up to -4
    pts += min(c.speed / 1_000_000, 1.0) * 3
    return pts


def rank(w: Wanted, responses: list[dict]) -> list[Scored]:
    """Acceptable candidates for w, best first."""
    out = []
    for c in candidates(responses):
        tier = _tier(c)
        if tier is None:
            continue
        m = _match(c, w)
        if m is None:
            continue
        pts, why = m
        score = tier * 100 + pts + _availability(c)
        if c.is_lossless and (c.bit_depth or 0) >= HI_RES_BIT_DEPTH:
            score += HI_RES_BONUS
            why.append("24bit")
        if c.ext in LOSSY and c.bitrate and not c.vbr:
            score += CBR_BONUS
            why.append("cbr")
        if c.is_lossless and c.sample_rate and c.sample_rate < 44100:
            score -= 10
            why.append("low-samplerate")
        out.append(Scored(c, score, tier, why))
    out.sort(key=lambda s: s.score, reverse=True)
    return out


def queries(w: Wanted) -> list[str]:
    """Search strings to try in order, most specific first, deduplicated."""
    qs = []
    for artist, title in w.variants():
        qs.append(f"{norm(artist)} {norm(title_forms(title)[-1])}")
    qs.append(norm(title_forms(w.title)[-1]))  # title only: noisy, last resort
    seen, out = set(), []
    for q in qs:
        if q and q not in seen:
            seen.add(q)
            out.append(q)
    return out
