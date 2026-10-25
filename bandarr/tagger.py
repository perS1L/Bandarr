"""Tag a finished file from Bandcamp metadata and place it, flat, in the master folder.

Replaces beets (decision with the user): Bandcamp already gives exact artist / title /
album / track number, which beats a MusicBrainz match on obscure releases, and the
provenance tag is a plain custom frame.

Tags are rewritten from scratch (whatever a Soulseek peer left in the file is
dropped) except embedded cover art, which is kept. Names are FAT32-safe because the
USB stick must be FAT32 for the Pioneer XDJ-700.
"""
from __future__ import annotations

import re
import shutil
import threading
from pathlib import Path

from .scoring import norm

from mutagen.aiff import AIFF
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, ID3, TALB, TIT2, TPE1, TPE2, TRCK, TXXX
from mutagen.mp3 import MP3

SOURCE_KEY = "source"  # FLAC: SOURCE vorbis comment; ID3: TXXX:source
VARIOUS = {"various artists", "various", "va", "v/a", "v.a."}
_ARTIST_PREFIX = re.compile(r"^(.+?)\s+[-\u2013\u2014]\s+(.+)$")  # "Artist - Title"
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_PLACE_LOCK = threading.Lock()  # name choice + move must not interleave between workers
MAX_STEM = 180  # FAT32 allows 255 chars per name; stay well under with the extension


class TagError(RuntimeError):
    pass


def is_compilation(rows) -> bool:
    """True when an album's tracks are by several artists (title is 'Artist - Title').

    rows: the album's tracks (mappings with album_artist / alt_artist). Splitting is only
    trusted for compilations: on a single-artist album a ' - ' in a title is just a title.
    """
    rows = list(rows)
    if any(str(r["album_artist"]).strip().lower() in VARIOUS for r in rows):
        return True
    distinct = {r["alt_artist"].strip().lower() for r in rows if r["alt_artist"]}
    if len(rows) == 1 and rows[0]["alt_artist"]:
        # a single track added on its own: the label is the Bandcamp artist and the title reads
        # "Artist - Title". Not when that artist is the page's artist (then the title is just a title).
        alt, page = norm(rows[0]["alt_artist"]), norm(rows[0]["album_artist"])
        return bool(alt) and alt not in page and page not in alt
    return len(distinct) >= 2


def display_names(row, compilation: bool) -> tuple[str, str]:
    """(artist, title) to write and to name the file with.

    Some labels publish titles that already start with the artist ("ANNE - Daydream" by
    ANNE): naming the file "Artist - Title" would then repeat it ("ANNE - ANNE - Daydream"),
    so a leading "<artist> - " is dropped from the title. Only when it is exactly the artist.
    """
    if compilation and row["alt_artist"] and row["alt_title"]:
        return row["alt_artist"].strip(), row["alt_title"].strip()
    artist, title = row["artist"].strip(), row["title"].strip()
    m = _ARTIST_PREFIX.match(title)
    if m and norm(m[1]) == norm(artist):
        title = m[2].strip()
    return artist, title


def safe_filename(stem: str) -> str:
    s = _ILLEGAL.sub("-", stem)
    s = re.sub(r"\s+", " ", s).strip(" .")
    return (s[:MAX_STEM].rstrip(" .") or "untitled")


def _open_tags(path: Path):
    ext = path.suffix.lower()
    if ext == ".flac":
        return "flac", FLAC(path)
    if ext in (".aiff", ".aif"):
        a = AIFF(path)
        if a.tags is None:
            a.add_tags()
        return "id3", a
    if ext == ".mp3":
        m = MP3(path, ID3=ID3)
        if m.tags is None:
            m.add_tags()
        return "id3", m
    raise TagError(f"cannot tag {ext or 'this'} files")


