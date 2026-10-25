"""Queued transfers, the upgrade search, and the downloader's queue handling."""
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_sync import BAD, Fakes, env, row_of, track  # noqa: F401  (env is a fixture)

from bandarr.downloader import DownloadFailed, download_first_working, peer_dest
from bandarr.slskd import SlskdError
from bandarr.sync import run_once


# --- downloader, with a fake slskd ----------------------------------------------------------

class FakeSlskd:
    """behaviors: peer -> ok | queued | rejected. 'ok' puts the file in place at enqueue."""

    def __init__(self, root, behaviors):
        self.root, self.behaviors, self.cancelled, self.enqueued = Path(root), behaviors, [], []

    def enqueue(self, username, files, dest, external_id=None):
        self.enqueued.append(username)
        if self.behaviors[username] == "ok":
            d = self.root / dest
            d.mkdir(parents=True, exist_ok=True)
            (d / files[0]["filename"].replace("\\", "/").rsplit("/", 1)[-1]).write_bytes(b"x")
        return {"id": f"b-{username}", "username": username}

    def batch(self, bid):
        u = bid[2:]
        state = {"ok": "Completed, Succeeded", "queued": "Queued, Remotely",
                 "rejected": "Completed, Rejected"}[self.behaviors[u]]
        return {"transfers": [{"id": f"t-{u}", "username": u, "state": state, "placeInQueue": 7,
                               "exception": "Banned" if state.endswith("Rejected") else None}]}

    def cancel(self, username, tid, remove=True):
        self.cancelled.append(username)


def cand(user):
    return SimpleNamespace(candidate=SimpleNamespace(username=user, filename=f"m\\{user}.flac", size=1),
                           score=300.0, tier=3)


def dl(client, users, tmp_path, **kw):
    return download_first_working(client, [cand(u) for u in users], "1:1", tmp_path,
                                  queue_timeout=0.02, poll=0, **kw)


def test_queued_transfer_is_kept_when_the_callback_asks_for_it(tmp_path):
    c = FakeSlskd(tmp_path, {"a": "queued", "b": "ok"})
    seen = []
    got = dl(c, ["a", "b"], tmp_path, on_queued=lambda s, bid, dest: seen.append((bid, dest)) or True)
    assert got.scored.candidate.username == "b" and c.cancelled == []
    assert seen == [("b-a", peer_dest("1:1", "a"))]


def test_queued_transfer_is_cancelled_without_a_callback(tmp_path):
    c = FakeSlskd(tmp_path, {"a": "queued", "b": "ok"})
    assert dl(c, ["a", "b"], tmp_path).scored.candidate.username == "b"
    assert c.cancelled == ["a"]


def test_rejected_transfer_is_reported_and_cancelled(tmp_path):
    c = FakeSlskd(tmp_path, {"a": "rejected", "b": "ok"})
    failed = []
    dl(c, ["a", "b"], tmp_path, on_failed=lambda s, why: failed.append((s.candidate.username, why)))
    assert failed == [("a", "Completed, Rejected: Banned")] and c.cancelled == ["a"]


def test_excluded_sources_are_never_enqueued(tmp_path):
    c = FakeSlskd(tmp_path, {"a": "ok", "b": "ok"})
    got = dl(c, ["a", "b"], tmp_path, exclude={("a", "m\\a.flac")})
    assert got.scored.candidate.username == "b" and c.enqueued == ["b"]


def test_nothing_downloadable_raises(tmp_path):
    c = FakeSlskd(tmp_path, {"a": "queued"})
    with pytest.raises(DownloadFailed):
        dl(c, ["a"], tmp_path, on_queued=lambda *a: True)


# --- sync: queued transfers and upgrades ------------------------------------------------------

class FakeBatches:
    """slskd as seen by collect_queued: batch id -> transfer state text."""

    def __init__(self):
        self.states, self.cancelled = {}, []

    def batch(self, bid):
        if bid not in self.states:
            raise SlskdError("HTTP 404")
        return {"transfers": [{"id": f"t-{bid}", "username": "u", "state": self.states[bid]}]}

    def cancel(self, username, tid, remove=True):
        self.cancelled.append(tid)


