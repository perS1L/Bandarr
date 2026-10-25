from bandarr.scoring import Wanted, queries, rank

W = Wanted(artist="Connor Wall", title="Oak", duration=312)


def resp(user, *files, free=True, queue=0, speed=1_000_000):
    return {"username": user, "hasFreeUploadSlot": free, "queueLength": queue,
            "uploadSpeed": speed, "files": list(files)}


def f(name, length=312, **kw):
    return {"filename": name, "size": 1000, "length": length, "isLocked": False, **kw}


def best(responses, w=W):
    r = rank(w, responses)
    return r[0].candidate if r else None


def test_flac_without_bitrate_is_never_dropped_by_bitrate_filter():
    # Soulseek FLAC carries bitDepth/sampleRate and no bitRate (CLAUDE.md section 8)
    c = best([resp("a", f("m\\Connor Wall - Oak.flac", bitDepth=16, sampleRate=44100))])
    assert c and c.ext == "flac" and c.bitrate is None


def test_lossless_beats_mp3_320_even_with_worse_availability():
    c = best([
        resp("mp3", f("Connor Wall - Oak.mp3", bitRate=320)),
        resp("flac", f("Connor Wall - Oak.flac", bitDepth=16, sampleRate=44100), free=False, queue=50),
    ])
    assert c.username == "flac"


def test_mp3_floor_is_320_lossy_only():
    assert best([resp("a", f("Connor Wall - Oak.mp3", bitRate=256))]) is None
    assert best([resp("a", f("Connor Wall - Oak.mp3", bitRate=192))]) is None
    assert best([resp("a", f("Connor Wall - Oak.mp3", bitRate=320))]).username == "a"


def test_unknown_mp3_bitrate_kept_but_below_verified_320():
    c = best([
        resp("unknown", f("Connor Wall - Oak.mp3")),
        resp("v320", f("Connor Wall - Oak.mp3", bitRate=320), free=False, queue=90),
    ])
    assert c.username == "v320"
    assert best([resp("unknown", f("Connor Wall - Oak.mp3"))]).username == "unknown"


def test_24bit_preferred_over_16bit_when_availability_is_similar():
    # the AIFF hook keeps 24-bit and the XDJ-700 plays it
    c = best([
        resp("16", f("Connor Wall - Oak.flac", bitDepth=16, sampleRate=44100)),
        resp("24", f("Connor Wall - Oak.flac", bitDepth=24, sampleRate=44100), queue=10),
    ])
    assert c.username == "24"


def test_a_free_slot_still_beats_a_long_queue_even_for_24bit():
    c = best([
        resp("16", f("Connor Wall - Oak.flac", bitDepth=16, sampleRate=44100)),
        resp("24", f("Connor Wall - Oak.flac", bitDepth=24, sampleRate=44100), free=False, queue=100, speed=0),
    ])
    assert c.username == "16"


def test_cbr_320_preferred_over_vbr_320():
    c = best([
        resp("vbr", f("Connor Wall - Oak.mp3", bitRate=320, isVariableBitRate=True)),
        resp("cbr", f("Connor Wall - Oak.mp3", bitRate=320, isVariableBitRate=False)),
    ])
    assert c.username == "cbr"


def test_quality_never_beats_a_wrong_version():
    c = best([
        resp("hires_remix", f("Connor Wall - Oak (X Remix).flac", bitDepth=24, sampleRate=44100)),
        resp("orig", f("Connor Wall - Oak.flac", bitDepth=16, sampleRate=44100)),
    ])
    assert c.username == "orig"


def test_non_audio_and_unsupported_formats_ignored():
    files = [f("Connor Wall - Oak.jpg"), f("Connor Wall - Oak.m4a"), f("Connor Wall - Oak.wav")]
    assert best([resp("a", *files)]) is None


def test_locked_files_ignored():
    locked = f("Connor Wall - Oak.flac", bitDepth=16, sampleRate=44100)
    locked["isLocked"] = True
    assert best([resp("a", locked)]) is None


def test_wrong_duration_means_different_version():
    assert best([resp("a", f("Connor Wall - Oak.flac", length=400))]) is None


def test_remix_is_penalised_below_the_original():
    c = best([
        resp("remix", f("Connor Wall - Oak (Foo Remix).flac")),
        resp("orig", f("Connor Wall - Oak.flac"), free=False, queue=80),
    ])
    assert c.username == "orig"


def test_original_mix_suffix_is_not_a_variant():
    r = rank(W, [resp("a", f("01-connor_wall-oak_(original_mix).flac"))])
    assert r and not any(x.startswith("variant") for x in r[0].reasons)


def test_other_track_with_same_artist_rejected():
    assert best([resp("a", f("Connor Wall - Elm.flac"))]) is None


