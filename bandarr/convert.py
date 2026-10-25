"""FLAC -> AIFF conversion hook (replaces the user's PowerShell one-liner).

Same core ffmpeg call as the original
(`-map 0:a -map 0:v? -c:a pcm_s16be -c:v copy`), tuned for Pioneer XDJ-700,
whose manual lists AIFF/WAV as 16 or 24 bit at 44.1 or 48 kHz only:
  * bit depth of the source is kept (16 -> pcm_s16be, 24 -> pcm_s24be);
  * sample rate is capped to 44.1/48 kHz by integer ratio (96k -> 48k, 88.2k -> 44.1k);
  * tags and cover go to ID3v2 inside the AIFF (ffmpeg writes almost none otherwise).

INVARIANT (CLAUDE.md section 9): only real FLAC streams are converted. The codec
is read with ffprobe, never trusted from the extension, so a renamed MP3 raises
NotFlac instead of becoming a "lossless-looking" AIFF. Callers must also have
passed the gate first; this check is a second, independent line of defence.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

FFMPEG = os.environ.get("FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("FFPROBE", "ffprobe")
DURATION_TOLERANCE_S = 0.1


class ConvertError(RuntimeError):
    pass


class NotFlac(ConvertError):
    """Input is not a genuine FLAC stream."""


@dataclass
class AudioInfo:
    codec: str
    sample_rate: int
    bits: int
    duration: float


def _run(cmd: list[str]) -> str:
    p = subprocess.run(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if p.returncode != 0:
        raise ConvertError(f"{cmd[0]} failed ({p.returncode}): {p.stderr.strip()[-400:]}")
    return p.stdout


def probe(path: Path) -> AudioInfo:
    out = _run([
        FFPROBE, "-v", "error", "-select_streams", "a:0",
        "-show_entries", "stream=codec_name,sample_rate,bits_per_raw_sample,bits_per_sample,sample_fmt:format=duration",
        "-of", "json", str(path),
    ])
    d = json.loads(out)
    streams = d.get("streams") or []
    if not streams:
        raise ConvertError(f"no audio stream in {path}")
    s = streams[0]
    bits = int(s.get("bits_per_raw_sample") or s.get("bits_per_sample") or 0)
    if not bits:  # FLAC 24-bit decodes as s32, 16-bit as s16
        bits = 16 if s.get("sample_fmt") in ("s16", "s16p") else 24
    return AudioInfo(
        codec=s.get("codec_name", ""),
        sample_rate=int(s.get("sample_rate") or 0),
        bits=bits,
        duration=float(d.get("format", {}).get("duration") or 0),
    )


def target_rate(rate: int) -> int:
    """44.1/48 kHz kept; otherwise the nearest supported rate by integer ratio."""
    if rate in (44100, 48000):
        return rate
    if rate % 44100 == 0:
        return 44100
    if rate % 48000 == 0:
        return 48000
    return 48000 if rate > 48000 else 44100


def target_codec(bits: int) -> str:
    return "pcm_s16be" if bits <= 16 else "pcm_s24be"


def flac_to_aiff(src: Path, dst: Path | None = None) -> Path:
    """Convert one FLAC to AIFF next to it (or at dst). The source is not touched.

    Writes to a temporary file and renames only after the result is verified, so
    a crash never leaves a truncated .aiff for the USB sync to pick up.
    """
    src = Path(src)
    dst = Path(dst) if dst else src.with_suffix(".aiff")

    info = probe(src)
    if info.codec != "flac":
        raise NotFlac(f"{src.name}: codec is {info.codec!r}, refusing to convert to AIFF")
    if info.sample_rate <= 0 or info.duration <= 0:
        raise ConvertError(f"{src.name}: unreadable stream info {info}")

    rate, codec = target_rate(info.sample_rate), target_codec(info.bits)
    tmp = dst.with_name(f".{dst.name}.part")
    cmd = [
        FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
        "-map", "0:a", "-map", "0:v?", "-c:a", codec, "-c:v", "copy",
    ]
    if rate != info.sample_rate:
        cmd += ["-ar", str(rate)]
    cmd += ["-write_id3v2", "1", "-id3v2_version", "3", "-f", "aiff", str(tmp)]

    try:
        _run(cmd)
        out = probe(tmp)
        if out.codec != codec or out.sample_rate != rate:
            raise ConvertError(f"{dst.name}: got {out.codec}@{out.sample_rate}, wanted {codec}@{rate}")
        if abs(out.duration - info.duration) > DURATION_TOLERANCE_S:
            raise ConvertError(f"{dst.name}: duration {out.duration:.2f}s vs source {info.duration:.2f}s")
        os.replace(tmp, dst)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    log.info("converted %s -> %s (%s, %d Hz)", src.name, dst.name, codec, rate)
    return dst
