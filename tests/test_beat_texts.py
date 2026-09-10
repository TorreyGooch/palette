"""Four texts on a beat, split by how long each one stays true.

  note           why this beat is here. The audit trail.
  description    what happens in this moment, in plain language.
  image_prompt   how to render one frame of it.
  video_prompt   how to render the motion, authored last.

The split is about **lifetime**, not tidiness. A description — *the lobster
loses and its posture collapses* — survives a change of model, of style, of
diffusion stack entirely. A prompt is written *at* a particular model and is
stale the day you swap it. Merged into one field you lose the durable half to
keep the disposable one.

`video_prompt` used to hold what is now `description`: it was the only text
besides the note, so it accumulated both jobs. Renaming it while almost
nothing had been written was the cheap moment.
"""
import json

import pytest
from fastapi import HTTPException

from tests.test_storyboard import api, image_item  # noqa: F401


def panels_of(board):
    return board["panels"]


TEXTS = ("description", "image_prompt", "video_prompt")


# -- any one of them makes a beat --------------------------------------------

@pytest.mark.parametrize("field", TEXTS)
def test_any_single_text_is_enough_to_be_a_beat(api, field):
    """The rule widened with the fields.

    `_clean_panels` drops a panel that is neither seen nor heard nor asked
    for, and a beat written only as a sentence about what should happen is
    the earliest and most useful kind. Dropping it on save deletes the
    thinking silently, which is the worst shape a bug can take.
    """
    board = api.storyboard_create(body={"name": "Shot list"})

    saved = api.storyboard_update(board["id"],
                                  body={"panels": [{field: "a defeated lobster"}]})

    assert len(panels_of(saved)) == 1
    assert panels_of(saved)[0][field] == "a defeated lobster"


def test_a_beat_with_no_text_and_no_asset_is_still_dropped(api):
    """Widening the rule must not turn it off."""
    board = api.storyboard_create(body={"name": "Shot list"})

    saved = api.storyboard_update(board["id"], body={"panels": [
        {"description": "   ", "image_prompt": "", "video_prompt": None,
         "note": "a note is not a beat"}]})

    assert panels_of(saved) == []


def test_a_note_alone_is_not_a_beat(api):
    """Unchanged, and deliberate. The note says why something is here; with
    nothing here, there is nothing for it to be about."""
    board = api.storyboard_create(body={"name": "Shot list"})

    saved = api.storyboard_update(board["id"],
                                  body={"panels": [{"note": "this matters"}]})

    assert panels_of(saved) == []


# -- they are four fields, not one -------------------------------------------

def test_all_four_survive_together(api):
    board = api.storyboard_create(body={"name": "Shot list"})

    saved = api.storyboard_update(board["id"], body={"panels": [{
        "note": "the argument turns here",
        "description": "the lobster loses and its posture collapses",
        "image_prompt": "defeated crustacean, low angle, cold rim light",
        "video_prompt": "slow push in, 35mm, 4s",
    }]})

    beat = panels_of(saved)[0]
    assert beat["note"] == "the argument turns here"
    assert beat["description"].startswith("the lobster loses")
    assert beat["image_prompt"].startswith("defeated crustacean")
    assert beat["video_prompt"] == "slow push in, 35mm, 4s"


def test_editing_a_prompt_leaves_the_description_alone(api):
    """The point of the split: rewriting for a new model must not cost the
    plain-language record of what the beat is."""
    board = api.storyboard_create(body={"name": "Shot list"})
    api.storyboard_update(board["id"], body={"panels": [{
        "description": "the lobster loses",
        "image_prompt": "sdxl phrasing, cinematic, 8k"}]})

    saved = api.storyboard_update(board["id"], body={"panels": [{
        **panels_of(api.storyboard_get(board["id"]))[0],
        "image_prompt": "entirely different phrasing for another model"}]})

    beat = panels_of(saved)[0]
    assert beat["description"] == "the lobster loses"
    assert beat["image_prompt"].startswith("entirely different")


def test_the_texts_round_trip_through_disk(api):
    board = api.storyboard_create(body={"name": "Shot list"})
    api.storyboard_update(board["id"], body={"panels": [{
        "description": "a two-headed worm regrows",
        "image_prompt": "macro, wet, cold light",
        "video_prompt": "hold, then a slow bloom"}]})

    reopened = api.storyboard_get(board["id"])

    beat = panels_of(reopened)[0]
    assert beat["description"] == "a two-headed worm regrows"
    assert beat["image_prompt"] == "macro, wet, cold light"
    assert beat["video_prompt"] == "hold, then a slow bloom"


# -- what the rendered board says when there is no picture -------------------

def test_the_render_prefers_the_description(api):
    """The PNG is read by a person, and plain language is what serves them.
    The prompts are instructions to a model."""
    from palette_app.main import beat_text

    assert beat_text({"description": "the lobster loses",
                      "image_prompt": "low angle, cold rim light"}) == \
        "the lobster loses"


def test_it_falls_through_so_a_prompt_only_beat_still_says_something(api):
    from palette_app.main import beat_text

    assert beat_text({"image_prompt": "low angle"}) == "low angle"
    assert beat_text({"video_prompt": "slow push in"}) == "slow push in"
    assert beat_text({"note": "why it is here"}) == "", "a note is not shot text"


def test_whitespace_is_not_content(api):
    from palette_app.main import beat_text

    assert beat_text({"description": "  \t ", "image_prompt": "macro"}) == "macro"


# -- candidates: generated, not yet chosen between ---------------------------

def test_a_beat_can_hold_candidates_with_nothing_selected(api):
    """The normal state after an unattended run. Selecting is a judgement,
    and it is left to a person unless someone asks otherwise."""
    board = api.storyboard_create(body={"name": "Shot list"})

    saved = api.storyboard_update(board["id"], body={"panels": [
        {"image_prompt": "a lobster", "candidates": ["img-a", "img-b", "img-c"]}]})

    beat = panels_of(saved)[0]
    assert beat["candidates"] == ["img-a", "img-b", "img-c"]
    assert beat["item_id"] is None, "generating must not select"


def test_candidates_alone_make_a_beat(api):
    """Its prompt could be cleared afterwards; the references are still real."""
    board = api.storyboard_create(body={"name": "Shot list"})

    saved = api.storyboard_update(board["id"],
                                  body={"panels": [{"candidates": ["img-a"]}]})

    assert len(panels_of(saved)) == 1


def test_selecting_one_leaves_the_others_listed(api, library):
    """So a choice can be reconsidered without generating again."""
    image_item(library, "chosen.png", "img-b")
    board = api.storyboard_create(body={"name": "Shot list"})

    saved = api.storyboard_update(board["id"], body={"panels": [
        {"candidates": ["img-a", "img-b", "img-c"], "item_id": "img-b"}]})

    beat = panels_of(saved)[0]
    assert beat["item_id"] == "img-b"
    assert beat["candidates"] == ["img-a", "img-b", "img-c"]


def test_empty_candidate_ids_are_dropped(api):
    board = api.storyboard_create(body={"name": "Shot list"})

    saved = api.storyboard_update(board["id"], body={"panels": [
        {"image_prompt": "a lobster", "candidates": ["img-a", "", None]}]})

    assert panels_of(saved)[0]["candidates"] == ["img-a"]
