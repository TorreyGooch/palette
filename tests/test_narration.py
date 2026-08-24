"""Beats: a moment that can be seen, heard, or both.

The word manifest `qs cut` has always written is finally read here, so a beat
can name a *range of words* instead of a pair of seconds. These tests use real
manifest shapes rather than stubs, because the whole value of the file is that
its numbers line up with the audio.
"""
import json

import pytest
from fastapi import HTTPException
from PIL import Image

from conftest import add_item
from palette_app import narration as nr
from palette_app import storyboard as sb


# ── helpers ───────────────────────────────────────────────────────────────────

WORDS = ["a", "lobster", "is", "defeated", "in", "a", "dominance", "battle"]


def manifest(words=WORDS, step=0.5, quote=None, duration=None):
    return {
        "clip": "clip.m4a",
        "duration": duration if duration is not None else len(words) * step,
        "attribution": {"person": "Jordan Peterson", "quote_text":
                        quote if quote is not None else " ".join(words)},
        "words": [{"word": w, "start": round(i * step, 3),
                   "end": round((i + 1) * step, 3)}
                  for i, w in enumerate(words)],
    }


def audio_item(root, filename, iid, *, with_manifest=True, duration=4.0, **extra):
    """An audio library item, optionally with the sidecar a cut leaves."""
    item = add_item(root, filename, iid, duration=duration, **extra)
    if with_manifest:
        (root / "media" / filename).with_suffix(".words.json").write_text(
            json.dumps(manifest(duration=duration)), encoding="utf-8")
    return item


def image_item(root, filename, iid):
    buf = root / "media" / filename
    Image.new("RGB", (64, 36), (200, 30, 30)).save(buf)
    add_item(root, filename, iid)
    lib = json.loads((root / "library.json").read_text(encoding="utf-8"))
    for entry in lib["items"]:
        if entry["id"] == iid:
            entry["type"] = "image"
    (root / "library.json").write_text(json.dumps(lib), encoding="utf-8")


@pytest.fixture
def api(library, monkeypatch):
    from palette_app import main

    monkeypatch.setattr(main, "get_library_path", lambda: library)
    return main


# ── reading the manifest ──────────────────────────────────────────────────────

def test_manifest_sits_beside_the_clip(tmp_path):
    assert nr.manifest_path(tmp_path / "qs_cut_x_1_2.m4a").name \
        == "qs_cut_x_1_2.words.json"


def test_a_clip_without_a_manifest_reads_as_none(tmp_path):
    assert nr.load_manifest(tmp_path / "nope.m4a") is None


def test_a_corrupt_manifest_reads_as_none_rather_than_raising(tmp_path):
    (tmp_path / "c.words.json").write_text("{oops", encoding="utf-8")
    assert nr.load_manifest(tmp_path / "c.m4a") is None


def test_a_manifest_that_is_not_an_object_is_refused(tmp_path):
    (tmp_path / "c.words.json").write_text("[1,2,3]", encoding="utf-8")
    assert nr.load_manifest(tmp_path / "c.m4a") is None


# ── choosing a range of words ─────────────────────────────────────────────────

@pytest.mark.parametrize("start,end,expected", [
    (None, None, (0, 7)),      # no bounds means the whole clip
    (1, 3, (1, 3)),
    (0, 0, (0, 0)),            # one word is a legal range
    (-5, 99, (0, 7)),          # clamped to what exists
    (3, 1, (1, 3)),            # reversed bounds are a typo, not an error
    (None, 2, (0, 2)),
    (5, None, (5, 7)),
])
def test_clamp_range(start, end, expected):
    assert nr.clamp_range(8, start, end) == expected


def test_clamping_an_empty_manifest_selects_nothing():
    assert nr.clamp_range(0, 0, 5) == (0, -1)


def test_summarize_takes_times_from_the_first_and_last_word():
    span = nr.summarize(manifest()["words"], 1, 3)
    assert span["text"] == "lobster is defeated"
    assert span["start"] == 0.5           # start of word 1
    assert span["end"] == 2.0             # end of word 3
    assert span["duration"] == 1.5
    assert span["word_count"] == 3


def test_summarize_of_nothing_is_none():
    assert nr.summarize([], 0, 3) is None


# ── binding a clip to a beat ──────────────────────────────────────────────────

