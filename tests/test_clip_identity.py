"""Correcting a cut must not cost you the thing that referred to it.

Two halves of one bug. Clip filenames truncated to whole seconds, and ffmpeg
writes with -y, so re-cutting a quote a fraction of a second differently
overwrote the old audio *and* its word manifest — while the old library item
went on pointing at that filename. A storyboard beat bound to it then resolved
its words against a manifest that no longer belonged to it: a fluent clip of a
different sentence in the right voice, which is precisely what the alignment
guard exists to prevent, arriving by a route the guard cannot see.

And correcting a cut minted a *new* item, so every reference to the old one —
a beat, and the reasoning written under it — was left behind.

Sub-second adjustment is the normal correction, not an edge case: `qs words`
hands you boundaries like 477.45 against 477.60, and the whole workflow is
"end just before a pause".
"""
import json

import pytest

from palette_app.library import (load_library, replace_item_media,
                                 save_library)
from quotesource.cut import clip_filename


# -- the filename ------------------------------------------------------------

def test_a_sub_second_difference_is_a_different_file():
    """The collision that silently overwrote a clip in place."""
    a = clip_filename("PWasTAtR6Ns", 477.45, 487.15)
    b = clip_filename("PWasTAtR6Ns", 477.60, 487.15)
    assert a != b


def test_bounds_are_carried_to_the_millisecond():
    assert clip_filename("EP", 477.45, 487.15) == "qs_cut_EP_477450_487150.m4a"


def test_the_same_bounds_still_give_the_same_name():
    """Idempotence is worth keeping: the same cut is the same artifact."""
    assert clip_filename("EP", 1.5, 2.5) == clip_filename("EP", 1.5, 2.5)


def test_two_cuts_that_shared_a_name_in_the_real_library_no_longer_do():
    """Both of these start inside second 477 and only the end saved them."""
    a = clip_filename("PWasTAtR6Ns", 477.8, 487.12)
    b = clip_filename("PWasTAtR6Ns", 477.03, 484.85)
    c = clip_filename("PWasTAtR6Ns", 477.03, 487.99)   # the near miss, realised
    assert len({a, b, c}) == 3


# -- identity survives the edit ----------------------------------------------

@pytest.fixture(autouse=True)
def _no_ffmpeg(monkeypatch):
    """Keep this file to the suite's contract: no ffmpeg, anywhere.

    replace_item_media probes the new media and rebuilds the thumbnail, both
    of which shell out. It passed locally because this machine has ffmpeg and
    failed on the Windows runner, which does not - the exact asymmetry the
    two-OS CI exists to catch, and a reminder that "it passed here" is not the
    claim conftest makes.
    """
    async def fake_probe(path):
        return {"duration": 9.7, "fps": None}

    async def fake_audio_thumbnail(path, thumb):
        thumb.parent.mkdir(parents=True, exist_ok=True)
        thumb.write_bytes(b"JPEG")
        return True

    monkeypatch.setattr("palette_app.api.media.probe", fake_probe)
    monkeypatch.setattr("palette_app.api.media.audio_thumbnail",
                        fake_audio_thumbnail)


@pytest.fixture
def staged(library):
    """A library holding one cut clip, with a manifest and a board-ish note."""
    from conftest import add_item

    media = library / "media"
    (media / "qs_cut_EP_477450_487150.m4a").write_bytes(b"OLD AUDIO")
    (media / "qs_cut_EP_477450_487150.words.json").write_text(
        json.dumps({"words": [{"word": "old", "start": 0.0, "end": 0.4}]}),
        encoding="utf-8")
    add_item(library, "qs_cut_EP_477450_487150.m4a", "clip-1")

    lib = load_library(library)
    item = next(i for i in lib["items"] if i["id"] == "clip-1")
    item["manifest"] = "qs_cut_EP_477450_487150.words.json"
    item["tags"] = ["quotesource", "word-cut", "Jordan Peterson"]
    item["palettes"] = ["pal-1"]
    item["attribution"] = {"person": "Jordan Peterson", "episode_id": "EP",
                           "range": [477.45, 487.15], "quote_text": "old words"}
    lib["palettes"] = [{"id": "pal-1", "name": "Narration"}]
    save_library(library, lib)
    return library


def new_clip(library, name="qs_cut_EP_477600_487150.m4a"):
    (library / "media" / name).write_bytes(b"NEW AUDIO")
    (library / "media" / name.replace(".m4a", ".words.json")).write_text(
        json.dumps({"words": [{"word": "new", "start": 0.0, "end": 0.4}]}),
        encoding="utf-8")
    return name


@pytest.mark.anyio
async def test_the_item_id_survives_a_correction(staged):
    """A board beat points at the id. Minting a new one abandons the note."""
    name = new_clip(staged)

    report = await replace_item_media(
        staged, "clip-1", name,
        manifest=name.replace(".m4a", ".words.json"),
        attribution={"person": "Jordan Peterson", "episode_id": "EP",
                     "range": [477.6, 487.15], "quote_text": "new words"})

    assert report["item_id"] == "clip-1"
    item = next(i for i in load_library(staged)["items"] if i["id"] == "clip-1")
    assert item["filename"] == name
    assert item["attribution"]["range"] == [477.6, 487.15]


