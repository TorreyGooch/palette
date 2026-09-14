"""What a rendered board says, and how long a split beat lasts.

Two changes asked for together, because both are about the handoff.

**The PNG printed too much.** Every panel carried its note and its image
prompt in full - the reasoning and the craft instructions - and a board of
real writing became a wall of text that buried the pictures. The handoff is
what the video should do: the board's description under the title, and each
beat's video prompt under its panel. The rest stays on the page.

**A split lost its pause.** A beat is measured first word to last, so the
silence between two halves of one quote belonged to neither and fell out of
the timeline. Video is generated from these times and the audio laid back
alongside afterwards, so the whole piece came out short at exactly the
speaker's hold.
"""
import json

import pytest

from tests.conftest import add_item
from tests.test_storyboard import api, image_item  # noqa: F401
from tests.test_video_prompt import render_bytes


# -- what the PNG prints ------------------------------------------------------

def one_beat_board(api, library, **beat):
    image_item(library, "a.png", "img-a")
    board = api.storyboard_create(body={"name": "Cold Open"})
    api.storyboard_update(board["id"], body={"panels": [
        {"item_id": "img-a", **beat}]})
    return board["id"]


def test_the_note_is_not_printed(api, library):
    """Why a beat is here is for the people working on it, not the handoff."""
    bid = one_beat_board(api, library, video_prompt="slow push in")
    without = render_bytes(api, library, bid, cols=1, title="")

    api.storyboard_update(bid, body={"panels": [
        {"item_id": "img-a", "video_prompt": "slow push in",
         "note": "the argument turns here, look at it like a biologist " * 6}]})
    with_note = render_bytes(api, library, bid, cols=1, title="")

    assert with_note == without


def test_the_image_prompt_is_not_printed(api, library):
    """It drew the reference; the reference is already on the panel."""
    bid = one_beat_board(api, library, video_prompt="slow push in")
    without = render_bytes(api, library, bid, cols=1, title="")

    api.storyboard_update(bid, body={"panels": [
        {"item_id": "img-a", "video_prompt": "slow push in",
         "image_prompt": "macro, single lobster on wet black basalt " * 6}]})
    with_prompt = render_bytes(api, library, bid, cols=1, title="")

    assert with_prompt == without


def test_the_video_prompt_is_printed(api, library):
    bid = one_beat_board(api, library)
    without = render_bytes(api, library, bid, cols=1, title="")

    api.storyboard_update(bid, body={"panels": [
        {"item_id": "img-a", "video_prompt": "pull back, not cut"}]})
    with_prompt = render_bytes(api, library, bid, cols=1, title="")

    assert with_prompt != without


def test_the_boards_description_is_printed_under_the_title(api, library):
    bid = one_beat_board(api, library)
    without = api.storyboard_render(bid, body={"cols": 1})

    api.storyboard_update(bid, body={
        "description": "why a lobster is the wrong place to look for a self"})
    with_description = api.storyboard_render(bid, body={"cols": 1})

    assert with_description["height"] > without["height"]


def test_dropping_the_title_drops_the_description_with_it(api, library):
    """The description belongs to the header."""
    bid = one_beat_board(api, library)
    without = render_bytes(api, library, bid, cols=1, title="")

    api.storyboard_update(bid, body={"description": "a whole piece " * 10})
    with_description = render_bytes(api, library, bid, cols=1, title="")

    assert with_description == without


def test_a_beat_with_only_an_image_prompt_is_not_missing(api, library):
    """Nothing was lost; nothing was shot yet."""
    board = api.storyboard_create(body={"name": "Shot list"})
    api.storyboard_update(board["id"], body={"panels": [
        {"image_prompt": "a single cell as landscape"}]})

    assert api.storyboard_render(board["id"], body={})["missing"] == []


def test_a_beat_with_only_references_is_not_missing(api, library):
    """The normal state after a generate: references, none chosen."""
    board = api.storyboard_create(body={"name": "Shot list"})
    api.storyboard_update(board["id"], body={"panels": [
        {"candidates": ["ref-1", "ref-2"]}]})

    assert api.storyboard_render(board["id"], body={})["missing"] == []


def test_a_lost_image_is_still_missing_whatever_else_is_written(api, library):
    bid = one_beat_board(api, library, video_prompt="push in",
                         image_prompt="a lobster")
    (library / "media" / "a.png").unlink()

    assert api.storyboard_render(bid, body={})["missing"] == [1]


# -- a split keeps its pause --------------------------------------------------