def test_bind_uses_word_times_when_a_manifest_exists(library):
    item = audio_item(library, "clip.m4a", "aud-1")
    bound = nr.bind(library / "media", item, 1, 3)
    assert bound["precision"] == "word"
    assert bound["text"] == "lobster is defeated"
    assert bound["duration"] == 1.5


def test_bind_with_no_range_takes_the_whole_clip(library):
    item = audio_item(library, "clip.m4a", "aud-1")
    bound = nr.bind(library / "media", item)
    assert (bound["word_start"], bound["word_end"]) == (0, len(WORDS) - 1)


def test_bind_falls_back_to_the_whole_clip_without_a_manifest(library):
    """`qs pull` stages audio with no sidecar; the beat must still work."""
    item = audio_item(library, "clip.m4a", "aud-1",
                      with_manifest=False, duration=12.5)
    bound = nr.bind(library / "media", item)
    assert bound["precision"] == "clip"
    assert bound["duration"] == 12.5
    assert bound["word_start"] is None


def test_bind_survives_an_item_with_no_file(library):
    assert nr.bind(library / "media", {"id": "x"})["precision"] == "clip"


# ── laying beats out in time ──────────────────────────────────────────────────

def test_beats_lay_end_to_end_on_the_narration():
    rows = nr.lay_out([
        {"id": "a", "narration": {"duration": 4.0}},
        {"id": "b", "narration": {"duration": 2.5}},
    ])
    assert [(r["at"], r["until"]) for r in rows] == [(0.0, 4.0), (4.0, 6.5)]


def test_a_beat_with_no_narration_holds_rather_than_inventing_a_length():
    rows = nr.lay_out([
        {"id": "a", "narration": {"duration": 4.0}},
        {"id": "b", "narration": None},
        {"id": "c", "narration": {"duration": 1.0}},
    ])
    assert rows[1]["duration"] is None
    assert rows[1]["at"] == rows[1]["until"] == 4.0
    assert (rows[2]["at"], rows[2]["until"]) == (4.0, 5.0)


# ── beats through the API ─────────────────────────────────────────────────────

def test_adding_an_audio_item_makes_a_beat_that_speaks(api, library):
    """The item's type decides which half of the beat it fills."""
    audio_item(library, "clip.m4a", "aud-1")
    board = api.storyboard_create(body={"name": "B"})
    result = api.storyboard_add_panels(board["id"], body={"item_ids": ["aud-1"]})

    beat = result["panels"][0]
    assert beat["item_id"] is None
    assert beat["narration"]["item_id"] == "aud-1"
    assert beat["narration"]["text"] == " ".join(WORDS)


def test_adding_an_image_still_makes_a_beat_that_is_seen(api, library):
    image_item(library, "a.png", "img-a")
    board = api.storyboard_create(body={"name": "B"})
    beat = api.storyboard_add_panels(
        board["id"], body={"item_ids": ["img-a"]})["panels"][0]
    assert beat["item_id"] == "img-a"
    assert beat["narration"] is None


def test_a_beat_can_be_heard_without_being_seen(api, library):
    """The change that matters: this used to be impossible to store."""
    audio_item(library, "clip.m4a", "aud-1")
    board = api.storyboard_create(body={"name": "B"})
    result = api.storyboard_update(board["id"], body={"panels": [
        {"narration": {"item_id": "aud-1", "word_start": 1, "word_end": 3}}]})

    assert len(result["panels"]) == 1
    assert result["panels"][0]["narration"]["text"] == "lobster is defeated"
    # and it survives a reload
    assert len(api.storyboard_get(board["id"])["panels"]) == 1


def test_a_beat_that_is_neither_seen_nor_heard_is_dropped(api, library):
    image_item(library, "a.png", "img-a")
    board = api.storyboard_create(body={"name": "B"})
    result = api.storyboard_update(board["id"], body={"panels": [
        {"item_id": "img-a"}, {"note": "an intention and nothing else"}]})
    assert len(result["panels"]) == 1


def test_a_beat_can_be_both_seen_and_heard(api, library):
    image_item(library, "a.png", "img-a")
    audio_item(library, "clip.m4a", "aud-1")
    board = api.storyboard_create(body={"name": "B"})
    beat = api.storyboard_update(board["id"], body={"panels": [
        {"item_id": "img-a", "narration": {"item_id": "aud-1"}}]})["panels"][0]
    assert beat["item_id"] == "img-a"
    assert beat["narration"]["item_id"] == "aud-1"


