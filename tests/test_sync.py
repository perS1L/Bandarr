import shutil
import time
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from bandarr import convert, gate
from bandarr.bandcamp import Track
from bandarr.config import Config
from bandarr.downloader import DownloadFailed, peer_dest, safe_dir
from bandarr.gate import GateResult
from bandarr.state import State
from bandarr.sync import Deps, run_once


def track(n, album="Al", item="1", **kw):
    base = dict(wishlist_key=f"{item}:{n}", artist="Art", title=f"T{n}", album=album, album_artist="Art",
                track_num=n, duration=100.0, item_type="album", url="u")
    return Track(**{**base, **kw})


@pytest.fixture
def env(tmp_path):
    cfg = Config(bandcamp_user="u", bandcamp_identity=None, slskd_url="x", slskd_api_key="k",
                 downloads_dir=tmp_path / "dl", master_dir=tmp_path / "master",
                 review_dir=tmp_path / "review", state_db=tmp_path / "s.db", covers_dir=tmp_path / "covers",
                 replaced_dir=tmp_path / "replaced", originals_dir=tmp_path / "originals",
                 download_workers=1)  # sequential: deterministic; parallel runs have their own tests
    return cfg, State(cfg.state_db), tmp_path


class Fakes:
    """Replaceable pipeline parts that record what they were asked to do.

    peers: Soulseek peers in ranking order. Each file written by `download` carries the
    peer name, so `gate` can give a per-peer verdict (gate_by_peer) like the real detectors.
    """

    def __init__(self, cfg, tracks, ext=".flac", gate_result=None, found=True, peers=("p1",)):
        self.cfg, self.tracks, self.ext, self.found, self.peers = cfg, tracks, ext, found, list(peers)
        self.gate_result = gate_result or GateResult("pass", "ok", rule="both", lv_verdict="PASS", fc_verdict="GENUINE")
        self.gate_by_peer = {}
        self.calls = []
        self.downloaded_from = []
        self.fail_download = set()
        self.explode = set()
        self.rate = 44100
        self.not_flac = False
        self.cover = b"\xff\xd8jpg"
        self.cover_used = []
        self.queued_peers = set()  # lossless peers whose transfer is stuck in their queue
        self.tiers = {}  # peer -> scoring tier (default 3 = lossless)
        self.ext_by_peer = {}  # peer -> file extension (default self.ext)
        self.queued_calls = []
        self.bad_keys = set()  # tracks whose copies the gate refuses (the verdict follows the track, not the peer)
        self.folder_by_title = {}  # title -> folder on the peer's disk (default: none)

    def deps(self):
        return Deps(fetch_tracks=self.fetch, find_best=self.find, download=self.download,
                    gate_check=self.gate, quarantine=gate.quarantine, flac_to_aiff=self.to_aiff,
                    probe=lambda p: SimpleNamespace(sample_rate=self.rate), tag_and_place=self.tag,
                    get_cover=lambda art, d: self.cover, fetch_items=lambda u, i=None: self._wishlist(),
                    locate=self.locate)

    items = None  # None: the wishlist is made of the albums of `self.tracks`

    def _wishlist(self):
        if self.items is not None:
            return self.items
        return [{"item_id": i} for i in dict.fromkeys(t.wishlist_key.split(":")[0] for t in self.tracks)]

    def fetch(self, user, identity=None, known_item_ids=None, items=None):
        return [t for t in self.tracks if t.wishlist_key.split(":")[0] not in (known_item_ids or set())], []

    def find(self, client, w):
        self.calls.append(("find", w.title))
        folder = self.folder_by_title.get(w.title)
        prefix = f"{folder}\\" if folder else ""
        return [SimpleNamespace(candidate=SimpleNamespace(username=u, filename=f"{prefix}{u}{self.ext_by_peer.get(u, self.ext)}", size=1),
                                tier=self.tiers.get(u, 3), score=300.0) for u in self.peers] if self.found else []

    def locate(self, root, dest, filename):
        hits = [p for p in (Path(root) / dest).rglob("*") if p.is_file()]
        if len(hits) != 1:
            raise DownloadFailed("no file")
        return hits[0]

    def download(self, client, ranked, key, root, exclude=frozenset(), on_queued=None, on_failed=None, **kw):
        self.calls.append(("download", key))
        if key in self.explode:
            raise RuntimeError("boom")
        if key in self.fail_download:
            raise DownloadFailed("all peers failed")
        for sc in ranked:
            c = sc.candidate
            if (c.username, c.filename) in exclude:
                continue
            if c.username in self.queued_peers:
                dest = peer_dest(key, c.username)
                self.queued_calls.append(c.username)
                if on_queued and on_queued(sc, f"batch-{c.username}", dest):
                    (Path(root) / dest).mkdir(parents=True, exist_ok=True)  # slskd's incoming folder
                continue
            self.downloaded_from.append(c.username)
            d = Path(root) / peer_dest(key, c.username)
            d.mkdir(parents=True, exist_ok=True)
            p = d / f"file{self.ext_by_peer.get(c.username, self.ext)}"
            p.write_bytes(c.username.encode())
            return SimpleNamespace(path=p, scored=sc)
        raise DownloadFailed("no candidate left")

    def gate(self, path, source):
        self.calls.append(("gate", path.suffix))
        if path.suffix not in (".flac", ".aiff", ".aif"):
            return GateResult("n/a", "not lossless")
        if any(safe_dir(k) in path.parent.name for k in self.bad_keys):
            return BAD
        return self.gate_by_peer.get(path.read_bytes().decode(), self.gate_result)

    def to_aiff(self, p):
        self.calls.append(("convert", p.name))
        if self.not_flac:
            raise convert.NotFlac("codec is 'mp3'")
        out = p.with_suffix(".aiff")
        out.write_bytes(b"aiff")
        return out

    def tag(self, path, row, master, source, compilation, cover=None):
        self.calls.append(("tag", source))
        self.cover_used.append(cover)
        master.mkdir(parents=True, exist_ok=True)
        dest = master / f"{row['artist']} - {row['title']}{path.suffix}"
        shutil.move(str(path), dest)
        return dest

    def names(self):
        return [c[0] for c in self.calls]


