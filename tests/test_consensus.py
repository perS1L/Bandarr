"""A refused copy is accepted when the rest of ITS folder (same peer, same directory) passed the gate."""
from pathlib import Path

from bandarr.sync import remote_dir, run_once

from test_sync import Fakes, env, row_of, track  # noqa: F401  (env is a fixture)


def album(n=5):
    return [track(i) for i in range(1, n + 1)]


def checks(st):
    return st.db.execute("SELECT * FROM gate_checks ORDER BY id").fetchall()


def test_remote_dir_is_the_folder_in_lower_case():
    assert remote_dir("@@Music\\Techno\\Album X\\01 a.flac") == "@@music\\techno\\album x"
    assert remote_dir("a/b/c.flac") == "a\\b"
    assert remote_dir("c.flac") == ""


def test_one_refused_track_among_passing_ones_of_the_same_folder_is_accepted(env):
    cfg, st, _ = env
    f = Fakes(cfg, album())
    f.bad_keys = {"1:5"}
    for t in album():
        f.folder_by_title[t.title] = "Techno\\Album"
    rep = run_once(cfg, st, None, f.deps())
    r = row_of(st, "1:5")
    assert (r["status"], r["format"], r["source"]) == ("done", "aiff", "soulseek")
    assert rep.consensus == 1 and rep.done == 4 and rep.in_review == 1
    assert Path(r["path"]).parent == cfg.master_dir
    assert list((cfg.review_dir / "accepted").glob("*.flac"))  # the evidence is kept, moved to accepted/
    last = checks(st)[-1]
    assert last["wishlist_key"] == "1:5" and last["consensus"] == "4/5"
    assert last["decision"] == "fail"  # the gate's own verdict is not rewritten


def test_a_track_from_another_folder_of_the_same_peer_gets_no_help(env):
    cfg, st, _ = env
    f = Fakes(cfg, album())
    f.bad_keys = {"1:5"}
    for t in album():
        f.folder_by_title[t.title] = "Techno\\Album"
    f.folder_by_title["T5"] = "Techno\\Other album"  # same peer, another release
    rep = run_once(cfg, st, None, f.deps())
    assert row_of(st, "1:5")["status"] == "in_review" and rep.consensus == 0


def test_too_few_passing_tracks_do_not_rescue(env):
    cfg, st, _ = env
    f = Fakes(cfg, album(3))
    f.bad_keys = {"1:3"}  # only 2 siblings pass, the minimum is 3
    run_once(cfg, st, None, f.deps())
    assert row_of(st, "1:3")["status"] == "in_review"


def test_a_folder_that_mostly_fails_is_not_trusted(env):
    cfg, st, _ = env
    f = Fakes(cfg, album(7))
    f.bad_keys = {"1:4", "1:5", "1:6", "1:7"}  # 3 pass, 4 refused: no majority
    rep = run_once(cfg, st, None, f.deps())
    assert rep.consensus == 0 and rep.in_review == 4


def test_accepted_tracks_do_not_vouch_for_each_other(env):
    cfg, st, _ = env
    f = Fakes(cfg, album(6))
    f.bad_keys = {"1:5", "1:6"}  # 4 pass, 2 refused: both rescued by the 4 real passes
    rep = run_once(cfg, st, None, f.deps())
    assert rep.consensus == 2
    # the witnesses are only the gate's own passes
    assert st.folder_votes("p1", "", "1:5") == (4, 5)


def test_the_consensus_can_be_switched_off(env):
    cfg, st, _ = env
    cfg.consensus_min = 0
    f = Fakes(cfg, album())
    f.bad_keys = {"1:5"}
    assert run_once(cfg, st, None, f.deps()).consensus == 0
    assert row_of(st, "1:5")["status"] == "in_review"


def test_the_rescue_replaces_the_mp3_fallback(env):
    cfg, st, _ = env
    f = Fakes(cfg, album(), peers=("p1", "mp"))
    f.ext_by_peer, f.tiers = {"mp": ".mp3"}, {"mp": 2}
    f.bad_keys = {"1:5"}
    run_once(cfg, st, None, f.deps())
    r = row_of(st, "1:5")
    assert r["format"] == "aiff" and Path(r["path"]).suffix == ".aiff"
    assert [p.name for p in cfg.replaced_dir.glob("*.mp3")] == ["Art - T5.mp3"]


def test_a_bandcamp_purchase_is_never_replaced(env):
    cfg, st, _ = env
    f = Fakes(cfg, album())
    f.bad_keys = {"1:5"}
    run_once(cfg, st, None, f.deps())
    st.update("1:5", status="in_review", source="bandcamp")
    with st.db:
        st.db.execute("UPDATE gate_checks SET consensus = NULL WHERE wishlist_key = '1:5'")
    f2 = Fakes(cfg, album())
    from bandarr.sync import Report, consensus_rescue
    rep = Report()
    consensus_rescue(st, cfg, None, f2.deps(), rep)
    assert rep.consensus == 0
