"""Wishlist membership: albums that left the wishlist (bought, removed, another account)."""
import sqlite3

from test_sync import Fakes, env, row_of, track  # noqa: F401  (env is a fixture)
from test_upgrade import FakeBatches, lossless_fakes, mp3_in_master

from bandarr.bandcamp import WishlistItems
from bandarr.state import State
from bandarr.sync import Report, run_once


def wishlist(*item_ids, complete=True):
    items = WishlistItems({"item_id": i} for i in item_ids)
    items.complete = complete
    return items


def two_albums(cfg):
    f = Fakes(cfg, [track(1, item="1"), track(2, item="2")])
    f.items = wishlist(1, 2)
    return f


def test_pending_tracks_of_an_album_that_left_the_wishlist_are_skipped_not_forgotten(env):
    cfg, st, _ = env
    f = two_albums(cfg)
    run_once(cfg, st, FakeBatches(), f.deps(), dry_run=True)  # both albums known and on the wishlist
    f.items = wishlist(2)  # album 1 was bought, removed, or this is another account
    rep = run_once(cfg, st, FakeBatches(), f.deps())
    assert rep.done == 1 and rep.off_wishlist == 1
    assert row_of(st, "1:1")["status"] == "pending" and row_of(st, "2:2")["status"] == "done"
    assert [c[1] for c in f.calls if c[0] == "find"] == ["T2"]  # album 1 was never searched
    assert st.is_on_wishlist("1") is False and st.is_on_wishlist("2") is True

    f.items = wishlist(1, 2)  # it comes back: nothing was lost, it is processed again
    f.calls.clear()
    assert run_once(cfg, st, FakeBatches(), f.deps()).done == 1
    assert row_of(st, "1:1")["status"] == "done"


def test_changing_account_leaves_the_old_accounts_pending_tracks_alone(env):
    cfg, st, _ = env
    cfg.limit = 1
    f = Fakes(cfg, [track(1, item="1"), track(2, item="1")])
    f.items = wishlist(1)
    assert run_once(cfg, st, FakeBatches(), f.deps()).done == 1  # old account: one track done, one pending
    done_file = row_of(st, "1:1")["path"]

    cfg.limit = None
    g = Fakes(cfg, [track(3, item="2")])  # new account: a different album
    g.items = wishlist(2)
    rep = run_once(cfg, st, FakeBatches(), g.deps())
    assert rep.done == 1 and rep.off_wishlist == 1
    assert row_of(st, "1:2")["status"] == "pending" and row_of(st, "2:3")["status"] == "done"
    assert row_of(st, "1:1")["status"] == "done" and row_of(st, "1:1")["path"] == done_file  # master untouched
    assert sorted(g.names()).count("download") == 1  # the old account's pending track was not downloaded


def test_an_incomplete_read_never_marks_albums_as_gone(env):
    cfg, st, _ = env
    f = two_albums(cfg)
    run_once(cfg, st, FakeBatches(), f.deps(), dry_run=True)
    f.items = wishlist(2, complete=False)  # pagination cut short
    rep = run_once(cfg, st, FakeBatches(), f.deps())
    assert rep.off_wishlist == 0 and rep.done == 2  # album 1 still processed


def test_switching_to_an_account_with_an_empty_wishlist_skips_everything_of_the_old_one(env, caplog):
    cfg, st, _ = env
    f = two_albums(cfg)
    run_once(cfg, st, FakeBatches(), f.deps(), dry_run=True)  # old account: two albums pending
    f.items = wishlist()  # new account: the page says item_count 0
    with caplog.at_level("WARNING"):
        rep = run_once(cfg, st, FakeBatches(), f.deps())
    assert rep.done == 0 and rep.off_wishlist == 2 and "find" not in f.names()
    assert st.summary() == {"pending": 2}  # kept, not deleted
    assert any("EMPTY" in r.message for r in caplog.records)  # and the log says why nothing happens
    f.items = wishlist(1, 2)  # back on the old account: everything resumes
    assert run_once(cfg, st, FakeBatches(), f.deps()).done == 2