MIXED = dict(peers=("flac", "mp3"), )


def mp3_in_master(cfg, st, **kw):
    """Run once with only an MP3 available; returns the Fakes used (upgrade off)."""
    cfg.upgrade = False
    f = Fakes(cfg, [track(1)], ext=".mp3", **kw)
    assert run_once(cfg, st, FakeBatches(), f.deps()).done == 1
    return f


def lossless_fakes(cfg, **kw):
    f = Fakes(cfg, [track(1)], peers=("flac", "mp3"), **kw)
    f.tiers, f.ext_by_peer = {"mp3": 2}, {"flac": ".flac", "mp3": ".mp3"}
    return f


def test_queued_lossless_is_kept_and_the_mp3_fallback_is_upgraded_when_it_arrives(env):
    cfg, st, _ = env
    cfg.upgrade = False
    f = lossless_fakes(cfg)
    f.queued_peers = {"flac"}
    client = FakeBatches()
    rep = run_once(cfg, st, client, f.deps())
    r = row_of(st, "1:1")
    assert rep.done == 1 and r["format"] == "mp3" and st.queued_keys() == {"1:1"}
    assert (cfg.downloads_dir / peer_dest("1:1", "flac")).is_dir()  # the waiting transfer's folder survives cleanup
    old_mp3 = Path(r["path"])

    client.states["batch-flac"] = "Queued, Remotely"
    assert run_once(cfg, st, client, f.deps()).collected == 0 and st.queued_keys() == {"1:1"}  # still waiting

    client.states["batch-flac"] = "Completed, Succeeded"
    (cfg.downloads_dir / peer_dest("1:1", "flac") / "flac.flac").write_bytes(b"flac")
    rep = run_once(cfg, st, client, f.deps())
    r = row_of(st, "1:1")
    assert rep.collected == 1 and rep.upgraded == 1 and st.queued_keys() == set()
    assert (r["format"], r["status"], r["source"]) == ("aiff", "done", "soulseek")
    assert Path(r["path"]).name == "Art - T1.aiff" and Path(r["path"]).exists()
    assert not old_mp3.exists() and (cfg.replaced_dir / old_mp3.name).exists()  # moved, not deleted
    assert not (cfg.downloads_dir / peer_dest("1:1", "flac")).exists()


def test_delete_replaced_option_removes_the_old_file_instead(env):
    cfg, st, _ = env
    cfg.delete_replaced = True
    mp3_in_master(cfg, st)
    old = Path(row_of(st, "1:1")["path"])
    cfg.upgrade, cfg.upgrade_interval_h = True, 0
    f = lossless_fakes(cfg)
    assert run_once(cfg, st, FakeBatches(), f.deps()).upgraded == 1
    assert not old.exists() and not cfg.replaced_dir.exists()


def test_upgrade_search_replaces_an_mp3_with_an_immediately_available_lossless(env):
    cfg, st, _ = env
    mp3_in_master(cfg, st)
    old = Path(row_of(st, "1:1")["path"])
    cfg.upgrade, cfg.upgrade_interval_h = True, 0
    f = lossless_fakes(cfg)
    rep = run_once(cfg, st, FakeBatches(), f.deps())
    r = row_of(st, "1:1")
    assert rep.upgraded == 1 and f.downloaded_from == ["flac"]  # the MP3 peer was not even considered
    assert r["format"] == "aiff" and not old.exists() and (cfg.replaced_dir / old.name).exists()


def test_a_refused_lossless_upgrade_leaves_the_mp3_untouched_and_is_not_retried(env):
    cfg, st, _ = env
    mp3_in_master(cfg, st)
    before = row_of(st, "1:1")
    cfg.upgrade, cfg.upgrade_interval_h = True, 0
    f = lossless_fakes(cfg)
    f.gate_by_peer = {"flac": BAD}
    rep = run_once(cfg, st, FakeBatches(), f.deps())
    after = row_of(st, "1:1")
    assert rep.upgraded == 0 and (after["format"], after["status"], after["path"]) == (before["format"], "done", before["path"])
    assert Path(after["path"]).exists() and list((cfg.review_dir / "fail").glob("*.flac"))
    assert ("flac", "flac.flac") in st.tried_sources("1:1")
    f.downloaded_from.clear()
    run_once(cfg, st, FakeBatches(), f.deps())  # interval 0: searched again, but nothing new to try
    assert f.downloaded_from == []