def row_of(st, key):
    return st.db.execute("SELECT * FROM tracks WHERE wishlist_key=?", (key,)).fetchone()


def test_happy_path_flac_is_gated_converted_tagged_and_recorded(env):
    cfg, st, _ = env
    f = Fakes(cfg, [track(1)])
    rep = run_once(cfg, st, None, f.deps())
    r = row_of(st, "1:1")
    assert rep.done == 1 and (r["status"], r["source"], r["format"], r["gate_verdict"]) == ("done", "soulseek", "aiff", "pass")
    assert Path(r["path"]).name == "Art - T1.aiff" and Path(r["path"]).exists()
    assert f.names() == ["find", "download", "gate", "convert", "tag"]
    assert not list((cfg.downloads_dir).rglob("*.flac"))  # intermediate FLAC removed
    assert st.db.execute("SELECT COUNT(*) FROM gate_checks").fetchone()[0] == 1


def test_refused_file_goes_to_review_and_is_never_converted_or_deleted(env):
    cfg, st, _ = env
    f = Fakes(cfg, [track(1)], gate_result=GateResult("fail", "refused", rule="both", lv_verdict="FAIL", fc_verdict="TRANSCODED"))
    rep = run_once(cfg, st, None, f.deps())
    r = row_of(st, "1:1")
    assert rep.in_review == 1 and r["status"] == "in_review" and r["gate_verdict"] == "fail"
    assert "convert" not in f.names() and "tag" not in f.names()
    assert Path(r["path"]).exists() and Path(r["path"]).parent == cfg.review_dir / "fail"
    assert not (cfg.master_dir.exists() and any(cfg.master_dir.iterdir()))


