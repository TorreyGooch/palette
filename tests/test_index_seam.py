"""The join between choosing a boundary and storing one.

Selection happens in *seconds* — you look at the timings, you see where the
speaker holds, you pick a moment. Everything durable stores *word indices*,
because an index means something to a person ("from 'lobster' to
'antidepressants'") and still points somewhere after a re-cut.

Nothing joined those two, so the conversion was done by hand on every beat.
Measured in one session: the same subtraction written four times, with a
copy-paste of an episode id, two floats and two indices in the middle of it —
"every one a typo away from a fluent cut of the wrong sentence".

Three fixes, one seam:
  - a pause names the words beside it by index, not only by spelling
  - a clip's own indexed words are reachable without first committing it to a
    beat (they were being read off disk, around the API, to get them)
  - a search hit says whether cutting it needs the network
"""
import json

import pytest
from fastapi import HTTPException


# -- a pause has to be addressable -------------------------------------------

@pytest.fixture
def word_map(monkeypatch, tmp_path):
    """word_map over canned whisper output — no ffmpeg, no GPU."""
    from quotesource import cut

    ep = tmp_path / "ep"
    ep.mkdir()
    monkeypatch.setattr(cut, "_find_episode_dir", lambda _: ep)
    monkeypatch.setattr(cut, "_source_media", lambda *a: ep / "audio.m4a")
    monkeypatch.setattr(cut, "_decode_window", lambda *a, **k: None)
    #                    "so" ... 600ms hold ... "so" again: the same spelling
    monkeypatch.setattr(cut, "window_words", lambda *a, **k: [
        {"word": "so", "start": 0.0, "end": 0.20},
        {"word": "strangely,", "start": 0.25, "end": 0.60},
        {"word": "so", "start": 1.20, "end": 1.40},
        {"word": "few", "start": 1.45, "end": 1.70},
    ])
    return lambda: cut.word_map("EP1", 10.0, 12.0, pad=0.0)


def test_every_word_carries_its_position(word_map):
    assert [w["index"] for w in word_map()["words"]] == [0, 1, 2, 3]


def test_a_pause_names_the_words_on_both_sides_by_index(word_map):
    (pause,) = word_map()["pauses"]
    assert pause["after_index"] == 1 and pause["after_word"] == "strangely,"
    assert pause["next_index"] == 2 and pause["next_word"] == "so"


def test_the_index_disambiguates_a_word_that_occurs_twice(word_map):
    """The reason a name is not enough.

    "so" is words 0 and 2 here, so a caller handed only `after_word` and
    searching the list by spelling could end the cut in the wrong place.
    """
    words = word_map()["words"]
    (pause,) = word_map()["pauses"]

    assert [w["word"] for w in words].count(pause["next_word"]) == 2
    # word_map reports absolute source time: the window starts at 10.0
    assert words[pause["next_index"]]["start"] == pytest.approx(11.20)


def test_the_gap_is_still_reported_in_seconds(word_map):
    """Indices are added, not substituted: selection is still done on time."""
    (pause,) = word_map()["pauses"]
    assert pause["gap"] == pytest.approx(0.6)
    assert pause["at"] == pytest.approx(10.60)


# -- the clip's words, without committing it to a beat first ------------------

def clip(root, item_id="clip1"):
    """A staged cut with the manifest `qs cut` writes beside it."""
    from tests.conftest import add_item

    name = f"qs_cut_EP_{item_id}.m4a"
    add_item(root, name, item_id)
    manifest = {
        "duration": 2.30,
        "attribution": {"precision": "word_accurate"},
        "words": [
            {"word": "hierarchies.", "start": 0.0, "end": 0.40},
            {"word": "And", "start": 1.10, "end": 1.30},
            {"word": "strangely,", "start": 1.35, "end": 1.80},
            {"word": "few", "start": 2.16, "end": 2.30},
        ],
    }
    (root / "media" / name).with_suffix(".words.json").write_text(
        json.dumps(manifest), encoding="utf-8")
    return item_id


@pytest.fixture
def app(library, monkeypatch):
    from palette_app import main

    monkeypatch.setattr(main, "_root", lambda: library)
    return main


def test_a_clip_reports_its_words_with_positions(app, library):
    out = app.item_words(clip(library))

    assert out["word_count"] == 4
    assert [w["index"] for w in out["words"]] == [0, 1, 2, 3]
    assert out["words"][2]["word"] == "strangely,"


def test_the_pauses_are_precomputed_with_indices(app, library):
    """The arithmetic that was written by hand four times in one session."""
    out = app.item_words(clip(library))

    assert [(p["after_index"], p["gap"]) for p in out["pauses"]] == [
        (0, pytest.approx(0.70)), (2, pytest.approx(0.36))]
    assert out["pauses"][0]["after_word"] == "hierarchies."


