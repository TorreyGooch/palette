"""Getting references from the GPU into a beat, without touching anything else.

Three things this has to get right, all of them learned the hard way
elsewhere in this codebase:

**Generating never selects.** A finished batch leaves `candidates` on the beat
and `item_id` alone. Choosing is a judgement and it stays with a person unless
someone asks otherwise — which is what lets an unattended run fill a whole
board without guessing at anyone's taste.

**Attaching is additive.** `PATCH /api/storyboards/{id}` replaces the panel
list wholesale, so a generate that read the board, appended and sent it back
would silently discard whatever was written while the GPU was busy — and a
batch takes minutes. The same lost update that made batch-tag necessary.

**References stay out of the way.** Three per beat per round fills a picker
faster than anything else here, and most are rejected on sight. Kept, because
"actually the second one was better" is real; hidden, because otherwise the
library becomes unusable within a session.
"""
import json

import pytest
from fastapi import HTTPException

from tests.test_storyboard import api  # noqa: F401
from tests.conftest import add_item


# -- references are kept but out of the way ---------------------------------

def tagged(library, iid, name, *tags):
    add_item(library, name, iid)
    lib = json.loads((library / "library.json").read_text(encoding="utf-8"))
    for entry in lib["items"]:
        if entry["id"] == iid:
            entry["tags"] = list(tags)
    (library / "library.json").write_text(json.dumps(lib), encoding="utf-8")


def test_generated_references_are_absent_from_the_default_listing(api, library):
    tagged(library, "ref-1", "gen_a.png", "reference", "generated")
    tagged(library, "real-1", "photo.png", "quotesource")

    assert [i["id"] for i in api.list_items()] == ["real-1"]


def test_asking_for_them_by_flag_shows_them(api, library):
    tagged(library, "ref-1", "gen_a.png", "reference", "generated")
    tagged(library, "real-1", "photo.png", "quotesource")

    assert {i["id"] for i in api.list_items(references=True)} == {"ref-1",
                                                                  "real-1"}


def test_asking_for_the_tag_by_name_shows_them(api, library):
    """Nothing becomes unreachable — this hides a default, not the items."""
    tagged(library, "ref-1", "gen_a.png", "reference", "generated")

    assert [i["id"] for i in api.list_items(tag="reference")] == ["ref-1"]


def test_hiding_them_does_not_disturb_another_filter(api, library):
    tagged(library, "ref-1", "gen_a.png", "reference")
    tagged(library, "real-1", "photo.png", "quotesource")

    assert [i["id"] for i in api.list_items(tag="quotesource")] == ["real-1"]


# -- attaching to a beat -----------------------------------------------------

def board_with_beat(api, prompt="a defeated lobster"):
    board = api.storyboard_create(body={"name": "Cold Open"})
    saved = api.storyboard_update(board["id"],
                                  body={"panels": [{"image_prompt": prompt}]})
    return board["id"], saved["panels"][0]["id"]


def test_candidates_are_appended_not_replaced(api, library):
    """Two rounds of three, and the first three still there."""
    board_id, beat_id = board_with_beat(api)

    api._append_candidates(library, board_id, beat_id, ["a", "b"])
    out = api._append_candidates(library, board_id, beat_id, ["c"])

    assert out["candidates"] == ["a", "b", "c"]


def test_appending_does_not_disturb_anything_else_on_the_board(api, library):
    """The lost update this avoids: a note written while the GPU was busy."""
    board_id, beat_id = board_with_beat(api)
    board = api.storyboard_get(board_id)
    api.storyboard_update(board_id, body={"panels": [
        {**board["panels"][0], "note": "written while rendering"}]})

    api._append_candidates(library, board_id, beat_id, ["a"])

    beat = api.storyboard_get(board_id)["panels"][0]
    assert beat["note"] == "written while rendering"
    assert beat["candidates"] == ["a"]