def test_not_found_is_recorded_without_downloading(env):
    cfg, st, _ = env
    f = Fakes(cfg, [track(1)], found=False)
    rep = run_once(cfg, st, None, f.deps())
    assert rep.not_found == 1 and row_of(st, "1:1")["status"] == "not_found" and "download" not in f.names()


def test_failed_download_stays_pending_and_other_tracks_continue(env):
    cfg, st, _ = env
    f = Fakes(cfg, [track(1), track(2)])
    f.fail_download.add("1:1")
    rep = run_once(cfg, st, None, f.deps())
    assert rep.failed == ["1:1"] and rep.done == 1
    assert row_of(st, "1:1")["status"] == "pending" and row_of(st, "1:2")["status"] == "done"


def test_unexpected_error_on_one_track_does_not_stop_the_run(env):
    cfg, st, _ = env
    f = Fakes(cfg, [track(1), track(2)])
    f.explode.add("1:1")
    rep = run_once(cfg, st, None, f.deps())
    assert rep.failed == ["1:1"] and rep.done == 1


def test_mp3_fallback_is_not_gated_converted_nor_recorded_as_gate_check(env):
    cfg, st, _ = env
    f = Fakes(cfg, [track(1)], ext=".mp3")
    run_once(cfg, st, None, f.deps())
    r = row_of(st, "1:1")
    assert (r["status"], r["format"], r["gate_verdict"]) == ("done", "mp3", "n/a")
    assert "convert" not in f.names()
    assert st.db.execute("SELECT COUNT(*) FROM gate_checks").fetchone()[0] == 0


def test_converter_refusing_a_fake_flac_sends_it_to_review(env):
    cfg, st, _ = env
    f = Fakes(cfg, [track(1)])
    f.not_flac = True
    run_once(cfg, st, None, f.deps())
    r = row_of(st, "1:1")
    assert r["status"] == "in_review" and r["gate_verdict"] == "fail" and Path(r["path"]).exists()


def test_aiff_from_soulseek_is_kept_if_playable_and_reviewed_if_not(env):
    cfg, st, _ = env
    f = Fakes(cfg, [track(1), track(2)], ext=".aiff")
    run_once(cfg, st, None, f.deps())
    assert row_of(st, "1:1")["format"] == "aiff" and "convert" not in f.names()
    cfg2, st2 = cfg, State(cfg.state_db.with_name("s2.db"))
    f2 = Fakes(cfg2, [track(1)], ext=".aiff")
    f2.rate = 96000
    run_once(cfg2, st2, None, f2.deps())
    assert row_of(st2, "1:1")["status"] == "in_review"


def test_second_run_does_not_redo_known_albums_or_done_tracks(env):
    cfg, st, _ = env
    f = Fakes(cfg, [track(1)])
    run_once(cfg, st, None, f.deps())
    f.calls.clear()
    rep = run_once(cfg, st, None, f.deps())
    assert rep.new_tracks == 0 and f.calls == []


def test_limit_caps_tracks_per_run(env):
    cfg, st, _ = env
    cfg.limit = 2
    f = Fakes(cfg, [track(i) for i in range(1, 6)])
    assert run_once(cfg, st, None, f.deps()).done == 2
    assert st.summary() == {"done": 2, "pending": 3}


def test_dry_run_reads_the_wishlist_but_touches_nothing_else(env):
    cfg, st, _ = env
    f = Fakes(cfg, [track(1)])
    rep = run_once(cfg, st, None, f.deps(), dry_run=True)
    assert rep.new_tracks == 1 and f.calls == [] and st.summary() == {"pending": 1}


def test_crash_between_download_and_import_is_recovered(env):
    cfg, st, _ = env
    f = Fakes(cfg, [track(1)])
    run_once(cfg, st, None, f.deps(), dry_run=True)
    st.update("1:1", status="downloaded", source="soulseek")
    (cfg.downloads_dir / peer_dest("1:1", "p1")).mkdir(parents=True)
    (cfg.downloads_dir / peer_dest("1:1", "p1") / "stale.flac").write_bytes(b"old")
    assert run_once(cfg, st, None, f.deps()).done == 1


