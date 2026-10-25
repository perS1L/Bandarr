import time
from types import SimpleNamespace

from bandarr.bandcamp import Track
from bandarr.queue_status import format_rows, rows
from bandarr.slskd import Slskd, SlskdError
from bandarr.state import State


def setup(tmp_path):
    st = State(tmp_path / "s.db")
    st.add_tracks([Track("1:1", "Art", "Title", "Al", "Art", 1, 100.0, "album", "u")])
    st.add_queued("1:1", "b1", "peer", "f.flac", 1, "d")
    return st


class Client:
    def __init__(self, state="Queued, Remotely", place=None, answers=None, broken=False):
        self.state, self.place, self.answers, self.broken, self.asked = state, place, answers, broken, 0

    def batch(self, bid):
        if self.broken:
            raise SlskdError("HTTP 404")
        return {"transfers": [{"id": "t1", "state": self.state, "placeInQueue": self.place}]}

    def queue_position(self, username, tid):
        self.asked += 1
        return self.answers


def test_reports_the_place_when_the_peer_answers(tmp_path):
    st = setup(tmp_path)
    [e] = rows(st, Client(answers=12), now=time.time() + 7200)
    assert (e["track"], e["peer"], e["place"], e["age"]) == ("Art - Title", "peer", 12, "2.0 h")
    assert "12" in format_rows([e])


def test_a_silent_peer_gives_no_place_but_no_error(tmp_path):
    [e] = rows(setup(tmp_path), Client(answers=None))
    assert e["place"] is None and "did not report" in e["note"]
    assert "(peer did not report its queue)" in format_rows([e])


def test_place_already_known_is_not_asked_again_and_no_position_skips_the_request(tmp_path):
    st = setup(tmp_path)
    c = Client(place=3)
    assert rows(st, c)[0]["place"] == 3 and c.asked == 0
    c2 = Client(answers=9)
    assert rows(st, c2, with_position=False)[0]["place"] is None and c2.asked == 0


def test_slskd_errors_are_reported_not_raised(tmp_path):
    [e] = rows(setup(tmp_path), Client(broken=True))
    assert e["state"] == "?" and e["note"].startswith("slskd:")


def test_nothing_waiting(tmp_path):
    assert format_rows(rows(State(tmp_path / "x.db"), Client())) == "no transfer is waiting in a queue"


def test_queue_position_parses_both_answer_shapes_and_survives_peer_silence(monkeypatch):
    c = Slskd("http://x", "k")
    monkeypatch.setattr(c, "_req", lambda *a, **k: {"placeInQueue": 4})
    assert c.queue_position("p", "t") == 4
    monkeypatch.setattr(c, "_req", lambda *a, **k: 6)
    assert c.queue_position("p", "t") == 6

    def silent(*a, **k):
        raise SlskdError("HTTP 500: The wait timed out after 5000 milliseconds")
    monkeypatch.setattr(c, "_req", silent)
    assert c.queue_position("p", "t") is None