def test_off_wishlist_mp3s_are_not_searched_for_an_upgrade(env):
    cfg, st, _ = env
    mp3_in_master(cfg, st)
    cfg.upgrade, cfg.upgrade_interval_h = True, 0
    f = lossless_fakes(cfg)
    f.items = wishlist(999)  # the track's album (1) is no longer on the wishlist
    rep = run_once(cfg, st, FakeBatches(), f.deps())
    assert rep.upgraded == 0 and "find" not in f.names()
    assert row_of(st, "1:1")["format"] == "mp3"  # the file itself is left alone


def test_a_queued_transfer_of_an_album_that_left_the_wishlist_is_cancelled(env):
    cfg, st, _ = env
    mp3_in_master(cfg, st)
    st.add_queued("1:1", "batch-flac", "flac", "flac.flac", 1, "d")
    client = FakeBatches()
    client.states["batch-flac"] = "Queued, Remotely"
    cfg.upgrade = False
    f = lossless_fakes(cfg)
    f.items = wishlist(999)
    run_once(cfg, st, client, f.deps())
    assert st.queued_keys() == set() and client.cancelled == ["t-batch-flac"]


def test_state_membership_counts_and_unknown_albums(tmp_path):
    st = State(tmp_path / "s.db")
    st.add_tracks([track(1, item="1"), track(2, item="2"), track(3, item="3")])
    assert st.sync_wishlist({"1", "2"}) == (1, 0)  # album 3 left
    assert st.sync_wishlist({"1", "2"}) == (0, 0)  # nothing new
    assert st.sync_wishlist({"1", "2", "3"}) == (0, 1)  # album 3 is back
    assert st.sync_wishlist({"1"}) == (2, 0)
    assert st.off_wishlist_pending() == 2 and [r["wishlist_key"] for r in st.to_fetch()] == ["1:1"]
    assert st.deferred_count() == 0 and st.is_on_wishlist("never-seen") is True


def test_the_report_says_how_many_tracks_were_skipped():
    assert "skipped, off the wishlist 4" in str(Report(off_wishlist=4))


def test_state_migrates_the_items_table_of_an_older_database(tmp_path):
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
    assert {"on_wishlist", "last_seen_at"} <= {r[1] for r in st.db.execute("PRAGMA table_info(items)")}
    assert st.is_on_wishlist("1") is True and [r["wishlist_key"] for r in st.to_fetch()] == ["1:1"]


# --- the Bandcamp reader: what counts as an empty wishlist --------------------------------------

import html as _html
import json as _json

import pytest

from bandarr import bandcamp


def page(blob):
    return f'<html><body><div id="pagedata" data-blob="{_html.escape(_json.dumps(blob), quote=True)}"></div></body></html>'


def read(monkeypatch, blob, posts=()):
    class Sess:
        def post(self, *a, **k):
            return type("R", (), {"status_code": 200, "json": lambda self_: posts[0]})()

    monkeypatch.setattr(bandcamp, "_session", lambda identity: Sess())
    monkeypatch.setattr(bandcamp, "_get", lambda s, url, retries=3: page(blob))
    return bandcamp.fetch_wishlist_items("someone")


def test_a_wishlist_that_says_item_count_zero_is_an_empty_and_complete_read(monkeypatch):
    items = read(monkeypatch, {"fan_data": {"fan_id": 7}, "wishlist_data": {"item_count": 0, "hidden": False,
                                                                           "sequence": [], "last_token": None}})
    assert list(items) == [] and items.complete is True


def test_a_page_without_wishlist_data_is_an_error_not_an_empty_wishlist(monkeypatch):
    with pytest.raises(bandcamp.BandcampError, match="wishlist_data missing"):
        read(monkeypatch, {"fan_data": {"fan_id": 7}})


def test_a_hidden_wishlist_without_identity_is_an_error(monkeypatch):
    with pytest.raises(bandcamp.BandcampError, match="private"):
        read(monkeypatch, {"fan_data": {"fan_id": 7}, "wishlist_data": {"item_count": 0, "hidden": True}})