def test_retry_not_found_flag_requeues_them(env):
    cfg, st, _ = env
    f = Fakes(cfg, [track(1)], found=False)
    run_once(cfg, st, None, f.deps())
    assert st.summary() == {"not_found": 1}
    f.found, cfg.retry_not_found = True, True
    assert run_once(cfg, st, None, f.deps()).done == 1


def test_bandcamp_tracks_are_never_touched(env):
    cfg, st, _ = env
    f = Fakes(cfg, [track(1)])
    run_once(cfg, st, None, f.deps(), dry_run=True)
    st.update("1:1", status="done", source="bandcamp", format="aiff")
    cfg.retry_not_found = True
    f.calls.clear()
    run_once(cfg, st, None, f.deps())
    assert f.calls == [] and row_of(st, "1:1")["source"] == "bandcamp"


# --- everything real except search/download: gate + convert + tagger ------------------

needs_ffmpeg = pytest.mark.skipif(not shutil.which(convert.FFMPEG), reason="ffmpeg not available")


@needs_ffmpeg
def test_real_pipeline_from_downloaded_flac_to_tagged_aiff_in_master(env):
    from bandarr import tagger
    cfg, st, tmp = env
    src = tmp / "full.flac"
    subprocess.run([convert.FFMPEG, "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "anoisesrc=color=white:amplitude=0.3:sample_rate=44100:duration=8", "-c:a", "flac",
                    "-metadata", "title=GARBAGE", str(src)], check=True)
    f = Fakes(cfg, [track(1, artist="Connor Wall", title="Oak")])

    def download(client, ranked, key, root, **kw):
        d = Path(root) / peer_dest(key, "peer")
        d.mkdir(parents=True, exist_ok=True)
        shutil.copy(src, d / "x.flac")
        return SimpleNamespace(path=d / "x.flac", scored=SimpleNamespace(
            candidate=SimpleNamespace(username="peer", filename="x.flac", size=1), tier=3, score=1.0))

    deps = f.deps()
    deps.download, deps.gate_check, deps.flac_to_aiff = download, gate.check, convert.flac_to_aiff
    deps.probe, deps.tag_and_place = convert.probe, tagger.tag_and_place
    assert run_once(cfg, st, None, deps).done == 1
    out = Path(row_of(st, "1:1")["path"])
    assert out == cfg.master_dir / "Connor Wall - Oak.aiff" and out.exists()
    assert tagger.read_source(out) == "soulseek"
    assert not list(cfg.downloads_dir.rglob("*.flac"))


# --- preflight: a broken tool must stop the run, not flood the review folder -------------

def test_preflight_reports_a_missing_tool_and_an_unreachable_slskd(env, monkeypatch):
    from bandarr import sync
    cfg, _, _ = env
    monkeypatch.setattr(sync.convert, "FFMPEG", "/nonexistent/ffmpeg")
    monkeypatch.setattr(sync.convert, "FFPROBE", "/nonexistent/ffprobe")

    class Down:
        def _req(self, *a, **k):
            raise sync.SlskdError("connection refused")

    problems = sync.preflight(cfg, Down())
    text = " | ".join(problems)
    assert "ffmpeg" in text and "ffprobe" in text and "slskd is not reachable" in text and "downloads dir" in text


def test_preflight_passes_when_everything_works(env, monkeypatch):
    from bandarr import sync
    cfg, _, tmp = env
    cfg.downloads_dir.mkdir()
    monkeypatch.setattr(sync.convert, "FFMPEG", "true")
    monkeypatch.setattr(sync.convert, "FFPROBE", "true")

    class Up:
        def _req(self, *a, **k):
            return {"server": {"state": "Connected, LoggedIn"}}

    assert sync.preflight(cfg, Up()) == []


