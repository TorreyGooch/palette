"""Splitting a speaking beat, and the board writes it depends on.

A twelve-second quote is several shots. The shape for that already existed -
beats can bind one clip with back-to-back word ranges, each with its own
prompt, references and duration - so splitting is an operation, not a new
kind of beat. These tests pin what the operation promises: the two halves say
exactly what the whole said, the writing stays on the first half, and the
split cannot be undone by a page that had not heard about it.

The second half of this file is the bug the Generate button would have hit on
its first use. A generate attaches references minutes after the page loaded
the board, and every autosave the page sent in the meantime carried the beat's
old candidate list - so typing a note while the GPU rendered erased the images
it had just made. Board writes also had no lock and were not atomic.
"""
import json

import pytest
from fastapi import HTTPException

from tests.test_narration import audio_item
from tests.test_storyboard import api  # noqa: F401

# test_narration's manifest: eight words, 0.5 s each, back to back.
#   0 a  1 lobster  2 is  3 defeated  4 in  5 a  6 dominance  7 battle


def speaking_board(api, library, **beat):
    audio_item(library, "clip.m4a", "clip-1")
    board = api.storyboard_create(body={"name": "Cold Open"})
    saved = api.storyboard_update(board["id"], body={"panels": [
        {"narration": {"item_id": "clip-1"}, **beat}]})
    return board["id"], saved["panels"][0]["id"]


def narration(panel):
    return panel["narration"]


# -- what a split produces ----------------------------------------------------

def test_the_two_halves_say_exactly_what_the_whole_said(api, library):
    bid, pid = speaking_board(api, library)
    whole = narration(api.storyboard_get(bid)["panels"][0])

    out = api.storyboard_split_beat(bid, pid, body={"at_word": 3})

    first, second = (narration(p) for p in out["panels"])
    assert first["text"] == "a lobster is"
    assert second["text"] == "defeated in a dominance battle"
    assert f"{first['text']} {second['text']}" == whole["text"]


def test_the_halves_add_up_to_the_original_duration(api, library):
    """Durations are derived from the words, so nothing can be lost between."""
    bid, pid = speaking_board(api, library)
    whole = narration(api.storyboard_get(bid)["panels"][0])["duration"]

    out = api.storyboard_split_beat(bid, pid, body={"at_word": 3})

    assert sum(narration(p)["duration"] for p in out["panels"]) == \
        pytest.approx(whole)


def test_the_word_ranges_are_back_to_back_on_the_same_clip(api, library):
    bid, pid = speaking_board(api, library)

    out = api.storyboard_split_beat(bid, pid, body={"at_word": 3})

    first, second = (narration(p) for p in out["panels"])
    assert (first["word_start"], first["word_end"]) == (0, 2)
    assert (second["word_start"], second["word_end"]) == (3, 7)
    assert first["item_id"] == second["item_id"] == "clip-1"


def test_a_beat_already_narrowed_splits_inside_its_own_range(api, library):
    bid, pid = speaking_board(api, library)
    api.storyboard_update(bid, body={"panels": [
        {"id": pid, "narration": {"item_id": "clip-1", "word_start": 2,
                                  "word_end": 6}}]})

    out = api.storyboard_split_beat(bid, pid, body={"at_word": 4})

    first, second = (narration(p) for p in out["panels"])
    assert (first["word_start"], first["word_end"]) == (2, 3)
    assert (second["word_start"], second["word_end"]) == (4, 6)


def test_the_writing_stays_on_the_first_half(api, library):
    """Guessing which half a sentence belongs to is worse than a blank."""
    bid, pid = speaking_board(api, library, note="why it is here",
                              image_prompt="a lobster", video_prompt="push in",
                              candidates=["ref-1"], item_id="img-1")

    out = api.storyboard_split_beat(bid, pid, body={"at_word": 3})

    first, second = out["panels"]
    assert first["id"] == pid
    assert (first["note"], first["image_prompt"], first["video_prompt"]) == \
        ("why it is here", "a lobster", "push in")
    assert first["candidates"] == ["ref-1"] and first["item_id"] == "img-1"
    assert (second["note"], second["image_prompt"], second["video_prompt"]) == \
        ("", "", "")
    assert second["candidates"] == [] and second["item_id"] is None


def test_the_new_beat_goes_directly_after_and_nothing_else_moves(api, library):
    audio_item(library, "clip.m4a", "clip-1")
    board = api.storyboard_create(body={"name": "Cold Open"})
    saved = api.storyboard_update(board["id"], body={"panels": [
        {"image_prompt": "before"},
        {"narration": {"item_id": "clip-1"}},
        {"image_prompt": "after"}]})
    before, middle, after = (p["id"] for p in saved["panels"])

    out = api.storyboard_split_beat(board["id"], middle, body={"at_word": 3})

    ids = [p["id"] for p in out["panels"]]
    assert ids[:2] == [before, middle] and ids[3] == after
    assert ids[2] == out["split"]["second"]


def test_the_split_is_on_disk_not_just_in_the_response(api, library):
    bid, pid = speaking_board(api, library)

    api.storyboard_split_beat(bid, pid, body={"at_word": 3})

    assert len(api.storyboard_get(bid)["panels"]) == 2


# -- what a split refuses -----------------------------------------------------

@pytest.mark.parametrize("at", [0, 8, 99, -1])
def test_a_split_point_outside_the_beat_is_refused(api, library, at):
    """At the first word there would be an empty first half; past the last,
    an empty second one."""
    bid, pid = speaking_board(api, library)

    with pytest.raises(HTTPException) as raised:
        api.storyboard_split_beat(bid, pid, body={"at_word": at})

    assert raised.value.status_code == 400
    assert len(api.storyboard_get(bid)["panels"]) == 1, "and nothing changed"


