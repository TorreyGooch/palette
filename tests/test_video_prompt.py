"""A beat can be asked for as well as seen or heard.

The third way a beat exists points forward: nothing has been shot or found
yet, and the prompt says what to make. That is the interesting case, and the
rule as written deleted it — `_clean_panels` dropped any panel with neither an
item nor a narration, so a prompt-only beat vanished silently on the next
save. Saving something and getting back less than you wrote is the worst shape
a bug can take, because nothing reports it.

The prompt is deliberately not the note. The note says *why* this beat is
here and is the audit trail that makes a board a decision rather than an asset
list; the prompt says *what to generate*. One field for both, and the
reasoning gets crowded out by craft instructions.
"""
import json

import pytest
from fastapi import HTTPException

# `library` is a conftest fixture and arrives on its own; these two are
# defined alongside the other storyboard tests.
from tests.test_storyboard import api, image_item  # noqa: F401


def panels_of(board):
    return board["panels"]


# -- the trap: a beat that is only a prompt ----------------------------------

def test_a_prompt_only_beat_survives_a_save(api):
    """It has no item and no narration, and it is the most useful kind."""
    board = api.storyboard_create(body={"name": "Shot list"})

    saved = api.storyboard_update(board["id"], body={"panels": [
        {"video_prompt": "slow push in on a lobster, tank light, 35mm"}]})

    assert len(panels_of(saved)) == 1
    assert panels_of(saved)[0]["video_prompt"].startswith("slow push in")


def test_a_prompt_only_beat_survives_a_round_trip(api):
    """Written, re-read from disk, still there."""
    board = api.storyboard_create(body={"name": "Shot list"})
    api.storyboard_update(board["id"], body={"panels": [
        {"video_prompt": "a two-headed worm, macro, cold light"}]})

    reopened = api.storyboard_get(board["id"])
    assert panels_of(reopened)[0]["video_prompt"] == (
        "a two-headed worm, macro, cold light")


def test_a_beat_with_nothing_at_all_is_still_dropped(api):
    """Widening the rule must not turn it off."""
    board = api.storyboard_create(body={"name": "B"})

    saved = api.storyboard_update(board["id"], body={"panels": [
        {"note": "a thought with no beat attached"}]})

    assert panels_of(saved) == []


def test_whitespace_is_not_a_prompt(api):
    board = api.storyboard_create(body={"name": "B"})

    saved = api.storyboard_update(board["id"], body={"panels": [
        {"video_prompt": "   \n\t  "}]})

    assert panels_of(saved) == []


def test_a_note_alone_does_not_keep_a_beat_alive(api):
    """The note is not one of the three ways a beat exists."""
    board = api.storyboard_create(body={"name": "B"})
    saved = api.storyboard_update(board["id"], body={"panels": [
        {"note": "why this matters", "video_prompt": ""}]})
    assert panels_of(saved) == []


# -- prompt and note are separate fields -------------------------------------

def test_prompt_and_note_are_kept_apart(api, library):
    image_item(library, "a.png", "img-a")
    board = api.storyboard_create(body={"name": "B"})

    saved = api.storyboard_update(board["id"], body={"panels": [
        {"item_id": "img-a",
         "note": "he is not making a point about lobsters yet",
         "video_prompt": "macro, shallow depth of field"}]})

    beat = panels_of(saved)[0]
    assert beat["note"] == "he is not making a point about lobsters yet"
    assert beat["video_prompt"] == "macro, shallow depth of field"


def test_a_visual_beat_needs_no_prompt(api, library):
    image_item(library, "a.png", "img-a")
    board = api.storyboard_create(body={"name": "B"})

    saved = api.storyboard_update(board["id"], body={
        "panels": [{"item_id": "img-a"}]})

    assert panels_of(saved)[0]["video_prompt"] == ""


# -- existing boards keep loading --------------------------------------------

def test_a_board_written_before_prompts_existed_still_opens(api, library):
    """Every stored board predates this field; none of them may break."""
    from palette_app.storyboard import board_path, load_board, save_board

    image_item(library, "a.png", "img-a")
    board = api.storyboard_create(body={"name": "Old"})
    api.storyboard_update(board["id"], body={
        "panels": [{"item_id": "img-a", "note": "kept"}]})

    # Strip the field back out, exactly as a board from before this change
    # would sit on disk.
    path = board_path(library, board["id"])
    raw = json.loads(path.read_text(encoding="utf-8"))
    for panel in raw["panels"]:
        panel.pop("video_prompt", None)
    path.write_text(json.dumps(raw, indent=2), encoding="utf-8")
    assert "video_prompt" not in json.loads(
        path.read_text(encoding="utf-8"))["panels"][0]

    opened = api.storyboard_get(board["id"])
    assert panels_of(opened)[0]["note"] == "kept"
    assert panels_of(opened)[0].get("video_prompt", "") == ""


# -- it renders ---------------------------------------------------------------

def test_a_prompt_only_board_renders(api, library):
    """A board of pure prompts should read as a shot list, not abort."""
    board = api.storyboard_create(body={"name": "Shot list"})
    api.storyboard_update(board["id"], body={"panels": [
        {"video_prompt": "slow push in on a lobster"},
        {"video_prompt": "cut to a two-headed worm"}]})

    result = api.storyboard_render(board["id"], body={"cols": 2})

    assert result["panels"] == 2
    assert result["size_bytes"] > 0
    assert (library / "exports" / result["filename"]).exists()


def test_a_prompt_only_beat_is_not_reported_missing(api, library):
    """`missing` means an image was asked for and lost. None was asked for."""
    board = api.storyboard_create(body={"name": "Shot list"})
    api.storyboard_update(board["id"], body={
        "panels": [{"video_prompt": "a lobster, tank light"}]})

    result = api.storyboard_render(board["id"], body={})

    assert result["missing"] == []


def test_a_lost_image_is_still_reported_when_a_prompt_carries_the_beat(
        api, library):
    """The signal has to survive the beat having something else to draw."""
    image_item(library, "a.png", "img-a")
    board = api.storyboard_create(body={"name": "B"})
    api.storyboard_update(board["id"], body={"panels": [
        {"item_id": "img-a", "video_prompt": "re-shoot this wider"}]})
    (library / "media" / "a.png").unlink()

    result = api.storyboard_render(board["id"], body={})

    assert result["missing"] == [1]