def test_config_paths_are_absolute(monkeypatch):
    monkeypatch.chdir("/tmp")
    cfg = Config.from_env({"BANDCAMP_USER": "u", "SLSKD_API_KEY": "k"})
    assert all(p.is_absolute() for p in (cfg.downloads_dir, cfg.master_dir, cfg.review_dir, cfg.state_db))


# --- a refused copy does not end the track: the next peer is tried ----------------------

BAD = GateResult("fail", "refused", rule="both", lv_verdict="FAIL", fc_verdict="TRANSCODED")


def test_refused_copy_falls_back_to_the_next_peer_and_keeps_the_evidence(env):
    cfg, st, _ = env
    f = Fakes(cfg, [track(1)], peers=("bad", "good"))
    f.gate_by_peer = {"bad": BAD}
    rep = run_once(cfg, st, None, f.deps())
    r = row_of(st, "1:1")
    assert rep.done == 1 and r["status"] == "done" and f.downloaded_from == ["bad", "good"]
    assert Path(r["path"]).parent == cfg.master_dir
    kept = list((cfg.review_dir / "fail").glob("*.flac"))  # refused copy is kept, never deleted
    assert len(kept) == 1
    assert "copy from peer bad" in (cfg.review_dir / "fail" / (kept[0].name + ".verdict.json")).read_text()
    assert st.db.execute("SELECT COUNT(*) FROM gate_checks WHERE wishlist_key='1:1'").fetchone()[0] == 2


def test_all_peers_refused_sends_the_track_to_review_after_max_tries(env):
    cfg, st, _ = env
    cfg.max_source_tries = 2
    f = Fakes(cfg, [track(1)], peers=("a", "b", "c"), gate_result=BAD)
    rep = run_once(cfg, st, None, f.deps())
    assert rep.in_review == 1 and f.downloaded_from == ["a", "b"]  # c never tried
    assert row_of(st, "1:1")["status"] == "in_review"
    assert len(list((cfg.review_dir / "fail").glob("*.flac"))) == 2
    assert not list(cfg.master_dir.glob("*")) if cfg.master_dir.exists() else True


def test_refusals_then_no_more_downloadable_peer_still_ends_in_review_not_retry(env):
    cfg, st, _ = env
    f = Fakes(cfg, [track(1)], peers=("only",), gate_result=BAD)
    assert run_once(cfg, st, None, f.deps()).in_review == 1


def test_the_same_peer_is_not_tried_twice_after_refusal(env):
    cfg, st, _ = env
    f = Fakes(cfg, [track(1)], peers=("a", "a", "b"))
    f.gate_by_peer = {"a": BAD}
    run_once(cfg, st, None, f.deps())
    assert f.downloaded_from == ["a", "b"]


# --- covers ----------------------------------------------------------------------------

def test_cover_is_passed_to_the_tagger_and_a_missing_cover_never_blocks(env):
    cfg, st, _ = env
    f = Fakes(cfg, [track(1), track(2)])
    run_once(cfg, st, None, f.deps(), dry_run=True)
    f.cover = None
    assert run_once(cfg, st, None, f.deps()).done == 2 and f.cover_used == [None, None]
    cfg2 = Config(**{**cfg.__dict__, "state_db": cfg.state_db.with_name("c.db"), "master_dir": cfg.master_dir / "x",
                     "downloads_dir": cfg.downloads_dir / "x"})
    st2 = State(cfg2.state_db)
    g = Fakes(cfg2, [track(1)])
    run_once(cfg2, st2, None, g.deps())
    assert g.cover_used == [b"\xff\xd8jpg"]


def test_art_ids_are_backfilled_for_rows_stored_before_covers(env):
    cfg, st, _ = env
    f = Fakes(cfg, [track(1, item="42")])  # stored without a cover id, like rows from before covers
    run_once(cfg, st, None, f.deps(), dry_run=True)
    assert row_of(st, "42:1")["art_id"] is None
    f.items = [{"item_id": 42, "item_art_id": 777}]
    run_once(cfg, st, None, f.deps(), dry_run=True)
    assert row_of(st, "42:1")["art_id"] == "777"


