"""Gate measurement campaign: download one track per wishlist album from Soulseek,
run both detectors, store the raw verdicts and make a spectrogram per file.

Nothing is converted, imported or deleted. Files stay under data/gate_lab/.
Label the results afterwards with State.label_gate() and read State.gate_report().

    python tools/gate_lab.py [--limit N] [--user BANDCAMP_USER]
"""
from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bandarr.bandcamp import fetch_tracks  # noqa: E402
from bandarr.downloader import DownloadFailed, download_first_working  # noqa: E402
from bandarr.finder import find_best  # noqa: E402
from bandarr.gate import check  # noqa: E402
from bandarr.scoring import Wanted  # noqa: E402
from bandarr.slskd import Slskd  # noqa: E402
from bandarr.state import State  # noqa: E402

log = logging.getLogger("gate_lab")


def env_file() -> dict[str, str]:
    out = {}
    for line in (ROOT / ".env").read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def spectrogram(src: Path, png: Path) -> bool:
    ffmpeg = os.environ.get("FFMPEG", "ffmpeg")
    p = subprocess.run(
        [ffmpeg, "-nostdin", "-loglevel", "error", "-y", "-i", str(src), "-lavfi",
         "showspectrumpic=s=1000x512:legend=1:scale=log:fscale=lin", str(png)],
        capture_output=True, text=True, stdin=subprocess.DEVNULL)
    return p.returncode == 0 and png.exists()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--user", default=None)
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")

    env = env_file()
    lab = ROOT / "data" / "gate_lab"
    lab.mkdir(parents=True, exist_ok=True)
    st = State(lab / "lab.db")
    client = Slskd("http://127.0.0.1:5030", env["SLSKD_API_KEY"])
    user = a.user or env.get("BANDCAMP_USER")

    tracks, failed = fetch_tracks(user, known_item_ids=st.known_item_ids())
    st.add_tracks(tracks)
    log.info("lab db: %d tracks (%d albums failed to expand)", len(st.by_status("pending", "done", "not_found")), len(failed))

    # one track per album: the first one
    rows = st.db.execute(
        "SELECT * FROM tracks GROUP BY item_id HAVING track_num = MIN(track_num) ORDER BY item_id LIMIT ?",
        (a.limit,)).fetchall()
    downloads = ROOT / "data" / "slskd" / "downloads"
    summary = []
    for i, row in enumerate(rows, 1):
        key = row["wishlist_key"]
        label = f"{row['artist']} - {row['title']}"[:60]
        log.info("[%d/%d] %s", i, len(rows), label)
        try:
            ranked = find_best(client, Wanted.from_mapping(row), gap=2)
            if not ranked:
                st.update(key, status="not_found")
                summary.append((label, "NOT FOUND", "", "", "", ""))
                continue
            d = download_first_working(client, ranked, key, downloads, queue_timeout=120, total_timeout=600)
            res = check(d.path, "soulseek")
            check_id = st.record_gate(key, str(d.path), res)
            png = d.path.with_suffix(".png")
            has_png = spectrogram(d.path, png)
            c = d.scored.candidate
            summary.append((label, f"id={check_id}", f"{c.ext} {c.bit_depth or ''}/{c.sample_rate or ''}",
                            f"LV={res.lv_verdict} {res.cutoff_hz/1000:.0f}k", f"FC={res.fc_verdict} {res.fc_confidence or 0:.2f}",
                            str(png if has_png else d.path)))
        except DownloadFailed as e:
            log.warning("download failed: %s", e)
            summary.append((label, "DOWNLOAD FAILED", "", "", "", str(e)[:80]))
        except Exception as e:  # keep the campaign going
            log.exception("track failed")
            summary.append((label, "ERROR", "", "", "", str(e)[:80]))

    print("\n=== SUMMARY ===")
    for s in summary:
        print(" | ".join(s))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
