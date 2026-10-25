import shutil
import subprocess

import pytest
from mutagen.aiff import AIFF
from mutagen.flac import FLAC

from bandarr import convert
from bandarr.tagger import (display_names, is_compilation, place, read_source, safe_filename,
                            tag_and_place, write_tags)


def row(**kw):
    base = dict(artist="Connor Wall", title="Oak", album="Equals", album_artist="Connor Wall",
                track_num=1, alt_artist=None, alt_title=None)
    return {**base, **kw}


# --- pure logic ------------------------------------------------------------------

def test_compilation_detected_by_several_distinct_split_artists_or_va_name():
    va = [row(album_artist="PAN", alt_artist="Kareem Lotfy", alt_title="Fr3sh"),
          row(album_artist="PAN", alt_artist="Malibu", alt_title="Held")]
    assert is_compilation(va)
    assert is_compilation([row(album_artist="Various Artists")])


def test_single_artist_album_with_dash_in_titles_is_not_split():
    # one distinct "artist" found by the heuristic is not enough to trust it
    album = [row(artist="Hadone", title="Part 1 - Intro", alt_artist="Part 1", alt_title="Intro"),
             row(artist="Hadone", title="Part 2 - Outro", alt_artist="Part 1", alt_title="Outro")]
    assert not is_compilation(album)
    assert display_names(album[0], False) == ("Hadone", "Part 1 - Intro")


def test_display_names_use_the_split_only_for_compilations():
    r = row(artist="PAN", title="Kareem Lotfy - Fr3sh", alt_artist="Kareem Lotfy", alt_title="Fr3sh")
    assert display_names(r, True) == ("Kareem Lotfy", "Fr3sh")
    assert display_names(r, False) == ("PAN", "Kareem Lotfy - Fr3sh")


def test_filenames_are_fat32_safe():
    assert safe_filename('AC/DC: "Back" in <Black>?') == "AC-DC- -Back- in -Black--"
    assert safe_filename("trailing dot. ") == "trailing dot"
    assert safe_filename("   ") == "untitled"
    assert len(safe_filename("x" * 400)) <= 180


def test_place_is_flat_renamed_and_never_overwrites_another_track(tmp_path):
    a, b = tmp_path / "a.flac", tmp_path / "b.flac"
    a.write_bytes(b"1")
    b.write_bytes(b"2")
    master = tmp_path / "master"
    d1 = place(a, master, "Connor Wall", "Oak")
    d2 = place(b, master, "Connor Wall", "Oak")
    assert d1.name == "Connor Wall - Oak.flac" and d2.name == "Connor Wall - Oak (2).flac"
    assert d1.read_bytes() == b"1" and d2.read_bytes() == b"2"


# --- with real audio files -----------------------------------------------------------

needs_ffmpeg = pytest.mark.skipif(not shutil.which(convert.FFMPEG), reason="ffmpeg not available")