def test_state_migrates_a_database_created_before_covers(tmp_path):
    import sqlite3
    db = sqlite3.connect(tmp_path / "old.db")
    db.executescript("""CREATE TABLE items (item_id TEXT PRIMARY KEY, expanded_at REAL NOT NULL);
        CREATE TABLE tracks (wishlist_key TEXT PRIMARY KEY, item_id TEXT NOT NULL, artist TEXT NOT NULL,
          title TEXT NOT NULL, album TEXT NOT NULL, album_artist TEXT NOT NULL, track_num INTEGER,
          duration REAL, alt_artist TEXT, alt_title TEXT, url TEXT, status TEXT NOT NULL DEFAULT 'pending',
          source TEXT, format TEXT, gate_verdict TEXT, path TEXT, updated_at REAL NOT NULL);""")
    db.commit()
    db.close()
    st = State(tmp_path / "old.db")
    assert "art_id" in {r[1] for r in st.db.execute("PRAGMA table_info(tracks)")}


# --- growing delay for tracks that fail without result ---------------------------------------------

def test_retry_delay_doubles_from_one_hour_up_to_the_cap(env):
    cfg, st, _ = env
    st.add_tracks([track(1)])
    delays = [st.schedule_retry("1:1", 1, 24, now=1000.0) for _ in range(7)]
    assert delays == [1, 2, 4, 8, 16, 24, 24]
    assert row_of(st, "1:1")["retry_count"] == 7 and row_of(st, "1:1")["retry_after"] == 1000.0 + 24 * 3600


def test_a_failing_track_is_deferred_not_researched_every_run_then_retried_when_due(env):
    cfg, st, _ = env
    f = Fakes(cfg, [track(1), track(2)])
    f.fail_download.add("1:1")
    run_once(cfg, st, None, f.deps())
    assert row_of(st, "1:1")["retry_count"] == 1 and row_of(st, "1:2")["status"] == "done"
    assert st.deferred_count() == 1 and st.to_fetch() == []

    f.calls.clear()
    rep = run_once(cfg, st, None, f.deps())  # a moment later: still held back, nothing searched
    assert f.calls == [] and rep.failed == []

    st.db.execute("UPDATE tracks SET retry_after = ? WHERE wishlist_key = '1:1'", (time.time() - 1,))
    st.db.commit()
    rep = run_once(cfg, st, None, f.deps())  # due again, fails again: the delay doubles
    assert rep.failed == ["1:1"] and row_of(st, "1:1")["retry_count"] == 2
    assert row_of(st, "1:1")["retry_after"] - time.time() > 1.9 * 3600


def test_success_clears_the_retry_state(env):
    cfg, st, _ = env
    f = Fakes(cfg, [track(1)])
    f.fail_download.add("1:1")
    run_once(cfg, st, None, f.deps())
    st.db.execute("UPDATE tracks SET retry_after = ? WHERE wishlist_key = '1:1'", (time.time() - 1,))
    st.db.commit()
    f.fail_download.clear()
    assert run_once(cfg, st, None, f.deps()).done == 1
    r = row_of(st, "1:1")
    assert (r["retry_count"], r["retry_after"], r["status"]) == (0, None, "done")


def test_an_unexpected_error_also_backs_off(env):
    cfg, st, _ = env
    f = Fakes(cfg, [track(1)])
    f.explode.add("1:1")
    run_once(cfg, st, None, f.deps())
    assert row_of(st, "1:1")["retry_count"] == 1 and st.to_fetch() == []


