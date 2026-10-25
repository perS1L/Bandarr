"""Parallel downloads: worker pool, per-worker state/client, search serialisation, file races."""
import threading
import time
from pathlib import Path

import pytest
from test_sync import BAD, Fakes, env, row_of, track  # noqa: F401  (env is a fixture)
from test_upgrade import FakeBatches

from bandarr import covers, gate, tagger
from bandarr.config import Config
from bandarr.gate import GateResult
from bandarr.slskd import SearchGate, Slskd
from bandarr.state import State
from bandarr.sync import _run_tasks, run_once


def run_threads(n, fn):
    """Start n threads on fn(i) together; re-raise the first exception."""
    start, errors = threading.Barrier(n), []

    def go(i):
        try:
            start.wait(timeout=10)
            fn(i)
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    ts = [threading.Thread(target=go, args=(i,)) for i in range(n)]
    [t.start() for t in ts]
    [t.join(timeout=30) for t in ts]
    if errors:
        raise errors[0]


# --- searches stay one at a time, with a pause -------------------------------------------------

def test_search_gate_allows_one_search_at_a_time_and_spaces_them(monkeypatch):
    log, lock, inflight = [], threading.Lock(), [0]

    def fake_req(self, method, path, **kw):
        with lock:
            if method == "POST":
                inflight[0] += 1
                assert inflight[0] == 1, "two searches ran at the same time"
                log.append(time.monotonic())
            elif method == "DELETE":
                inflight[0] -= 1
        time.sleep(0.01)
        return {"isComplete": True} if method == "GET" and not path.endswith("/responses") else ([] if method == "GET" else None)

    monkeypatch.setattr(Slskd, "_req", fake_req)
    base = Slskd("http://x", "k", search_gate=SearchGate(0.05))
    run_threads(5, lambda i: base.clone().search("q", poll=0))
    assert len(log) == 5
    assert all(b - a >= 0.05 for a, b in zip(log, log[1:]))  # a pause between two searches


def test_clone_has_its_own_session_but_shares_the_gate():
    a = Slskd("http://x", "key", search_gate=SearchGate(1))
    b = a.clone()
    assert b.s is not a.s and b.search_gate is a.search_gate and b.s.headers["X-API-Key"] == "key"


def test_no_gate_means_no_waiting(monkeypatch):
    monkeypatch.setattr(Slskd, "_req", lambda self, m, p, **k: {"isComplete": True} if m == "GET" and "responses" not in p else [])
    t = time.monotonic()
    Slskd("http://x", "k").search("q", poll=0)
    assert time.monotonic() - t < 0.5


# --- one sqlite connection per worker ----------------------------------------------------------

def test_cloned_state_connections_can_write_concurrently_without_locking_errors(tmp_path):
    st = State(tmp_path / "s.db")
    st.add_tracks([track(i) for i in range(1, 9)])

    def work(i):
        s = st.clone()
        for _ in range(25):
            s.update(f"1:{i + 1}", status="downloaded", source="soulseek", path=f"/x/{i}")
            s.log_source(f"1:{i + 1}", f"peer{i}", "f", "failed")
            s.schedule_retry(f"1:{i + 1}", 1, 24)
        s.close()

    run_threads(8, work)
    assert st.summary() == {"downloaded": 8}
    assert [r["retry_count"] for r in st.db.execute("SELECT retry_count FROM tracks ORDER BY wishlist_key")] == [25] * 8


# --- the worker pool ---------------------------------------------------------------------------

def test_run_tasks_runs_up_to_n_at_once_never_shares_a_worker_resource_and_keeps_order(tmp_path):
    st = State(tmp_path / "s.db")

    class Client:
        def clone(self):
            return Client()

    lock, busy, max_busy, seen = threading.Lock(), set(), [0], []

    def fn(s, c, task):
        key = (id(s), id(c))
        with lock:
            assert key not in busy, "a state/client pair was used by two threads at once"
            busy.add(key)
            max_busy[0] = max(max_busy[0], len(busy))
            seen.append(s)
        time.sleep(0.05)
        with lock:
            busy.discard(key)
        return task * 2

    assert _run_tasks(st, Client(), range(9), 3, fn) == [t * 2 for t in range(9)]
    assert max_busy[0] == 3
    assert all(s is not st for s in seen)  # workers got their own connections


