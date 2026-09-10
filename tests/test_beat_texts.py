"""Where plain language lives, and where instructions to a model live.

A beat had four texts for a while: `note`, `description`, `image_prompt`,
`video_prompt`, split on the argument that a description survives a change of
model and a prompt does not.

Three beats of real writing broke that. The durable/disposable split holds for
*how a thing is shot* and collapses for *what is in it*: "single lobster on
wet dark rock" became "single lobster on wet black basalt", and all the prompt
added was styling. The subject got written twice.

The better reading is that the thing wanting a plain-language account was
never the shot — it was the **piece**. So a beat now carries `note`,
`image_prompt` and `video_prompt`, and the **board** carries a `description`
of the whole video. A beat says what is in front of the camera; the board says
what the video is.

`note` survived the same test and stayed: "the argument is about mechanism, so
look at it the way a biologist would" is not the same kind of sentence as
anything you would hand a model.
"""
import json

import pytest

from tests.test_storyboard import api  # noqa: F401


def panels_of(board):
    return board["panels"]


TEXTS = ("image_prompt", "video_prompt")


# -- what the board says it is -----------------------------------------------

def test_a_board_carries_a_description_of_the_whole_piece(api):
    """The level no beat can speak for, because a beat only knows its frame."""
    board = api.storyboard_create(body={"name": "Cold Open"})

    saved = api.storyboard_update(board["id"], body={
        "description": "why a lobster is the wrong place to look for a self"})

    assert saved["description"].startswith("why a lobster")


def test_the_description_survives_a_reopen(api):
    board = api.storyboard_create(body={"name": "Cold Open"})
    api.storyboard_update(board["id"], body={"description": "the whole piece"})

    assert api.storyboard_get(board["id"])["description"] == "the whole piece"


def test_it_can_be_cleared_because_not_yet_said_is_a_real_state(api):
    """Unlike the name, which falls back rather than emptying."""
    board = api.storyboard_create(body={"name": "Cold Open"})
    api.storyboard_update(board["id"], body={"description": "a first attempt"})

    saved = api.storyboard_update(board["id"], body={"description": "  "})

    assert saved["description"] == ""
    assert saved["name"] == "Cold Open", "the name still refuses to empty"


def test_a_board_that_predates_the_field_still_opens(api):
    """Additive, so nothing needs migrating — but prove it rather than assume."""
    board = api.storyboard_create(body={"name": "Cold Open"})
    root = api._root()
    from palette_app.storyboard import board_path

    path = board_path(root, board["id"])
    stored = json.loads(path.read_text(encoding="utf-8"))
    stored.pop("description", None)
    path.write_text(json.dumps(stored), encoding="utf-8")

    reopened = api.storyboard_get(board["id"])

    assert reopened["name"] == "Cold Open"
    assert reopened.get("description", "") == ""


def test_editing_beats_does_not_disturb_the_description(api):
    board = api.storyboard_create(body={"name": "Cold Open"})
    api.storyboard_update(board["id"], body={"description": "the whole piece"})

    saved = api.storyboard_update(board["id"],
                                  body={"panels": [{"image_prompt": "a lobster"}]})

    assert saved["description"] == "the whole piece"


# -- any one text makes a beat -----------------------------------------------

@pytest.mark.parametrize("field", TEXTS)
def test_any_single_text_is_enough_to_be_a_beat(api, field):
    """A beat written rather than shot is the earliest and most useful kind,
    and dropping it on save deletes the thinking silently."""
    board = api.storyboard_create(body={"name": "Shot list"})

    saved = api.storyboard_update(board["id"],
                                  body={"panels": [{field: "a defeated lobster"}]})

    assert len(panels_of(saved)) == 1
    assert panels_of(saved)[0][field] == "a defeated lobster"


def test_a_beat_with_no_text_and_no_asset_is_still_dropped(api):
    board = api.storyboard_create(body={"name": "Shot list"})

    saved = api.storyboard_update(board["id"], body={"panels": [
        {"image_prompt": "", "video_prompt": None, "note": "not a beat"}]})

    assert panels_of(saved) == []


def test_a_note_alone_is_not_a_beat(api):
    """The note says why something is here; with nothing here, there is
    nothing for it to be about."""
    board = api.storyboard_create(body={"name": "Shot list"})

    saved = api.storyboard_update(board["id"],
                                  body={"panels": [{"note": "this matters"}]})

    assert panels_of(saved) == []


def test_the_three_beat_texts_survive_together(api):
    board = api.storyboard_create(body={"name": "Shot list"})

    saved = api.storyboard_update(board["id"], body={"panels": [{
        "note": "the argument turns here",
        "image_prompt": "single lobster on wet black basalt, cold rim light",
        "video_prompt": "slow push in, hold static for the last second",
    }]})

    beat = panels_of(saved)[0]
    assert beat["note"] == "the argument turns here"
    assert beat["image_prompt"].startswith("single lobster")
    assert beat["video_prompt"].startswith("slow push in")


def test_rewriting_a_prompt_leaves_the_note_alone(api):
    """What the merge kept: the reasoning is still not in the prompt."""
    board = api.storyboard_create(body={"name": "Shot list"})
    api.storyboard_update(board["id"], body={"panels": [{
        "note": "look at it the way a biologist would",
        "image_prompt": "sdxl phrasing, cinematic, 8k"}]})

    saved = api.storyboard_update(board["id"], body={"panels": [{
        **panels_of(api.storyboard_get(board["id"]))[0],
        "image_prompt": "entirely different phrasing for another model"}]})

    beat = panels_of(saved)[0]
    assert beat["note"] == "look at it the way a biologist would"
    assert beat["image_prompt"].startswith("entirely different")