def test_state_migrates_retry_columns_on_an_older_database(tmp_path):
    import sqlite3
    db = sqlite3.connect(tmp_path / "old.db")
    db.executescript("""CREATE TABLE items (item_id TEXT PRIMARY KEY, expanded_at REAL NOT NULL);
        CREATE TABLE tracks (wishlist_key TEXT PRIMARY KEY, item_id TEXT NOT NULL, artist TEXT NOT NULL,
          title TEXT NOT NULL, album TEXT NOT NULL, album_artist TEXT NOT NULL, track_num INTEGER,
          duration REAL, alt_artist TEXT, alt_title TEXT, url TEXT, art_id TEXT,
          status TEXT NOT NULL DEFAULT 'pending', source TEXT, format TEXT, gate_verdict TEXT, path TEXT,
          updated_at REAL NOT NULL);
        INSERT INTO items VALUES ('1', 0);
        INSERT INTO tracks (wishlist_key,item_id,artist,title,album,album_artist,updated_at)
          VALUES ('1:1','1','a','t','al','a',0);""")
    db.commit()
    db.close()
    st = State(tmp_path / "old.db")
    cols = {r[1] for r in st.db.execute("PRAGMA table_info(tracks)")}
    assert {"retry_count", "retry_after", "upgrade_checked_at"} <= cols
    assert [r["wishlist_key"] for r in st.to_fetch()] == ["1:1"]  # existing rows are due


def test_not_found_tracks_are_listed_in_a_file_and_searched_again_after_the_retry_delay(env):
    cfg, st, _ = env
    cfg.upgrade = False  # the retry delay must not depend on the upgrade loop
    f = Fakes(cfg, [track(1)], found=False)
    run_once(cfg, st, None, f.deps())
    txt = (cfg.state_db.parent / "not_found.txt").read_text(encoding="utf-8")
    assert "1 track(s)" in txt and "T1" in txt and "never" not in txt  # searched once: a date is shown

    f.found, cfg.not_found_retry_h = True, 7
    assert run_once(cfg, st, None, f.deps()).done == 0 and st.summary() == {"not_found": 1}  # too early
    st.db.execute("UPDATE tracks SET upgrade_checked_at = upgrade_checked_at - 8 * 3600")
    st.db.commit()
    assert run_once(cfg, st, None, f.deps()).done == 1
    assert "nothing" in (cfg.state_db.parent / "not_found.txt").read_text(encoding="utf-8")


def test_a_blocked_peer_is_never_used_as_a_source(env):
    cfg, st, _ = env
    cfg.blocked_peers = frozenset({"p1"})
    f = Fakes(cfg, [track(1)], peers=("p1", "p2"))
    assert run_once(cfg, st, None, f.deps()).done == 1
    assert f.downloaded_from == ["p2"]


def test_a_queued_transfer_of_a_peer_blocked_later_is_dropped(env):
    from test_upgrade import FakeBatches
    cfg, st, _ = env
    f = Fakes(cfg, [track(1)], peers=("slow",))
    f.queued_peers = {"slow"}
    client = FakeBatches()
    client.states["batch-slow"] = "Queued, Remotely"
    run_once(cfg, st, client, f.deps())
    assert st.queued_keys() == {"1:1"}
    cfg.blocked_peers = frozenset({"slow"})
    run_once(cfg, st, client, f.deps())
    assert st.queued_keys() == set() and client.cancelled == ["t-batch-slow"]


def test_gate_none_trusts_the_copy_records_no_measurement_but_the_converter_still_checks_the_codec(env):
    cfg, st, _ = env
    off = GateResult("n/a", "gate disabled", rule="none")
    f = Fakes(cfg, [track(1), track(2)], gate_result=off)
    f.bad_keys = set()
    rep = run_once(cfg, st, None, f.deps())
    assert rep.done == 2 and row_of(st, "1:1")["format"] == "aiff" and row_of(st, "1:1")["gate_verdict"] == "n/a"
    assert st.db.execute("SELECT COUNT(*) FROM gate_checks").fetchone()[0] == 0  # not a measurement
    # a MP3 renamed .flac is still stopped by the converter (critical invariant)
    cfg2 = cfg
    f2 = Fakes(cfg2, [track(1, item="2", title="Other")], gate_result=off)
    f2.not_flac = True
    rep2 = run_once(cfg2, st, None, f2.deps())
    assert row_of(st, "2:1")["status"] == "in_review" and rep2.in_review == 1
