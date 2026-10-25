import os
import shutil
import subprocess

import pytest

from bandarr import convert
from bandarr.gate import RULES, check, decide, quarantine
from bandarr.state import State

pytest.importorskip("lossless_verify")

# --- pure decision rules (no binaries needed) --------------------------------

def test_rules_on_the_cases_that_matter():
    # Oak: lossless-verify says SUSPECT (cutoff rule), flaccheck says GENUINE
    assert decide("SUSPECT", "GENUINE", "either") is True
    assert decide("SUSPECT", "GENUINE", "lv") is True
    assert decide("SUSPECT", "GENUINE", "both") is False
    assert decide("SUSPECT", "GENUINE", "fc_judge") is False
    # a hard FAIL from lossless-verify is never waved through
    assert decide("FAIL", "GENUINE", "fc_judge") is True
    # both agree it is clean
    assert all(decide("PASS", "GENUINE", r) is False for r in RULES)
    # flaccheck alone flags: refused by every rule that lets flaccheck speak
    assert decide("PASS", "TRANSCODED", "either") is True
    assert decide("PASS", "TRANSCODED", "fc_judge") is True
    assert decide("PASS", "TRANSCODED", "both") is False and decide("PASS", "TRANSCODED", "lv") is False
    # INCONCLUSIVE is not trusted
    assert decide("PASS", "INCONCLUSIVE", "either") is True


def test_upsampling_exception_only_applies_above_44k1():
    from bandarr.gate import effective_lv
    # 44.1 kHz material upsampled to 48 kHz: cliff at the old Nyquist (22 kHz)
    assert effective_lv("SUSPECT", 22000, 48000) == "PASS"
    assert effective_lv("FAIL", 21000, 96000) == "PASS"
    # at 44.1 kHz a cliff at 21 kHz is NOT upsampling (a real MP3-128 transcode measured that)
    assert effective_lv("FAIL", 21000, 44100) == "FAIL"
    assert effective_lv("SUSPECT", 22000, 44100) == "SUSPECT"
    # genuine lossy low-passes stay flagged at any sample rate
    assert effective_lv("SUSPECT", 20000, 48000) == "SUSPECT"
    assert effective_lv("FAIL", 17000, 48000) == "FAIL"
    assert effective_lv("PASS", 22000, 48000) == "PASS" and effective_lv("UNKNOWN", 22000, 48000) == "UNKNOWN"


def test_without_flaccheck_lossless_verify_decides_alone_and_conservatively():
    for r in RULES:
        assert decide("PASS", None, r) is False
        assert decide("SUSPECT", None, r) is True


def test_unknown_rule_is_an_error(tmp_path):
    with pytest.raises(ValueError):
        check(tmp_path / "a.flac", "soulseek", rule="nope")


def test_bandcamp_and_mp3_never_touch_the_detectors(tmp_path):
    assert check(tmp_path / "a.flac", "bandcamp").verdict == "n/a"
    assert check(tmp_path / "a.mp3", "soulseek").verdict == "n/a"


def test_gate_report_compares_rules_on_labelled_files(tmp_path):
    from bandarr.gate import GateResult
    from bandarr.bandcamp import Track
    st = State(tmp_path / "s.db")
    st.add_tracks([Track("1:1", "A", "T", "Al", "A", 1, 100.0, "album", "u")])

    def rec(lv, fc, label):
        i = st.record_gate("1:1", "/x", GateResult("pass", "", rule="either", lv_verdict=lv, fc_verdict=fc))
        st.label_gate(i, label)

    rec("SUSPECT", "GENUINE", "genuine")   # an Oak-like file
    rec("SUSPECT", "TRANSCODED", "fake")
    rec("PASS", "GENUINE", "fake")          # AAC 256-like: nobody sees it
    rep = st.gate_report()
    assert rep["either"] == {"fake_caught": 1, "fake_missed": 1, "genuine_refused": 1, "genuine_passed": 0}
    assert rep["both"] == {"fake_caught": 1, "fake_missed": 1, "genuine_refused": 0, "genuine_passed": 1}
    with pytest.raises(ValueError):
        st.label_gate(1, "maybe")
    with pytest.raises(KeyError):
        st.label_gate(999, "fake")


# --- with real ffmpeg (and flaccheck when available) ---------------------------

needs_ffmpeg = pytest.mark.skipif(not shutil.which(convert.FFMPEG), reason="ffmpeg not available")
needs_flaccheck = pytest.mark.skipif(
    not (os.environ.get("FLACCHECK") or shutil.which("flaccheck")), reason="flaccheck not available")


def ff(*args):
    subprocess.run([convert.FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


@pytest.fixture
def fullband(tmp_path):
    p = tmp_path / "real.flac"
    ff("-f", "lavfi", "-i", "anoisesrc=color=white:amplitude=0.3:sample_rate=44100:duration=8", "-c:a", "flac", str(p))
    return p


@pytest.fixture
def transcode(tmp_path, fullband):
    mp3, p = tmp_path / "t.mp3", tmp_path / "fake.flac"
    ff("-i", str(fullband), "-c:a", "libmp3lame", "-b:a", "128k", str(mp3))
    ff("-i", str(mp3), "-c:a", "flac", str(p))
    return p


@needs_ffmpeg
def test_genuine_fullband_flac_passes(fullband):
    r = check(fullband, "soulseek")
    assert r.verdict == "pass" and r.ok and r.lv_verdict == "PASS"


@needs_ffmpeg
def test_mp3_transcoded_to_flac_is_refused_by_default_rule(transcode):
    r = check(transcode, "soulseek")
    assert r.verdict in ("suspect", "fail") and not r.ok and r.rule == "both"


@needs_ffmpeg
@needs_flaccheck
def test_both_detectors_report_raw_verdicts(transcode):
    r = check(transcode, "soulseek")
    assert r.lv_verdict in ("SUSPECT", "FAIL") and r.fc_verdict in ("TRANSCODED", "SUSPICIOUS")
    assert r.fc_confidence is not None


@needs_ffmpeg
def test_bandcamp_bypasses_even_a_transcode(transcode):
    assert check(transcode, "bandcamp").verdict == "n/a"


@needs_ffmpeg
def test_corrupt_lossless_is_not_trusted(tmp_path):
    p = tmp_path / "bad.flac"
    p.write_bytes(b"garbage")
    r = check(p, "soulseek")
    assert r.verdict == "suspect" and not r.ok


@needs_ffmpeg
def test_quarantine_moves_never_deletes_and_never_overwrites(tmp_path, transcode):
    r = check(transcode, "soulseek")
    review = tmp_path / "review"
    d1 = quarantine(transcode, r, review)
    assert d1.exists() and not transcode.exists() and d1.parent.name == r.verdict
    assert (d1.parent / (d1.name + ".verdict.json")).exists()
    again = tmp_path / "fake.flac"
    again.write_bytes(b"x")
    d2 = quarantine(again, r, review)
    assert d2 != d1 and d1.exists() and d2.exists()


def test_rule_none_runs_no_detector_and_says_nothing_was_measured(tmp_path):
    # the file does not even exist: nothing is opened, analysed or run
    res = check(tmp_path / "missing.flac", "soulseek", rule="none")
    assert res.ok and res.verdict == "n/a" and res.rule == "none" and res.lv_verdict is None
    assert "disabled" in res.reason
