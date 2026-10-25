from bandarr.finder import find_best
from bandarr.scoring import Wanted

W = Wanted(artist="PAN", title="Kareem Lotfy - Fr3sh", duration=232,
           alt_artist="Kareem Lotfy", alt_title="Fr3sh")


def r(user, name, **kw):
    return {"username": user, "hasFreeUploadSlot": True, "queueLength": 0, "uploadSpeed": 1_000_000,
            "files": [{"filename": name, "size": 1, "length": 231, "isLocked": False, **kw}]}


class Fake:
    def __init__(self, by_query):
        self.by_query, self.asked = by_query, []

    def search(self, q):
        self.asked.append(q)
        return self.by_query.get(q, [])


MP3 = r("m", "PAN\\01 Kareem Lotfy - Fr3sh.mp3", bitRate=320)
FLAC = r("f", "PAN\\01 Kareem Lotfy - Fr3sh.flac", bitDepth=16, sampleRate=44100)


def test_mp3_only_first_query_does_not_stop_the_search_for_lossless():
    c = Fake({"pan kareem lotfy fr3sh": [MP3], "kareem lotfy fr3sh": [FLAC]})
    res = find_best(c, W, gap=0)
    assert c.asked == ["pan kareem lotfy fr3sh", "kareem lotfy fr3sh"]
    assert res[0].candidate.ext == "flac" and {x.candidate.ext for x in res} == {"flac", "mp3"}


def test_stops_as_soon_as_lossless_found():
    c = Fake({"pan kareem lotfy fr3sh": [FLAC]})
    find_best(c, W, gap=0)
    assert c.asked == ["pan kareem lotfy fr3sh"]


def test_falls_back_to_mp3_when_no_lossless_anywhere():
    c = Fake({"kareem lotfy fr3sh": [MP3]})
    res = find_best(c, W, gap=0)
    assert [x.candidate.ext for x in res] == ["mp3"] and len(c.asked) == 2


def test_nothing_found():
    assert find_best(Fake({}), W, gap=0) == []


def test_peer_controlled_names_cannot_reshape_the_api_path():
    from bandarr.slskd import Slskd
    seen = []
    c = Slskd("http://x", "k")
    c._req = lambda method, path, **kw: seen.append((method, path)) or {}
    c.cancel("../../application/x", "id/1")
    c.queue_position("a/b", "c d")
    assert seen == [("DELETE", "/transfers/downloads/..%2F..%2Fapplication%2Fx/id%2F1"),
                    ("GET", "/transfers/downloads/a%2Fb/c%20d/position")]