# -- the beat's old description is folded forward, never dropped -------------

def test_a_legacy_description_is_folded_into_the_prompt(api):
    """On a beat with no picture yet the description *is* the thinking, so
    dropping it would delete the part nobody could reconstruct."""
    board = api.storyboard_create(body={"name": "Shot list"})

    saved = api.storyboard_update(board["id"], body={"panels": [{
        "description": "Nothing around it gives away how big it is",
        "image_prompt": "macro, single lobster on wet black basalt"}]})

    beat = panels_of(saved)[0]
    assert "Nothing around it gives away how big it is" in beat["image_prompt"]
    assert "wet black basalt" in beat["image_prompt"]
    assert "description" not in beat


def test_the_description_comes_first_so_it_reads_as_the_thinking(api):
    board = api.storyboard_create(body={"name": "Shot list"})

    saved = api.storyboard_update(board["id"], body={"panels": [{
        "description": "THINKING", "image_prompt": "STYLING"}]})

    assert panels_of(saved)[0]["image_prompt"] == "THINKING\n\nSTYLING"


def test_a_description_with_no_prompt_becomes_the_prompt(api):
    board = api.storyboard_create(body={"name": "Shot list"})

    saved = api.storyboard_update(board["id"],
                                  body={"panels": [{"description": "a lobster"}]})

    assert panels_of(saved)[0]["image_prompt"] == "a lobster"


def test_folding_does_not_stack_up_over_repeated_saves(api):
    """The migration runs on every read, so it has to be idempotent."""
    board = api.storyboard_create(body={"name": "Shot list"})
    api.storyboard_update(board["id"], body={"panels": [{
        "description": "THINKING", "image_prompt": "STYLING"}]})

    for _ in range(3):
        beats = panels_of(api.storyboard_get(board["id"]))
        api.storyboard_update(board["id"], body={"panels": beats})

    beat = panels_of(api.storyboard_get(board["id"]))[0]
    assert beat["image_prompt"].count("THINKING") == 1
    assert beat["image_prompt"] == "THINKING\n\nSTYLING"


# -- what the rendered board says when there is no picture -------------------

def test_the_render_uses_the_image_prompt(api):
    from palette_app.main import beat_text

    assert beat_text({"image_prompt": "a lobster on basalt",
                      "video_prompt": "slow push in"}) == "a lobster on basalt"


def test_it_falls_through_so_a_motion_only_beat_still_says_something(api):
    from palette_app.main import beat_text

    assert beat_text({"video_prompt": "pull back, not cut"}) == \
        "pull back, not cut"
    assert beat_text({"note": "why it is here"}) == "", "a note is not shot text"


def test_whitespace_is_not_content(api):
    from palette_app.main import beat_text

    assert beat_text({"image_prompt": " \t ", "video_prompt": "pull back"}) == \
        "pull back"


# -- candidates: generated, not yet chosen between ---------------------------

def test_a_beat_can_hold_candidates_with_nothing_selected(api):
    """The normal state after a run, and a finished one — a person clicks
    through the cycler; nothing is expected to resolve it."""
    board = api.storyboard_create(body={"name": "Shot list"})

    saved = api.storyboard_update(board["id"], body={"panels": [
        {"image_prompt": "a lobster", "candidates": ["img-a", "img-b", "img-c"]}]})

    beat = panels_of(saved)[0]
    assert beat["candidates"] == ["img-a", "img-b", "img-c"]
    assert beat["item_id"] is None, "generating must not select"


def test_candidates_alone_make_a_beat(api):
    board = api.storyboard_create(body={"name": "Shot list"})

    saved = api.storyboard_update(board["id"],
                                  body={"panels": [{"candidates": ["img-a"]}]})

    assert len(panels_of(saved)) == 1


def test_empty_candidate_ids_are_dropped(api):
    board = api.storyboard_create(body={"name": "Shot list"})

    saved = api.storyboard_update(board["id"], body={"panels": [
        {"image_prompt": "a lobster", "candidates": ["img-a", "", None]}]})

    assert panels_of(saved)[0]["candidates"] == ["img-a"]


def test_a_plain_read_folds_it_too_without_any_save(api):
    """The bug the tests missed: folding lived only on the write path.

    Every test above arrives through a PATCH, so all of them passed while a
    GET went on returning the old shape — a stored `description` beside a
    prompt that did not contain it. The live board is what showed it. This
    writes the legacy shape to disk directly and never saves through the API.
    """
    import json

    from palette_app.storyboard import board_path

    board = api.storyboard_create(body={"name": "Cold Open"})
    api.storyboard_update(board["id"],
                          body={"panels": [{"image_prompt": "STYLING"}]})

    path = board_path(api._root(), board["id"])
    stored = json.loads(path.read_text(encoding="utf-8"))
    stored["panels"][0]["description"] = "THINKING"
    path.write_text(json.dumps(stored), encoding="utf-8")

    beat = panels_of(api.storyboard_get(board["id"]))[0]

    assert beat["image_prompt"] == "THINKING\n\nSTYLING"
    assert "description" not in beat, "the retired key must not reach a client"