def test_upgrade_search_is_throttled_by_the_interval(env):
    cfg, st, _ = env
    mp3_in_master(cfg, st)
    cfg.upgrade, cfg.upgrade_interval_h = True, 24
    f = Fakes(cfg, [track(1)], ext=".mp3")
    f.tiers = {"p1": 2}
    run_once(cfg, st, FakeBatches(), f.deps())
    searches = f.names().count("find")
    run_once(cfg, st, FakeBatches(), f.deps())
    assert searches == 1 and f.names().count("find") == 1  # not searched again within 24 h


def test_bandcamp_tracks_are_never_upgraded_and_their_queued_transfers_are_dropped(env):
    cfg, st, _ = env
    mp3_in_master(cfg, st)
    st.db.execute("UPDATE tracks SET source='bandcamp' WHERE wishlist_key='1:1'")
    st.db.commit()
    st.add_queued("1:1", "batch-flac", "flac", "flac.flac", 1, peer_dest("1:1", "flac"))
    cfg.upgrade, cfg.upgrade_interval_h = True, 0
    f = lossless_fakes(cfg)
    client = FakeBatches()
    client.states["batch-flac"] = "Completed, Succeeded"
    rep = run_once(cfg, st, client, f.deps())
    assert rep.upgraded == 0 and rep.collected == 0 and st.queued_keys() == set()
    assert row_of(st, "1:1")["format"] == "mp3" and "find" not in f.names()


def test_a_transfer_waiting_longer_than_the_limit_is_given_up_on(env):
    cfg, st, _ = env
    mp3_in_master(cfg, st)
    st.add_queued("1:1", "batch-flac", "flac", "flac.flac", 1, peer_dest("1:1", "flac"))
    st.db.execute("UPDATE queued_transfers SET enqueued_at = ?", (time.time() - 8 * 86400,))
    st.db.commit()
    client = FakeBatches()
    client.states["batch-flac"] = "Queued, Remotely"
    cfg.upgrade = False
    run_once(cfg, st, client, lossless_fakes(cfg).deps())
    assert st.queued_keys() == set() and client.cancelled == ["t-batch-flac"]
    assert ("flac", "flac.flac") in st.tried_sources("1:1")


def test_a_transfer_that_ended_badly_is_dropped_and_remembered(env):
    cfg, st, _ = env
    mp3_in_master(cfg, st)
    st.add_queued("1:1", "batch-flac", "flac", "flac.flac", 1, peer_dest("1:1", "flac"))
    client = FakeBatches()
    client.states["batch-flac"] = "Completed, Rejected"
    cfg.upgrade = False
    rep = run_once(cfg, st, client, lossless_fakes(cfg).deps())
    assert rep.collected == 0 and st.queued_keys() == set() and row_of(st, "1:1")["format"] == "mp3"


def test_a_track_whose_only_lossless_copy_is_queued_waits_and_is_not_researched(env):
    cfg, st, _ = env
    cfg.upgrade = False
    f = Fakes(cfg, [track(1)], peers=("flac",))
    f.queued_peers = {"flac"}
    f.ext_by_peer = {"flac": ".flac"}
    client = FakeBatches()
    client.states["batch-flac"] = "Queued, Remotely"
    rep = run_once(cfg, st, client, f.deps())
    assert rep.waiting == 1 and row_of(st, "1:1")["status"] == "pending" and not rep.failed
    f.calls.clear()
    run_once(cfg, st, client, f.deps())
    assert f.calls == []  # skipped while its transfer waits