def test_run_tasks_with_one_worker_or_one_task_uses_the_callers_objects(tmp_path):
    st, client = State(tmp_path / "s.db"), object()
    got = []
    _run_tasks(st, client, [1, 2], 1, lambda s, c, t: got.append((s, c)))
    _run_tasks(st, client, [3], 5, lambda s, c, t: got.append((s, c)))
    assert all(s is st and c is client for s, c in got) and len(got) == 3
    assert _run_tasks(st, client, [], 3, lambda s, c, t: 1) == []


def test_run_tasks_closes_the_worker_connections(tmp_path):
    st = State(tmp_path / "s.db")
    used = []
    _run_tasks(st, None, range(4), 2, lambda s, c, t: used.append(s))
    import sqlite3
    for s in set(used):
        with pytest.raises(sqlite3.ProgrammingError):
            s.db.execute("SELECT 1")


# --- whole runs ------------------------------------------------------------------------------

class ParallelFakes(Fakes):
    """Fakes whose downloads can be forced to overlap with a barrier."""

    def __init__(self, *a, barrier=None, **kw):
        super().__init__(*a, **kw)
        self.barrier, self.inflight, self.max_inflight, self.lock = barrier, 0, 0, threading.Lock()

    def download(self, *a, **kw):
        with self.lock:
            self.inflight += 1
            self.max_inflight = max(self.max_inflight, self.inflight)
        try:
            if self.barrier:
                self.barrier.wait(timeout=10)  # fails the test if downloads are not simultaneous
            return super().download(*a, **kw)
        finally:
            with self.lock:
                self.inflight -= 1


def test_three_workers_really_download_three_tracks_at_once(env):
    cfg, st, _ = env
    cfg.download_workers = 3
    f = ParallelFakes(cfg, [track(i) for i in range(1, 7)], barrier=threading.Barrier(3))
    rep = run_once(cfg, st, FakeBatches(), f.deps())
    assert rep.done == 6 and f.max_inflight == 3
    assert st.summary() == {"done": 6}
    assert len({row["path"] for row in st.db.execute("SELECT path FROM tracks")}) == 6
    assert len(list(cfg.master_dir.glob("*"))) == 6
    assert st.db.execute("SELECT COUNT(*) FROM gate_checks").fetchone()[0] == 6  # every worker's writes landed


def test_the_parameter_caps_the_parallelism(env):
    cfg, st, _ = env
    cfg.download_workers = 2
    f = ParallelFakes(cfg, [track(i) for i in range(1, 7)], barrier=threading.Barrier(2))
    assert run_once(cfg, st, FakeBatches(), f.deps()).done == 6 and f.max_inflight == 2


def test_one_worker_is_the_old_sequential_behaviour(env):
    cfg, st, _ = env
    f = ParallelFakes(cfg, [track(i) for i in range(1, 5)])
    assert run_once(cfg, st, FakeBatches(), f.deps()).done == 4 and f.max_inflight == 1


def test_failures_stay_isolated_and_counted_correctly_in_parallel(env):
    cfg, st, _ = env
    cfg.download_workers = 3
    f = ParallelFakes(cfg, [track(i) for i in range(1, 7)])
    f.fail_download.add("1:1")
    f.explode.add("1:2")
    f.gate_by_peer = {}
    f.gate_result = GateResult("pass", "ok", rule="both", lv_verdict="PASS", fc_verdict="GENUINE")
    rep = run_once(cfg, st, FakeBatches(), f.deps())
    assert rep.done == 4 and sorted(rep.failed) == ["1:1", "1:2"]
    assert row_of(st, "1:1")["retry_count"] == 1 and row_of(st, "1:2")["retry_count"] == 1
    assert row_of(st, "1:3")["retry_count"] == 0