def ff(*args):
    subprocess.run([convert.FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


@pytest.fixture
def cover(tmp_path):
    ff("-f", "lavfi", "-i", "color=c=red:s=64x64:d=1", "-frames:v", "1", str(tmp_path / "c.png"))
    return tmp_path / "c.png"


def make_flac(path, cover=None, **tags):
    meta = [x for k, v in tags.items() for x in ("-metadata", f"{k}={v}")]
    src = ["-f", "lavfi", "-i", "sine=frequency=440:duration=2"]
    if cover:
        ff(*src, "-i", str(cover), "-map", "0:a", "-map", "1:v", "-c:a", "flac", "-c:v", "png",
           "-disposition:v", "attached_pic", *meta, str(path))
    else:
        ff(*src, "-c:a", "flac", *meta, str(path))
    return path


@needs_ffmpeg
def test_flac_tags_replaced_source_written_cover_kept(tmp_path, cover):
    p = make_flac(tmp_path / "x.flac", cover=cover, title="GARBAGE", artist="WRONG", comment="peer spam")
    out = tag_and_place(p, row(), tmp_path / "master", "soulseek", False)
    f = FLAC(out)
    assert out.name == "Connor Wall - Oak.flac"
    assert f["title"] == ["Oak"] and f["artist"] == ["Connor Wall"] and f["album"] == ["Equals"]
    assert "comment" not in f and len(f.pictures) == 1
    assert read_source(out) == "soulseek"


@needs_ffmpeg
def test_aiff_tags_source_and_cover_survive_conversion_then_tagging(tmp_path, cover):
    flac = make_flac(tmp_path / "x.flac", cover=cover, title="GARBAGE", genre="Peer genre")
    aiff = convert.flac_to_aiff(flac)
    out = tag_and_place(aiff, row(track_num=3), tmp_path / "master", "bandcamp", False)
    tags = AIFF(out).tags
    assert out.suffix == ".aiff" and str(tags["TIT2"]) == "Oak" and str(tags["TRCK"]) == "3"
    assert "TCON" not in tags  # leftover peer tag removed
    assert len(tags.getall("APIC")) == 1
    assert read_source(out) == "bandcamp"
    assert tags.version == (2, 3, 0)


@needs_ffmpeg
def test_mp3_is_tagged_too(tmp_path):
    p = tmp_path / "x.mp3"
    ff("-f", "lavfi", "-i", "sine=duration=1", "-c:a", "libmp3lame", str(p))
    out = tag_and_place(p, row(), tmp_path / "master", "soulseek", False)
    assert read_source(out) == "soulseek" and out.name == "Connor Wall - Oak.mp3"


def test_unsupported_format_is_an_error(tmp_path):
    from bandarr.tagger import TagError
    p = tmp_path / "x.ogg"
    p.write_bytes(b"x")
    with pytest.raises(TagError):
        write_tags(p, row(), "soulseek", "A", "T")


JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 40


@needs_ffmpeg
def test_cover_added_when_missing_and_never_replaces_an_existing_one(tmp_path, cover):
    from bandarr.tagger import AIFF as _  # noqa: F401  (module imports mutagen AIFF)
    bare = make_flac(tmp_path / "bare.flac")
    out = tag_and_place(bare, row(), tmp_path / "m", "soulseek", False, cover=JPEG)
    assert len(FLAC(out).pictures) == 1 and FLAC(out).pictures[0].data == JPEG
    with_cover = make_flac(tmp_path / "has.flac", cover=cover)
    original = FLAC(with_cover).pictures[0].data
    out2 = tag_and_place(with_cover, row(title="Elm"), tmp_path / "m", "soulseek", False, cover=JPEG)
    assert FLAC(out2).pictures[0].data == original  # the file's own cover wins


@needs_ffmpeg
def test_cover_added_to_aiff_without_one(tmp_path):
    aiff = convert.flac_to_aiff(make_flac(tmp_path / "x.flac"))
    out = tag_and_place(aiff, row(), tmp_path / "m", "soulseek", False, cover=JPEG)
    frames = AIFF(out).tags.getall("APIC")
    assert len(frames) == 1 and frames[0].data == JPEG and frames[0].mime == "image/jpeg"


@needs_ffmpeg
def test_add_cover_if_missing_is_idempotent(tmp_path):
    from bandarr.tagger import add_cover_if_missing
    aiff = convert.flac_to_aiff(make_flac(tmp_path / "x.flac"))
    assert add_cover_if_missing(aiff, JPEG) is True
    assert add_cover_if_missing(aiff, b"\xff\xd8other") is False
    assert AIFF(aiff).tags.getall("APIC")[0].data == JPEG


# --- a title that already starts with the artist is not repeated in the file name ---------------

def test_a_leading_artist_in_the_title_is_not_repeated():
    r = row(artist="ANNĒ", title="ANNĒ - Daydream", album_artist="ANNĒ")
    assert display_names(r, False) == ("ANNĒ", "Daydream")
    assert safe_filename("%s - %s" % display_names(r, False)) == "ANNĒ - Daydream"


def test_the_artist_prefix_match_ignores_case_and_accents_but_must_be_the_whole_artist():
    assert display_names(row(artist="ANNĒ", title="anne - Daydream"), False) == ("ANNĒ", "Daydream")
    # another artist, a part title, or only a part of a multi-artist name: left alone
    assert display_names(row(artist="Hadone", title="Part 1 - Intro"), False) == ("Hadone", "Part 1 - Intro")
    assert display_names(row(artist="A & B", title="A - Song"), False) == ("A & B", "A - Song")
    assert display_names(row(artist="Hadone", title="Hadone"), False) == ("Hadone", "Hadone")  # nothing left to keep
    assert display_names(row(artist="X", title="X - "), False) == ("X", "X -")


def test_compilation_split_still_wins_over_the_prefix_rule():
    r = row(artist="PAN", title="Kareem Lotfy - Fr3sh", alt_artist="Kareem Lotfy", alt_title="Fr3sh")
    assert display_names(r, True) == ("Kareem Lotfy", "Fr3sh")


@needs_ffmpeg
def test_retag_and_place_renames_an_existing_file_and_keeps_provenance_and_cover(tmp_path, cover):
    from bandarr.tagger import retag_and_place
    r = row(artist="ANNĒ", title="ANNĒ - Daydream", album_artist="ANNĒ")
    master = tmp_path / "master"
    master.mkdir()
    old = master / "ANNĒ - ANNĒ - Daydream.flac"
    make_flac(old, cover=cover, title="ANNĒ - Daydream", artist="ANNĒ")
    write_tags(old, r, "soulseek", "ANNĒ", "ANNĒ - Daydream")  # as the old code wrote it
    new = retag_and_place(old, r, master, False)
    f = FLAC(new)
    assert new.name == "ANNĒ - Daydream.flac" and not old.exists()
    assert f["title"] == ["Daydream"] and read_source(new) == "soulseek" and len(f.pictures) == 1