@pytest.mark.anyio
async def test_curation_is_not_lost_to_a_timing_fix(staged):
    """Tags and palettes are the judgment. A nudged boundary is not."""
    name = new_clip(staged)

    await replace_item_media(staged, "clip-1", name,
                             manifest=name.replace(".m4a", ".words.json"))

    item = next(i for i in load_library(staged)["items"] if i["id"] == "clip-1")
    assert item["tags"] == ["quotesource", "word-cut", "Jordan Peterson"]
    assert item["palettes"] == ["pal-1"]


@pytest.mark.anyio
async def test_the_old_media_and_manifest_are_removed(staged):
    """Nothing refers to them any more, and clips are not small."""
    name = new_clip(staged)

    report = await replace_item_media(
        staged, "clip-1", name, manifest=name.replace(".m4a", ".words.json"))

    assert not (staged / "media" / "qs_cut_EP_477450_487150.m4a").exists()
    assert not (staged / "media" / "qs_cut_EP_477450_487150.words.json").exists()
    assert set(report["removed"]) == {"qs_cut_EP_477450_487150.m4a",
                                      "qs_cut_EP_477450_487150.words.json"}


@pytest.mark.anyio
async def test_media_another_item_still_points_at_is_kept(staged):
    """Several items can legitimately share one file; this is not a licence."""
    from conftest import add_item

    add_item(staged, "qs_cut_EP_477450_487150.m4a", "clip-2")
    name = new_clip(staged)

    report = await replace_item_media(
        staged, "clip-1", name, manifest=name.replace(".m4a", ".words.json"))

    assert (staged / "media" / "qs_cut_EP_477450_487150.m4a").exists()
    assert "qs_cut_EP_477450_487150.m4a" not in report["removed"]


@pytest.mark.anyio
async def test_the_report_says_what_moved(staged):
    """A correction should be auditable, not silent."""
    name = new_clip(staged)

    report = await replace_item_media(
        staged, "clip-1", name,
        manifest=name.replace(".m4a", ".words.json"),
        attribution={"range": [477.6, 487.15]})

    assert report["old_range"] == [477.45, 487.15]
    assert report["new_range"] == [477.6, 487.15]
    assert report["old_filename"] == "qs_cut_EP_477450_487150.m4a"
    assert report["new_filename"] == name


@pytest.mark.anyio
async def test_replacing_an_unknown_item_is_refused(staged):
    name = new_clip(staged)
    with pytest.raises(KeyError):
        await replace_item_media(staged, "nope", name)


@pytest.mark.anyio
async def test_media_that_is_not_there_is_refused_before_anything_changes(
        staged):
    """Pointing an item at a file that does not exist is worse than failing."""
    with pytest.raises(FileNotFoundError):
        await replace_item_media(staged, "clip-1", "never_written.m4a")

    item = next(i for i in load_library(staged)["items"] if i["id"] == "clip-1")
    assert item["filename"] == "qs_cut_EP_477450_487150.m4a"
    assert (staged / "media" / "qs_cut_EP_477450_487150.m4a").exists()


# -- the endpoint ------------------------------------------------------------

@pytest.fixture
def api(staged, monkeypatch):
    from palette_app import main

    monkeypatch.setattr(main, "get_library_path", lambda: staged)
    monkeypatch.setattr(main, "_remote", lambda: None)
    return main


def test_recut_takes_the_episode_from_the_item_not_the_caller(api,
                                                              monkeypatch):
    """Same quote, moved. Re-supplying the source invites miscitation."""
    from fastapi import HTTPException          # noqa: F401  (parity of import)

    seen = {}
    monkeypatch.setattr(api, "qs_cut", lambda body: seen.update(body) or {"ok": 1})

    api.qs_recut(body={"item_id": "clip-1", "start": 477.6, "end": 487.15})

    assert seen["episode_id"] == "EP"
    assert seen["person"] == "Jordan Peterson"
    assert seen["replace_item"] == "clip-1"


def test_recutting_an_item_with_no_attribution_is_refused(api, staged):
    from fastapi import HTTPException
    from conftest import add_item

    add_item(staged, "plain.mp4", "plain-1")
    with pytest.raises(HTTPException) as raised:
        api.qs_recut(body={"item_id": "plain-1", "start": 1, "end": 2})
    assert raised.value.status_code == 400
    assert "qs cut" in raised.value.detail


def test_recutting_an_unknown_item_is_404(api):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as raised:
        api.qs_recut(body={"item_id": "nope", "start": 1, "end": 2})
    assert raised.value.status_code == 404


@pytest.mark.parametrize("body", [
    {"item_id": "clip-1"},
    {"item_id": "clip-1", "start": 5},
    {"item_id": "clip-1", "start": "x", "end": 9},
])
def test_bounds_are_required_and_numeric(api, body):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as raised:
        api.qs_recut(body=body)
    assert raised.value.status_code == 400


def test_a_reversed_range_is_refused(api):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as raised:
        api.qs_recut(body={"item_id": "clip-1", "start": 9, "end": 5})
    assert raised.value.status_code == 400


def test_recut_is_advertised_as_a_capability():
    from palette_app import main

    assert "recut" in main.CAPABILITIES
