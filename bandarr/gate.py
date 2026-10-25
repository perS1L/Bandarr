"""Anti-transcode gate (CLAUDE.md section 9).

Two independent detectors run on every lossless file from Soulseek:
  * lossless-verify (spectral cliff; pinned in requirements.txt)
  * flaccheck (cliff + MDCT quantisation + other fingerprints; Rust binary)
Both raw verdicts are kept in the result so they can be stored and compared on
real data. A configurable RULE turns them into one decision; the default is the
most cautious one (refuse when either tool flags). Which rule is best is not
settled: see `State.gate_report()` once real files have been labelled.

Scope: lossless from Soulseek only. Bandcamp purchases bypass (trusted by
origin) and MP3 is not tested. Refused files (suspect/fail) are moved to the
review folder, never deleted, never converted.

Measured limits (CLAUDE.md section 14): neither tool saw AAC 256 transcodes, and
genuine but band-limited masters can be flagged.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path

from lossless_verify import analyze

_QUARANTINE_LOCK = threading.Lock()  # name choice + move must not interleave between workers
LOSSLESS_EXT = {".flac", ".aiff", ".aif"}
_LV_VERDICT = {"PASS": "pass", "SUSPECT": "suspect", "FAIL": "fail"}
NO_GATE = "none"  # GATE_RULE=none: no detector runs, every lossless copy is trusted (the user's choice)
DEFAULT_RULE = "both"  # refuse only when both tools flag; chosen with the user on the first real data
# We only want to avoid transcodes from LOSSY sources; upsampled (fake hi-res) lossless is fine.
# A file above 44.1 kHz whose spectrum ends at ~22 kHz was upsampled from 44.1 kHz material: the
# cliff lossless-verify sees is the old Nyquist edge, not a lossy low-pass. Restricted to
# sample_rate > 44100 on purpose: at 44.1 kHz a cliff at 21 kHz is NOT explained by upsampling
# (a synthetic MP3-128 transcode measured exactly that: cutoff 21 kHz, 82 dB drop).
UPSAMPLE_CUTOFF_RANGE_HZ = (21000, 23000)


def effective_lv(lv: str, cutoff_hz: float, sample_rate: int) -> str:
    """lossless-verify verdict with the upsampling false alarm removed."""
    lo, hi = UPSAMPLE_CUTOFF_RANGE_HZ
    if lv in ("SUSPECT", "FAIL") and sample_rate > 44100 and lo <= cutoff_hz < hi:
        return "PASS"
    return lv


# --- decision rules over the raw verdicts -----------------------------------
# lv in PASS|SUSPECT|FAIL|UNKNOWN ; fc in GENUINE|SUSPICIOUS|TRANSCODED|INCONCLUSIVE or
# None when flaccheck is unavailable (then lossless-verify decides alone).
# Each returns True when the file must be REFUSED.
def _lv_only(lv: str, fc: str | None) -> bool:
    return lv != "PASS"


def _either(lv: str, fc: str | None) -> bool:
    return lv != "PASS" or (fc is not None and fc != "GENUINE")


def _both(lv: str, fc: str | None) -> bool:
    return lv != "PASS" and (fc is None or fc != "GENUINE")


def _fc_judge(lv: str, fc: str | None) -> bool:
    if fc is None:
        return lv != "PASS"
    return fc != "GENUINE" or lv == "FAIL"


RULES = {"lv": _lv_only, "either": _either, "both": _both, "fc_judge": _fc_judge}


@dataclass
class GateResult:
    verdict: str  # pass | suspect | fail | n/a  (state.VERDICTS)
    reason: str
    rule: str | None = None
    lv_verdict: str | None = None  # raw lossless-verify verdict
    fc_verdict: str | None = None  # raw flaccheck verdict (None: not run/unavailable)
    fc_confidence: float | None = None
    cutoff_hz: float = 0.0
    max_drop_db: float = 0.0
    cliff_ratio: float | None = None
    sample_rate: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when the file may continue down the pipeline (convert, import)."""
        return self.verdict in ("pass", "n/a")


def decide(lv: str, fc: str | None, rule: str) -> bool:
    """True = refuse. Raises KeyError for an unknown rule."""
    return RULES[rule](lv, fc)