# Clip-relative word times with a real hold: 1.36 s of silence after "is".
GAPPED = [("a", 0.00, 0.40), ("lobster", 0.40, 0.90), ("is", 1.00, 1.20),
          ("defeated", 2.56, 3.00), ("in", 3.00, 3.20), ("battle", 3.30, 3.80)]


def gapped_clip(library, filename="gap.m4a", iid="gap-1"):
    add_item(library, filename, iid, duration=4.0)
    (library / "media" / filename).with_suffix(".words.json").write_text(
        json.dumps({"duration": 4.0,
                    "attribution": {"person": "Jordan Peterson"},
                    "words": [{"word": w, "start": s, "end": e}
                              for w, s, e in GAPPED]}),
        encoding="utf-8")
    return iid


def split_board(api, library):
    clip = gapped_clip(library)
    board = api.storyboard_create(body={"name": "Timing"})
    beat = api.storyboard_update(board["id"], body={"panels": [
        {"narration": {"item_id": clip}}]})["panels"][0]
    return board["id"], beat["id"]


def test_split_halves_add_up_to_the_whole_across_a_pause(api, library):
    """The bug: 6.96 + 11.98 for a 20.3 s beat. The pause fell out."""
    bid, pid = split_board(api, library)
    whole = api.storyboard_get(bid)["panels"][0]["narration"]["duration"]

    out = api.storyboard_split_beat(bid, pid, body={"at_word": 3})

    first, second = (p["narration"] for p in out["panels"])
    assert first["duration"] + second["duration"] == pytest.approx(whole)
    assert out["timeline"][-1]["until"] == pytest.approx(whole)


def test_the_pause_stays_on_the_earlier_beat(api, library):
    """The picture already up holds through the silence."""
    bid, pid = split_board(api, library)

    out = api.storyboard_split_beat(bid, pid, body={"at_word": 3})

    first, second = (p["narration"] for p in out["panels"])
    assert first["hold_s"] == pytest.approx(1.36)
    assert first["end"] == pytest.approx(second["start"]) == pytest.approx(2.56)
    assert first["duration"] == pytest.approx(2.56)
    assert "hold_s" not in second
    assert second["duration"] == pytest.approx(1.24)


def test_it_is_derived_on_read_so_nothing_new_is_stored(api, library):
    from palette_app.storyboard import board_path

    bid, pid = split_board(api, library)
    api.storyboard_split_beat(bid, pid, body={"at_word": 3})

    stored = json.loads(board_path(library, bid).read_text(encoding="utf-8"))
    for beat in stored["panels"]:
        assert set(beat["narration"]) == {"item_id", "word_start", "word_end"}


def test_a_split_at_a_word_with_no_pause_is_just_as_whole(api, library):
    """Splitting anywhere, not only on a hold."""
    bid, pid = split_board(api, library)
    whole = api.storyboard_get(bid)["panels"][0]["narration"]["duration"]

    out = api.storyboard_split_beat(bid, pid, body={"at_word": 1})

    first, second = (p["narration"] for p in out["panels"])
    assert first["duration"] + second["duration"] == pytest.approx(whole)
    assert first["hold_s"] == pytest.approx(0.0)


def test_beats_that_skip_words_between_them_keep_their_own_length(api, library):
    """Words 0-1 then 4-5: nothing continues, so nothing is stretched."""
    clip = gapped_clip(library)
    board = api.storyboard_create(body={"name": "Timing"})
    out = api.storyboard_update(board["id"], body={"panels": [
        {"narration": {"item_id": clip, "word_start": 0, "word_end": 1}},
        {"narration": {"item_id": clip, "word_start": 4, "word_end": 5}}]})

    first = out["panels"][0]["narration"]
    assert first["duration"] == pytest.approx(0.90)
    assert "hold_s" not in first


def test_beats_on_different_clips_keep_their_own_length(api, library):
    clip_a = gapped_clip(library, "a.m4a", "clip-a")
    clip_b = gapped_clip(library, "b.m4a", "clip-b")
    board = api.storyboard_create(body={"name": "Timing"})
    out = api.storyboard_update(board["id"], body={"panels": [
        {"narration": {"item_id": clip_a, "word_start": 0, "word_end": 2}},
        {"narration": {"item_id": clip_b, "word_start": 3, "word_end": 5}}]})

    first = out["panels"][0]["narration"]
    assert first["duration"] == pytest.approx(1.20)
    assert "hold_s" not in first