def test_only_the_word_range_is_stored_not_the_times(api, library):
    """Times are re-read from the manifest, so they cannot rot on disk."""
    audio_item(library, "clip.m4a", "aud-1")
    board = api.storyboard_create(body={"name": "B"})
    api.storyboard_update(board["id"], body={"panels": [
        {"narration": {"item_id": "aud-1", "word_start": 1, "word_end": 3}}]})

    stored = sb.load_board(library, board["id"])["panels"][0]["narration"]
    assert stored == {"item_id": "aud-1", "word_start": 1, "word_end": 3}


def test_a_stale_client_duration_is_ignored(api, library):
    audio_item(library, "clip.m4a", "aud-1")
    board = api.storyboard_create(body={"name": "B"})
    beat = api.storyboard_update(board["id"], body={"panels": [
        {"narration": {"item_id": "aud-1", "word_start": 1, "word_end": 3,
                       "duration": 999.0, "text": "not what he said"}}]})["panels"][0]
    assert beat["narration"]["duration"] == 1.5
    assert beat["narration"]["text"] == "lobster is defeated"


def test_an_out_of_range_word_index_is_clamped_not_fatal(api, library):
    audio_item(library, "clip.m4a", "aud-1")
    board = api.storyboard_create(body={"name": "B"})
    beat = api.storyboard_update(board["id"], body={"panels": [
        {"narration": {"item_id": "aud-1", "word_start": 0,
                       "word_end": 9999}}]})["panels"][0]
    assert beat["narration"]["word_end"] == len(WORDS) - 1


def test_a_narration_only_beat_is_not_reported_as_missing(api, library):
    """`missing` means an image was asked for and lost, not simply absent."""
    audio_item(library, "clip.m4a", "aud-1")
    board = api.storyboard_create(body={"name": "B"})
    beat = api.storyboard_add_panels(
        board["id"], body={"item_ids": ["aud-1"]})["panels"][0]
    assert beat["missing"] is False


def test_a_narration_clip_that_left_the_library_says_so(api, library):
    audio_item(library, "clip.m4a", "aud-1")
    board = api.storyboard_create(body={"name": "B"})
    api.storyboard_add_panels(board["id"], body={"item_ids": ["aud-1"]})

    lib = json.loads((library / "library.json").read_text(encoding="utf-8"))
    lib["items"] = []
    (library / "library.json").write_text(json.dumps(lib), encoding="utf-8")

    assert api.storyboard_get(board["id"])["panels"][0]["narration"]["missing"] is True


def test_the_board_reports_where_each_beat_falls_in_time(api, library):
    audio_item(library, "one.m4a", "aud-1")
    audio_item(library, "two.m4a", "aud-2")
    board = api.storyboard_create(body={"name": "B"})
    result = api.storyboard_add_panels(board["id"],
                                       body={"item_ids": ["aud-1", "aud-2"]})

    timeline = result["timeline"]
    assert timeline[0]["at"] == 0.0
    assert timeline[1]["at"] == timeline[0]["until"] > 0


# ── rendering a beat that speaks ──────────────────────────────────────────────

def beat(image=None, quote=None, note=""):
    return {"image": image, "quote": quote, "note": note}


def test_a_quote_renders_as_a_card_not_a_hole(tmp_path):
    out = tmp_path / "b.png"
    result = sb.render_storyboard(
        [beat(quote="a lobster is defeated in a dominance battle")], out)
    assert result["ok"] is True
    assert result["missing"] == []
    assert out.exists()


def test_a_lost_image_is_still_reported_even_when_a_quote_carries_the_beat(tmp_path):
    result = sb.render_storyboard(
        [beat(image=tmp_path / "gone.png", quote="still here")],
        tmp_path / "b.png")
    assert result["missing"] == [1]      # the image loss is not swallowed
    assert result["ok"] is True


def test_a_beat_with_neither_is_still_reported(tmp_path):
    assert sb.render_storyboard([beat(note="nothing")],
                                tmp_path / "b.png")["missing"] == [1]


def test_a_long_quote_does_not_overflow_its_panel(tmp_path):
    """The card clips to the box rather than painting over the next row."""
    long_quote = "the lobster in question " * 40
    result = sb.render_storyboard([beat(quote=long_quote), beat(quote="short")],
                                  tmp_path / "b.png", cols=2, tile_width=200)
    assert result["ok"] is True
    assert result["grid"] == "2x1"