def test_min_gap_selects_which_holds_count(app, library):
    out = app.item_words(clip(library), min_gap=0.5)
    assert [p["after_index"] for p in out["pauses"]] == [0]


def test_it_needs_no_audio_at_all(app, library):
    """The point of reading the manifest: it works on a 403'd episode.

    An episode whose media YouTube refuses can still have its existing clips
    subdivided, because the manifest is the artifact and it is already local.
    """
    item = clip(library)
    (library / "media" / f"qs_cut_EP_{item}.m4a").unlink()

    assert app.item_words(item)["word_count"] == 4


def test_a_clip_with_no_manifest_says_so_rather_than_looking_silent(app,
                                                                    library):
    """`qs pull` writes no sidecar, and an empty list reads as 'no speech'."""
    from tests.conftest import add_item

    add_item(library, "pulled.m4a", "pulled")
    out = app.item_words("pulled")

    assert out["precision"] == "clip"
    assert out["words"] == [] and out["pauses"] == []
    assert "no word manifest" in out["detail"]


def test_an_unknown_item_is_404(app):
    with pytest.raises(HTTPException) as raised:
        app.item_words("nope")
    assert raised.value.status_code == 404


def test_the_indices_are_the_ones_a_beat_stores(app, library):
    """The whole point: what comes back can be PATCHed straight into a beat.

    A range chosen here must select the same words when the board re-reads the
    manifest, or the endpoint is only a second opinion.
    """
    from palette_app import narration

    item = clip(library)
    out = app.item_words(item)
    end_on = out["pauses"][0]["after_index"]

    media = library / "media" / f"qs_cut_EP_{item}.m4a"
    bound = narration.summarize(
        narration.word_list(narration.load_manifest(media)), 0, end_on)

    assert bound["text"] == "hierarchies."
    assert bound["word_start"] == 0 and bound["word_end"] == end_on


# -- does cutting this hit need the network? ---------------------------------

@pytest.fixture
def corpus(tmp_path, monkeypatch):
    monkeypatch.setenv("QUOTESOURCE_DATA", str(tmp_path))
    return tmp_path


def row(episode_id="EP1", source_id="src", duplicate_of=None):
    return (episode_id, source_id, 1.0, 2.0, "text", 0.8,
            "Title", "20240101", "https://y/watch?v=EP1", "clean", duplicate_of)


def test_a_hit_whose_audio_is_on_disk_says_stored(corpus):
    from quotesource import search
    from quotesource.paths import episode_dir

    ep = episode_dir("src", "EP1")
    ep.mkdir(parents=True)
    (ep / "audio.m4a").write_bytes(b"x")

    assert search._hit(row())["audio_stored"] is True


def test_a_hit_with_no_stored_audio_says_false_not_a_verdict(corpus):
    """Not "fetchable" and not "refused".

    Nothing knows an episode can be fetched until it fetches it, and a 403 is
    a fact about one attempt that decays - so neither belongs in a field read
    as a property of the episode.
    """
    from quotesource import search
    from quotesource.paths import episode_dir

    episode_dir("src", "EP1").mkdir(parents=True)

    assert search._hit(row())["audio_stored"] is False


def test_the_state_is_per_episode_not_per_source(corpus):
    from quotesource import search
    from quotesource.paths import episode_dir

    for ep_id, has_audio in (("EP1", True), ("EP2", False)):
        d = episode_dir("src", ep_id)
        d.mkdir(parents=True)
        if has_audio:
            (d / "audio.m4a").write_bytes(b"x")

    assert search._hit(row("EP1"))["audio_stored"] is True
    assert search._hit(row("EP2"))["audio_stored"] is False


def test_an_unanswerable_question_is_null_not_a_guess(corpus, monkeypatch):
    """A hit is worth returning even when the question cannot be answered, and
    `None` is the absence of a fact rather than a third state of the world."""
    from quotesource import search

    def gone(*a, **k):
        raise OSError("gone")

    monkeypatch.setattr(search, "episode_dir", gone)

    assert search._hit(row())["audio_stored"] is None


# -- an older end must degrade legibly, not silently --------------------------

def test_the_new_fields_are_advertised_as_capabilities():
    """`audio_stored` is produced by the *server*, so a new desktop against an
    old one sees hits with the field simply absent. That is the failure the
    capability list exists to prevent — it is how the staging flag went
    unnoticed — so each of these is announced rather than assumed.
    """
    from palette_app import main

    for capability in ("word_index", "hit_audio", "clip_words",
                       "words_match_cut", "hit_duplicates"):
        assert capability in main.CAPABILITIES