def test_refusals_and_not_found_in_parallel_are_all_accounted_for(env):
    cfg, st, _ = env
    cfg.download_workers = 3
    f = ParallelFakes(cfg, [track(i) for i in range(1, 5)], gate_result=BAD)
    rep = run_once(cfg, st, FakeBatches(), f.deps())
    assert rep.in_review == 4 and st.summary() == {"in_review": 4}
    assert len(list((cfg.review_dir / "fail").glob("*.flac"))) == 4  # none overwritten
    cfg2 = Config(**{**cfg.__dict__, "state_db": cfg.state_db.with_name("n.db")})
    st2 = State(cfg2.state_db)
    g = ParallelFakes(cfg2, [track(i) for i in range(1, 5)], found=False)
    assert run_once(cfg2, st2, FakeBatches(), g.deps()).not_found == 4


def test_parallel_upgrades_each_replace_their_own_mp3(env):
    cfg, st, _ = env
    cfg.upgrade = False
    f = Fakes(cfg, [track(i) for i in range(1, 5)], ext=".mp3")
    assert run_once(cfg, st, FakeBatches(), f.deps()).done == 4
    olds = sorted(Path(r["path"]).name for r in st.db.execute("SELECT path FROM tracks"))

    cfg.upgrade, cfg.upgrade_interval_h, cfg.download_workers = True, 0, 3
    g = ParallelFakes(cfg, [track(i) for i in range(1, 5)], peers=("flac", "mp3"))
    g.tiers, g.ext_by_peer = {"mp3": 2}, {"flac": ".flac", "mp3": ".mp3"}
    assert run_once(cfg, st, FakeBatches(), g.deps()).upgraded == 4
    assert sorted(p.name for p in cfg.replaced_dir.iterdir()) == olds
    assert st.summary() == {"done": 4} and {r["format"] for r in st.db.execute("SELECT format FROM tracks")} == {"aiff"}


# --- file races, with the real functions ------------------------------------------------------

def test_place_never_overwrites_when_many_workers_pick_the_same_name(tmp_path):
    master = tmp_path / "master"
    src = []
    for i in range(12):
        p = tmp_path / f"in{i}.flac"
        p.write_bytes(f"track {i}".encode())
        src.append(p)
    run_threads(12, lambda i: tagger.place(src[i], master, "Same Artist", "Same Title"))
    out = sorted(master.iterdir())
    assert len(out) == 12 and {p.read_bytes() for p in out} == {f"track {i}".encode() for i in range(12)}


def test_quarantine_never_overwrites_when_many_workers_refuse_the_same_name(tmp_path):
    res = GateResult("fail", "x")
    review = tmp_path / "review"
    files = []
    for i in range(12):
        d = tmp_path / f"d{i}"
        d.mkdir()
        (d / "same.flac").write_bytes(f"copy {i}".encode())
        files.append(d / "same.flac")
    run_threads(12, lambda i: gate.quarantine(files[i], res, review))
    kept = [p for p in (review / "fail").iterdir() if p.suffix == ".flac"]
    assert len(kept) == 12 and {p.read_bytes() for p in kept} == {f"copy {i}".encode() for i in range(12)}


def test_one_cover_download_for_an_album_even_with_many_workers(tmp_path, monkeypatch):
    calls = []

    def slow_get(url, **kw):
        calls.append(url)
        time.sleep(0.1)
        return type("R", (), {"status_code": 200, "content": b"\xff\xd8" + b"x" * 5000})()

    monkeypatch.setattr(covers.requests, "get", slow_get)
    got = []
    run_threads(8, lambda i: got.append(covers.get_cover("999", tmp_path)))
    assert len(calls) == 1 and all(g == b"\xff\xd8" + b"x" * 5000 for g in got)
    assert not list(tmp_path.glob("*.part"))


# --- configuration ----------------------------------------------------------------------------

def test_worker_and_gap_settings_come_from_the_environment():
    base = {"BANDCAMP_USER": "u", "SLSKD_API_KEY": "k"}
    assert Config.from_env(base).download_workers == 3 and Config.from_env(base).search_gap_s == 3
    assert Config.from_env({**base, "DOWNLOAD_WORKERS": "5", "SEARCH_GAP_S": "1.5"}).download_workers == 5
    assert Config.from_env({**base, "DOWNLOAD_WORKERS": "0"}).download_workers == 1  # never below one
    assert Config.from_env({**base, "SEARCH_GAP_S": "1.5"}).search_gap_s == 1.5