def run_flaccheck(path: Path, binary: str | None = None) -> tuple[str, float] | None:
    """(verdict, confidence) from flaccheck, or None if it is unavailable/failed."""
    binary = binary or os.environ.get("FLACCHECK") or shutil.which("flaccheck")
    if not binary:
        return None
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "r.json"
        p = subprocess.run(
            [binary, "scan", "--mode", "balanced", "--format", "json", "-o", str(out), str(path)],
            capture_output=True, text=True, stdin=subprocess.DEVNULL,
        )
        try:
            r = json.loads(out.read_text())["results"][0]
            return r["transcode_verdict"], float(r["confidence"])
        except (OSError, ValueError, KeyError, IndexError):
            return None


def check(
    path: Path,
    source: str,
    rule: str | None = None,
    ffmpeg: str | None = None,
    ffprobe: str | None = None,
    flaccheck: str | None = None,
) -> GateResult:
    path = Path(path)
    rule = rule or os.environ.get("GATE_RULE") or DEFAULT_RULE
    if rule != NO_GATE and rule not in RULES:
        raise ValueError(f"unknown gate rule {rule!r}, expected one of {sorted(RULES) + [NO_GATE]}")
    if source == "bandcamp":
        return GateResult("n/a", "bandcamp purchase: trusted by origin")
    if path.suffix.lower() not in LOSSLESS_EXT:
        return GateResult("n/a", f"{path.suffix.lower() or 'file'} is not lossless: not tested")

    if rule == NO_GATE:  # not "pass": nothing was measured, and the state must say so
        return GateResult("n/a", "gate disabled (GATE_RULE=none): lossless copy trusted without analysis", rule=NO_GATE)

    ffmpeg = ffmpeg or os.environ.get("FFMPEG") or shutil.which("ffmpeg") or "ffmpeg"
    ffprobe = ffprobe or os.environ.get("FFPROBE") or shutil.which("ffprobe") or "ffprobe"
    try:
        r = analyze(str(path), ffmpeg, ffprobe)
    except Exception as e:  # unreadable/corrupt: not verified, so not trusted
        return GateResult("suspect", f"analysis failed: {e}", rule=rule, lv_verdict="UNKNOWN")

    lv = r["verdict"]  # PASS | SUSPECT | FAIL | UNKNOWN
    fc = run_flaccheck(path, flaccheck)
    res = GateResult(
        "pass", "", rule=rule, lv_verdict=lv,
        fc_verdict=fc[0] if fc else None, fc_confidence=fc[1] if fc else None,
        cutoff_hz=r.get("cutoff_hz", 0), max_drop_db=r.get("max_drop_db", 0),
        cliff_ratio=r.get("cliff_ratio"), sample_rate=r.get("sr", 0),
    )
    if fc is None:
        res.notes.append("flaccheck unavailable: lossless-verify decided alone")
    lv_eff = effective_lv(lv, res.cutoff_hz, res.sample_rate)
    if lv_eff != lv:
        res.notes.append(f"lossless-verify {lv} ignored: {res.sample_rate} Hz file with the spectrum "
                         f"ending at {res.cutoff_hz / 1000:.0f} kHz (upsampled from 44.1 kHz, not a transcode)")

    detail = (f"lossless-verify {lv} (cutoff {res.cutoff_hz / 1000:.0f} kHz, "
              f"drop {res.max_drop_db:.1f} dB); flaccheck "
              f"{res.fc_verdict or 'n/a'}{f' ({res.fc_confidence:.2f})' if fc else ''}; rule {rule}")
    if decide(lv_eff, res.fc_verdict, rule):
        # fail = strong evidence from either tool, suspect = anything weaker or unreadable
        strong = lv_eff == "FAIL" or res.fc_verdict == "TRANSCODED"
        res.verdict = "fail" if strong else "suspect"
        res.reason = "refused: " + detail
    else:
        res.reason = "accepted: " + detail
    return res


def quarantine(path: Path, result: GateResult, review_dir: Path) -> Path:
    """Move a refused file to review_dir/<verdict>/ with a sidecar. Never overwrites."""
    dest_dir = Path(review_dir) / result.verdict
    dest_dir.mkdir(parents=True, exist_ok=True)
    with _QUARANTINE_LOCK:
        dest = dest_dir / Path(path).name
        n = 1
        while dest.exists():
            dest = dest_dir / f"{Path(path).stem}-{n}{Path(path).suffix}"
            n += 1
        shutil.move(str(path), dest)
    dest.with_name(dest.name + ".verdict.json").write_text(
        json.dumps({"original": str(path), **asdict(result)}, indent=2))
    return dest