def write_tags(path: Path, row, source: str, artist: str, title: str, cover: bytes | None = None) -> None:
    """Rewrite all text tags of path; embedded pictures are kept, and `cover` (JPEG bytes)
    is added only when the file has none."""
    path = Path(path)
    kind, f = _open_tags(path)
    track = str(row["track_num"]) if row["track_num"] else None
    if kind == "flac":
        f.delete()  # drops every vorbis comment; pictures live in separate blocks and stay
        f["title"], f["artist"], f["album"], f["albumartist"] = title, artist, row["album"], row["album_artist"]
        if track:
            f["tracknumber"] = track
        f[SOURCE_KEY] = source
        if cover and not f.pictures:
            pic = Picture()
            pic.type, pic.mime, pic.desc, pic.data = 3, "image/jpeg", "Cover", cover
            f.add_picture(pic)
        f.save()
        return
    tags = f.tags
    for frame in [k for k in tags.keys() if not k.startswith("APIC")]:
        del tags[frame]
    tags.add(TIT2(encoding=1, text=title))
    tags.add(TPE1(encoding=1, text=artist))
    tags.add(TALB(encoding=1, text=row["album"]))
    tags.add(TPE2(encoding=1, text=row["album_artist"]))
    if track:
        tags.add(TRCK(encoding=1, text=track))
    tags.add(TXXX(encoding=1, desc=SOURCE_KEY, text=source))
    if cover and not tags.getall("APIC"):
        tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover))
    f.save(v2_version=3)  # ID3v2.3: the most widely read version on DJ gear


def add_cover_if_missing(path: Path, cover: bytes) -> bool:
    """Embed `cover` (JPEG bytes) in an already tagged file that has none. True if added."""
    kind, f = _open_tags(Path(path))
    if kind == "flac":
        if f.pictures:
            return False
        pic = Picture()
        pic.type, pic.mime, pic.desc, pic.data = 3, "image/jpeg", "Cover", cover
        f.add_picture(pic)
        f.save()
        return True
    if f.tags.getall("APIC"):
        return False
    f.tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover))
    f.save(v2_version=3)
    return True


def read_source(path: Path) -> str | None:
    """Provenance tag of a file (CLAUDE.md: provenance is a tag, never the extension)."""
    kind, f = _open_tags(Path(path))
    if kind == "flac":
        v = f.get(SOURCE_KEY)
        return v[0] if v else None
    fr = f.tags.getall(f"TXXX:{SOURCE_KEY}")
    return str(fr[0].text[0]) if fr else None


def place(path: Path, master_dir: Path, artist: str, title: str) -> Path:
    """Move path to master_dir/'Artist - Title.ext' (flat). Never overwrites another track."""
    path, master_dir = Path(path), Path(master_dir)
    master_dir.mkdir(parents=True, exist_ok=True)
    stem = safe_filename(f"{artist} - {title}")
    with _PLACE_LOCK:
        dest = master_dir / f"{stem}{path.suffix.lower()}"
        n = 2
        while dest.exists() and dest.resolve() != path.resolve():
            dest = master_dir / f"{stem} ({n}){path.suffix.lower()}"
            n += 1
        if dest.resolve() != path.resolve():
            shutil.move(str(path), dest)
    return dest


def retag_and_place(path: Path, row, master_dir: Path, compilation: bool, source: str | None = None) -> Path:
    """Apply the current naming rules to a file that is already in the master folder: tags
    rewritten (provenance and cover kept) and the file renamed. Returns the new path."""
    source = source or read_source(path) or row["source"] or "soulseek"
    artist, title = display_names(row, compilation)
    write_tags(path, row, source, artist, title)
    return place(path, master_dir, artist, title)


def tag_and_place(path: Path, row, master_dir: Path, source: str, compilation: bool,
                  cover: bytes | None = None) -> Path:
    """Write clean tags (incl. source=) and move the file into the master folder."""
    artist, title = display_names(row, compilation)
    write_tags(path, row, source, artist, title, cover)
    return place(path, master_dir, artist, title)