def test_the_same_reference_is_not_added_twice(api, library):
    board_id, beat_id = board_with_beat(api)

    api._append_candidates(library, board_id, beat_id, ["a", "b"])
    out = api._append_candidates(library, board_id, beat_id, ["b", "c"])

    assert out["candidates"] == ["a", "b", "c"]


def test_an_unknown_beat_is_404_rather_than_a_silent_no_op(api, library):
    board_id, _ = board_with_beat(api)

    with pytest.raises(HTTPException) as raised:
        api._append_candidates(library, board_id, "nope", ["a"])

    assert raised.value.status_code == 404


# -- the job ----------------------------------------------------------------

@pytest.fixture
def fake_gpu(api, monkeypatch):
    """generate() without a GPU: two images, bytes we can recognise."""
    from palette_app import generate as gen

    monkeypatch.setattr(gen, "generate", lambda prompt, **k: {
        "workflow": "reference", "prompt": prompt, "count": 2,
        "seeds": [1, 2],
        "images": [{"filename": "a.png", "subfolder": "", "type": "output"},
                   {"filename": "b.png", "subfolder": "", "type": "output"}]})
    monkeypatch.setattr(gen, "fetch_image", lambda image: b"\x89PNG" + b"x" * 32)
    monkeypatch.setattr(api, "_remote", lambda: None)
    return gen


def run_job(api, body):
    """Start a generate and wait for the thread to finish."""
    import time

    job_id = api.generate_references(body=body)["job_id"]
    for _ in range(200):
        job = api.qs_pull_status(job_id)
        if job.get("done"):
            return job
        time.sleep(0.02)
    raise AssertionError("job never finished")


def test_a_finished_batch_registers_every_image(api, library, fake_gpu):
    job = run_job(api, {"prompt": "a defeated lobster"})

    assert job["error"] is None
    assert len(job["item_ids"]) == 2
    stored = {i["id"]: i for i in api.list_items(references=True)}
    assert all(iid in stored for iid in job["item_ids"])


def test_every_reference_is_tagged_so_it_stays_hidden(api, library, fake_gpu):
    job = run_job(api, {"prompt": "a defeated lobster"})

    stored = {i["id"]: i for i in api.list_items(references=True)}
    for iid in job["item_ids"]:
        assert "reference" in stored[iid]["tags"]
        assert "generated" in stored[iid]["tags"]
    assert api.list_items() == [], "and none of them in the default listing"


def test_generating_attaches_candidates_but_selects_nothing(api, library,
                                                             fake_gpu):
    """The rule the whole design rests on."""
    board_id, beat_id = board_with_beat(api)

    job = run_job(api, {"prompt": "a defeated lobster",
                        "board_id": board_id, "beat_id": beat_id})

    beat = api.storyboard_get(board_id)["panels"][0]
    assert beat["candidates"] == job["item_ids"]
    assert beat["item_id"] is None, "generating must never choose"


def test_without_a_beat_the_references_are_just_registered(api, library,
                                                            fake_gpu):
    """Generating for its own sake is legitimate — not every render is a beat."""
    job = run_job(api, {"prompt": "a lobster"})

    assert len(job["item_ids"]) == 2
    assert "beat" not in job


def test_an_empty_prompt_is_refused_before_a_job_exists(api, fake_gpu):
    with pytest.raises(HTTPException) as raised:
        api.generate_references(body={"prompt": "   "})

    assert raised.value.status_code == 400


def test_a_failure_is_reported_on_the_job_rather_than_swallowed(api, library,
                                                                 monkeypatch):
    from palette_app import generate as gen

    monkeypatch.setattr(api, "_remote", lambda: None)

    def boom(*a, **k):
        raise gen.GenerateError("no workflow templates in /nowhere")

    monkeypatch.setattr(gen, "generate", boom)

    job = run_job(api, {"prompt": "a lobster"})

    assert job["stage"] == "failed"
    assert "no workflow templates" in job["error"]