def test_title_without_artist_needs_matching_length():
    w = Wanted(artist="Some Label", title="Oak", duration=312)
    assert best([resp("a", f("01 Oak.flac", length=311))], w).username == "a"
    assert best([resp("a", f("01 Oak.flac", length=None))], w) is None


def test_va_alt_artist_title_matches():
    w = Wanted(artist="PAN", title="Kareem Lotfy - Fr3sh", duration=232,
               alt_artist="Kareem Lotfy", alt_title="Fr3sh")
    c = best([resp("a", f("PAN 77\\01 Kareem Lotfy - Fr3sh.flac", length=231))], w)
    assert c and c.username == "a"


def test_feat_decoration_dropped_by_file_still_matches():
    w = Wanted(artist="Foo", title="Bar (feat. Baz)", duration=200)
    assert best([resp("a", f("Foo - Bar.flac", length=200))], w)


def test_accents_and_punctuation_normalised():
    w = Wanted(artist="Chlär", title="Nacht Powder", duration=None)
    assert best([resp("a", f("Chlar - Nacht Powder.flac", length=None))], w)


def test_queries_specific_first_title_only_last_and_deduped():
    w = Wanted(artist="PAN", title="Kareem Lotfy - Fr3sh", alt_artist="Kareem Lotfy", alt_title="Fr3sh")
    qs = queries(w)
    assert qs[0] == "pan kareem lotfy fr3sh" and qs[-1] == "kareem lotfy fr3sh"
    assert len(qs) == len(set(qs))


# --- the artist may appear only in the folders, the file name holding just the title --------------

def accepted(responses, w=W):
    return rank(w, responses)


def test_artist_only_in_the_folders_and_title_only_in_the_file_name():
    for name in ("Music\\Connor Wall\\Equals (2026)\\01 Oak.flac",
                 "Music\\Connor Wall - Equals\\Oak.flac",
                 "@@user\\complete\\Connor_Wall-Equals-SUBL029-WEB-FLAC\\01-oak.flac"):
        [s] = accepted([resp("a", f(name, bitDepth=16, sampleRate=44100))])
        assert "artist" in s.reasons and "title" in s.reasons, name
    # and the file name being only the title counts as an exact match
    assert "exact" in accepted([resp("a", f("Connor Wall\\Equals\\01 Oak.flac"))])[0].reasons


def test_folder_artist_with_accents_and_punctuation():
    w = Wanted(artist="Chlär", title="Nacht Powder", duration=None)
    [s] = accepted([resp("a", f("Music\\Chlar\\EP\\02 Nacht Powder.flac", length=None))], w)
    assert "artist" in s.reasons


def test_another_artists_folder_is_rejected_unless_the_length_matches():
    other = "Music\\Someone Else\\Album\\01 Oak.flac"
    assert accepted([resp("a", f(other, length=250))]) == []  # different artist, different length
    # same title and the same length (+-3 s) is still taken: a strong signal when the artist is unknown
    assert accepted([resp("a", f(other, length=311))])


def test_the_artist_in_the_folder_ranks_above_an_unknown_artist_with_the_same_length():
    r = accepted([
        resp("unknown", f("Music\\Someone Else\\01 Oak.flac", length=311)),
        resp("folder", f("Music\\Connor Wall\\01 Oak.flac", length=311)),
    ])
    assert r[0].candidate.username == "folder"


def test_known_limit_a_file_called_only_by_its_track_number_is_not_found():
    # the title lives only in a folder name and the file is "01.flac": not matched (documented, not supported)
    assert accepted([resp("a", f("Music\\Connor Wall - Oak (2026)\\01.flac"))]) == []


def test_title_only_match_needs_a_bare_file_name_even_when_the_length_fits():
    w = Wanted(artist="Bakked", title="FFFF", duration=352.7)

    def resp(path, length):
        return [{"username": "u", "hasFreeUploadSlot": True, "queueLength": 0, "uploadSpeed": 1_000_000,
                 "files": [{"filename": path, "size": 1, "bitRate": 320, "length": length}]}]
    # another artist's release sharing the title and a similar length: refused
    assert not rank(w, resp("music\\gintas k - ffff (unpublished album)\\gintas k - ffff - 04 ffff.mp3", 352))
    assert not rank(w, resp("music\\GGGG-Rrrr\\01-ffff-delire_tripant.mp3", 353))
    # a file named just number + title, with the right length: accepted
    assert rank(w, resp("music\\OKS07\\04 FFFF.mp3", 353))
    # the same bare name with a wrong length: refused
    assert not rank(w, resp("music\\OKS07\\04 FFFF.mp3", 200))