def test_not_found_tracks_are_searched_again_after_the_interval_only(env):
    cfg, st, _ = env
    cfg.upgrade, cfg.upgrade_interval_h = True, 24
    f = Fakes(cfg, [track(1)], found=False)
    run_once(cfg, st, FakeBatches(), f.deps())
    assert st.summary() == {"not_found": 1}
    f.found = True
    run_once(cfg, st, FakeBatches(), f.deps())
    assert st.summary() == {"not_found": 1}  # searched an instant ago: left alone
    st.db.execute("UPDATE tracks SET upgrade_checked_at = ?", (time.time() - 25 * 3600,))
    st.db.commit()
    assert run_once(cfg, st, FakeBatches(), f.deps()).done == 1


def test_replacement_finds_the_old_file_when_the_stored_path_comes_from_another_mount(env):
    cfg, st, _ = env
    mp3_in_master(cfg, st)
    real = Path(row_of(st, "1:1")["path"])
    st.db.execute("UPDATE tracks SET path = ? WHERE wishlist_key = '1:1'", ("/opt/host/data/master/" + real.name,))
    st.db.commit()
    cfg.upgrade, cfg.upgrade_interval_h = True, 0
    assert run_once(cfg, st, FakeBatches(), lossless_fakes(cfg).deps()).upgraded == 1
    assert not real.exists() and (cfg.replaced_dir / real.name).exists()


def test_a_stored_path_outside_master_is_never_touched(env, tmp_path):
    cfg, st, _ = env
    mp3_in_master(cfg, st)
    outside = tmp_path / "elsewhere.mp3"
    outside.write_bytes(b"keep me")
    st.db.execute("UPDATE tracks SET path = ? WHERE wishlist_key = '1:1'", (str(outside),))
    st.db.commit()
    cfg.upgrade, cfg.upgrade_interval_h = True, 0
    run_once(cfg, st, FakeBatches(), lossless_fakes(cfg).deps())
    assert outside.read_bytes() == b"keep me"


def test_a_queued_lossless_is_cancelled_when_another_peer_delivers_lossless(env):
    cfg, st, _ = env
    cfg.upgrade = False
    f = Fakes(cfg, [track(1)], peers=("slow", "fast"))
    f.queued_peers = {"slow"}
    client = FakeBatches()
    client.states["batch-slow"] = "Queued, Remotely"
    rep = run_once(cfg, st, client, f.deps())
    r = row_of(st, "1:1")
    assert rep.done == 1 and r["format"] == "aiff"
    assert st.queued_keys() == set() and client.cancelled == ["t-batch-slow"]  # nothing left waiting at the slow peer


def test_a_leftover_queued_transfer_of_a_lossless_track_is_dropped_on_collect(env):
    cfg, st, _ = env
    cfg.upgrade = False
    f = Fakes(cfg, [track(1)])
    client = FakeBatches()
    assert run_once(cfg, st, client, f.deps()).done == 1
    st.add_queued("1:1", "batch-old", "slow", "x.flac", 1, "1_1__slow")
    client.states["batch-old"] = "Queued, Remotely"
    run_once(cfg, st, client, f.deps())
    assert st.queued_keys() == set() and client.cancelled == ["t-batch-old"]


def test_a_path_that_pointed_into_review_never_resolves_to_a_master_file_of_the_same_name(env):
    from bandarr.sync import _old_file
    cfg, st, _ = env
    mp3_in_master(cfg, st)
    real = Path(row_of(st, "1:1")["path"])
    for status, stored in (("done", "/opt/host/data/review/fail/" + real.name),
                           ("in_review", "/opt/host/data/master/" + real.name)):
        st.db.execute("UPDATE tracks SET status = ?, path = ? WHERE wishlist_key = '1:1'", (status, stored))
        assert _old_file(row_of(st, "1:1"), cfg) is None
    st.db.execute("UPDATE tracks SET status = 'done', path = ? WHERE wishlist_key = '1:1'",
                  ("/opt/host/data/master/" + real.name,))
    assert _old_file(row_of(st, "1:1"), cfg) == real  # the legitimate host-path case still works
