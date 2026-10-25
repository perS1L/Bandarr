import json
import shutil
import subprocess

import pytest

from bandarr import convert
from bandarr.convert import NotFlac, flac_to_aiff, probe, target_codec, target_rate

pytestmark = pytest.mark.skipif(not shutil.which(convert.FFMPEG), reason="ffmpeg not available")


def ff(*args):
    subprocess.run([convert.FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


def make_flac(path, rate=44100, fmt="s16", secs=2, cover=None, **tags):
    meta = [x for k, v in tags.items() for x in ("-metadata", f"{k}={v}")]
    src = ["-f", "lavfi", "-i", f"sine=frequency=440:sample_rate={rate}:duration={secs}"]
    if cover:
        ff(*src, "-i", str(cover), "-map", "0:a", "-map", "1:v", "-c:a", "flac", "-sample_fmt", fmt,
           "-c:v", "png", "-disposition:v", "attached_pic", *meta, str(path))
    else:
        ff(*src, "-c:a", "flac", "-sample_fmt", fmt, *meta, str(path))
    return path


def ffprobe_json(path):
    out = subprocess.run([convert.FFPROBE, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def test_target_rate_by_integer_ratio():
    assert [target_rate(r) for r in (44100, 48000, 88200, 96000, 176400, 192000, 32000, 22050, 64000)] == \
        [44100, 48000, 44100, 48000, 44100, 48000, 44100, 44100, 48000]


def test_target_codec_keeps_depth():
    assert target_codec(16) == "pcm_s16be" and target_codec(24) == "pcm_s24be"


def test_16bit_44k_stays_16bit_44k_with_tags(tmp_path):
    src = make_flac(tmp_path / "a.flac", title="Oak", artist="Connor Wall", album="Equals")
    dst = flac_to_aiff(src)
    assert dst == tmp_path / "a.aiff" and dst.exists() and src.exists()
    j = ffprobe_json(dst)
    a = [s for s in j["streams"] if s["codec_type"] == "audio"][0]
    assert a["codec_name"] == "pcm_s16be" and a["sample_rate"] == "44100"
    tags = {k.lower(): v for k, v in j["format"].get("tags", {}).items()}
    assert tags.get("title") == "Oak" and tags.get("artist") == "Connor Wall" and tags.get("album") == "Equals"
    assert not list(tmp_path.glob(".*part"))


def test_24bit_is_kept_not_truncated(tmp_path):
    src = make_flac(tmp_path / "a.flac", fmt="s32")
    assert probe(src).bits == 24
    a = [s for s in ffprobe_json(flac_to_aiff(src))["streams"] if s["codec_type"] == "audio"][0]
    assert a["codec_name"] == "pcm_s24be"


@pytest.mark.parametrize("rate,expected", [(96000, 48000), (88200, 44100)])
def test_high_sample_rates_are_capped(tmp_path, rate, expected):
    out = probe(flac_to_aiff(make_flac(tmp_path / "a.flac", rate=rate, fmt="s32")))
    assert out.sample_rate == expected and out.codec == "pcm_s24be"


def test_cover_art_is_carried_over(tmp_path):
    ff("-f", "lavfi", "-i", "color=c=red:s=64x64:d=1", "-frames:v", "1", str(tmp_path / "c.png"))
    src = make_flac(tmp_path / "a.flac", cover=tmp_path / "c.png")
    kinds = [s["codec_type"] for s in ffprobe_json(flac_to_aiff(src))["streams"]]
    assert "video" in kinds


def test_mp3_renamed_to_flac_is_refused(tmp_path):
    fake = tmp_path / "fake.flac"
    ff("-f", "lavfi", "-i", "sine=duration=1", "-c:a", "libmp3lame", "-f", "mp3", str(fake))
    with pytest.raises(NotFlac):
        flac_to_aiff(fake)
    assert not (tmp_path / "fake.aiff").exists() and not list(tmp_path.glob(".*part"))


def test_corrupt_input_leaves_no_output(tmp_path):
    bad = tmp_path / "bad.flac"
    bad.write_bytes(b"not audio at all")
    with pytest.raises(convert.ConvertError):
        flac_to_aiff(bad)
    assert not list(tmp_path.glob("*.aiff")) and not list(tmp_path.glob(".*part"))