def test_a_pagination_that_stops_early_is_flagged_incomplete(monkeypatch):
    blob = {"fan_data": {"fan_id": 7},
            "wishlist_data": {"item_count": 3, "hidden": False, "sequence": ["a1"], "last_token": "t"},
            "item_cache": {"wishlist": {"a1": {"item_id": 1}}}}
    items = read(monkeypatch, blob, posts=[{"items": [], "more_available": False, "last_token": None}])
    assert len(items) == 1 and items.complete is False


# --- the same song as an album track and as a single track ------------------------------------

def _row(key, artist, title, alt_artist=None, alt_title=None, album_artist="Label", item="1"):
    return dict(wishlist_key=key, item_id=item, artist=artist, title=title, album="A", album_artist=album_artist,
                track_num=1, duration=None, alt_artist=alt_artist, alt_title=alt_title, url="u", art_id=None)


def _add(st, **r):
    from bandarr.bandcamp import Track
    st.add_tracks([Track(r["wishlist_key"], r["artist"], r["title"], r["album"], r["album_artist"], 1, None, "album",
                         "u", r["alt_artist"], r["alt_title"], None)])


def test_a_pending_single_track_already_done_as_part_of_an_album_is_a_duplicate(tmp_path):
    from bandarr.state import State
    st = State(tmp_path / "s.db")
    _add(st, **_row("10:1", "Label", "Pink Concrete - My, My Mind", "Pink Concrete", "My, My Mind", item="10"))
    st.update("10:1", status="done", source="soulseek", format="aiff", path="/m/a.aiff")
    _add(st, **_row("20:20", "Label", "Pink Concrete - My, My Mind", "Pink Concrete", "My, My Mind", item="20"))
    _add(st, **_row("30:30", "Label", "Other - Song", "Other", "Song", item="30"))
    assert st.mark_duplicates() == 1
    r = st.get_track("20:20")
    assert (r["status"], r["duplicate_of"]) == ("duplicate", "10:1")
    assert [x["wishlist_key"] for x in st.to_fetch()] == ["30:30"]


def test_a_bandcamp_purchase_is_never_turned_into_a_duplicate(tmp_path):
    from bandarr.state import State
    st = State(tmp_path / "s.db")
    _add(st, **_row("10:1", "A", "T", item="10"))
    st.update("10:1", status="done", source="soulseek", format="aiff", path="/m/a.aiff")
    _add(st, **_row("20:20", "A", "T", item="20"))
    st.update("20:20", status="done", source="bandcamp", format="aiff", path="/m/b.aiff")
    st.mark_duplicate("20:20", "10:1")
    assert st.get_track("20:20")["status"] == "done"


def test_a_single_track_by_a_label_is_split_but_a_solo_title_is_not():
    from bandarr.tagger import display_names, is_compilation
    label = _row("1:1", "Label", "Pink Concrete - My, My Mind", "Pink Concrete", "My, My Mind", item="1")
    assert is_compilation([label]) and display_names(label, True) == ("Pink Concrete", "My, My Mind")
    solo = _row("2:2", "ANNE", "ANNE - Daydream", "ANNE", "Daydream", album_artist="ANNE", item="2")
    assert not is_compilation([solo]) and display_names(solo, False) == ("ANNE", "Daydream")


def test_two_pending_twins_in_one_run_are_not_processed_together(env):
    from test_sync import Fakes, track
    from test_upgrade import FakeBatches
    from bandarr.sync import run_once
    cfg, st, _ = env
    cfg.download_workers = 3
    a, b = track(1), track(2)
    b.title, b.artist, b.wishlist_key = a.title, a.artist, "9:9"
    f = Fakes(cfg, [a, b])
    assert run_once(cfg, st, FakeBatches(), f.deps()).done == 1
    run_once(cfg, st, FakeBatches(), f.deps())
    assert st.summary() == {"done": 1, "duplicate": 1} and len(list(cfg.master_dir.glob("*"))) == 1