def test_no_split_point_is_refused(api, library):
    bid, pid = speaking_board(api, library)

    with pytest.raises(HTTPException) as raised:
        api.storyboard_split_beat(bid, pid, body={})

    assert raised.value.status_code == 400


def test_a_beat_that_does_not_speak_cannot_be_split(api, library):
    board = api.storyboard_create(body={"name": "Shot list"})
    pid = api.storyboard_update(board["id"], body={"panels": [
        {"image_prompt": "a lobster"}]})["panels"][0]["id"]

    with pytest.raises(HTTPException) as raised:
        api.storyboard_split_beat(board["id"], pid, body={"at_word": 1})

    assert raised.value.status_code == 400


def test_a_clip_with_no_word_manifest_cannot_be_split(api, library):
    """A pull clip has no words, so there is nothing to split between."""
    audio_item(library, "pulled.m4a", "pull-1", with_manifest=False)
    board = api.storyboard_create(body={"name": "Cold Open"})
    pid = api.storyboard_update(board["id"], body={"panels": [
        {"narration": {"item_id": "pull-1"}}]})["panels"][0]["id"]

    with pytest.raises(HTTPException) as raised:
        api.storyboard_split_beat(board["id"], pid, body={"at_word": 1})

    assert raised.value.status_code == 400
    assert "manifest" in raised.value.detail


def test_an_unknown_beat_is_404(api, library):
    bid, _ = speaking_board(api, library)

    with pytest.raises(HTTPException) as raised:
        api.storyboard_split_beat(bid, "no-such-beat", body={"at_word": 3})

    assert raised.value.status_code == 404


# -- a stale page cannot erase what the server attached -----------------------

def test_an_autosave_from_a_stale_page_keeps_attached_references(api, library):
    """The Generate button's first-use bug: references attached while the page
    held the old board, then erased by the page's next autosave."""
    bid, pid = speaking_board(api, library)
    stale = api.storyboard_get(bid)["panels"]

    api._append_candidates(library, bid, pid, ["ref-1", "ref-2"])
    stale[0]["note"] = "typed while the GPU was busy"
    saved = api.storyboard_update(bid, body={"panels": stale})

    beat = saved["panels"][0]
    assert beat["candidates"] == ["ref-1", "ref-2"]
    assert beat["note"] == "typed while the GPU was busy", "and the edit lands"


def test_a_beat_the_page_deleted_is_not_brought_back(api, library):
    bid, pid = speaking_board(api, library)
    api._append_candidates(library, bid, pid, ["ref-1"])

    saved = api.storyboard_update(bid, body={"panels": []})

    assert saved["panels"] == []


def test_the_page_can_still_add_candidates_the_server_lacks(api, library):
    bid, pid = speaking_board(api, library)
    api._append_candidates(library, bid, pid, ["ref-1"])
    beats = api.storyboard_get(bid)["panels"]
    beats[0]["candidates"] = ["ref-1", "ref-2"]

    saved = api.storyboard_update(bid, body={"panels": beats})

    assert saved["panels"][0]["candidates"] == ["ref-1", "ref-2"]


# -- every board read-modify-write holds the lock ----------------------------

@pytest.fixture
def lock_spy(api, library, monkeypatch):
    """Record, at each board save, whether the library lock was held."""
    from palette_app import library as lib_mod

    held = []
    real_save = api.save_board

    def spy(root, board):
        depths = getattr(lib_mod._held, "depths", {}) or {}
        held.append(depths.get(lib_mod._lock_key(root), 0) > 0)
        return real_save(root, board)

    monkeypatch.setattr(api, "save_board", spy)
    import palette_app.storyboard as sb_mod
    monkeypatch.setattr(sb_mod, "save_board", spy)
    return held


def test_patch_holds_the_lock_while_it_saves(api, library, lock_spy):
    bid, _ = speaking_board(api, library)
    lock_spy.clear()

    api.storyboard_update(bid, body={"name": "Renamed"})

    assert lock_spy == [True]


def test_attaching_references_holds_the_lock(api, library, lock_spy):
    bid, pid = speaking_board(api, library)
    lock_spy.clear()

    api._append_candidates(library, bid, pid, ["ref-1"])

    assert lock_spy == [True]


def test_splitting_holds_the_lock(api, library, lock_spy):
    bid, pid = speaking_board(api, library)
    lock_spy.clear()

    api.storyboard_split_beat(bid, pid, body={"at_word": 3})

    assert lock_spy == [True]


def test_appending_items_holds_the_lock(api, library, lock_spy):
    bid, _ = speaking_board(api, library)
    lock_spy.clear()

    api.storyboard_add_panels(bid, body={"item_ids": ["clip-1"]})

    assert lock_spy == [True]


# -- a board is never half-written -------------------------------------------

def test_a_failed_write_leaves_the_previous_board_intact(api, library,
                                                        monkeypatch):
    from palette_app import library as lib_mod
    from palette_app.storyboard import board_path

    bid, _ = speaking_board(api, library)
    path = board_path(library, bid)
    before = path.read_text(encoding="utf-8")

    def refuse(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(lib_mod.os, "replace", refuse)
    with pytest.raises(OSError):
        api.storyboard_update(bid, body={"name": "Never lands"})

    assert path.read_text(encoding="utf-8") == before
    assert json.loads(before)["name"] == "Cold Open"
    assert not list(path.parent.glob("*.tmp")), "and no temp file left behind"
