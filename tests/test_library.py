"""Library roots and item deletion.

Both bugs covered here caused real damage: staging failed on a root that was
not fully built, and deleting one item destroyed media that other items were
still using.
"""
import asyncio
import json

import pytest

from conftest import add_item, read_library


# ── ensure_library ────────────────────────────────────────────────────────────

def test_builds_a_bare_root(tmp_path):
    from palette_app.library import ensure_library

    root = tmp_path / "fresh"
    ensure_library(root)

    for folder in ("media", "thumbnails", "exports"):
        assert (root / folder).is_dir(), f"{folder} not created"
    assert (root / "library.json").exists()


def test_completes_a_partial_root(tmp_path):
    """A root holding only a corpus — exactly how the server was assembled."""
    from palette_app.library import ensure_library

    root = tmp_path / "partial"
    (root / "quotesource").mkdir(parents=True)

    ensure_library(root)

    assert (root / "media").is_dir()
    assert (root / "library.json").exists()
    assert (root / "quotesource").is_dir(), "must not disturb existing content"


def test_preserves_an_existing_database(library):
    """Idempotent: re-running must never wipe items."""
    from palette_app.library import ensure_library

    add_item(library, "keep.m4a", "item-1")
    ensure_library(library)

    assert [i["id"] for i in read_library(library)["items"]] == ["item-1"]


# ── delete_item ───────────────────────────────────────────────────────────────

def delete(root, item_id, monkeypatch):
    from palette_app import main

    monkeypatch.setattr(main, "_root", lambda: root)
    return asyncio.run(main.delete_item(item_id))


def test_delete_removes_the_item_and_its_files(library, monkeypatch):
    add_item(library, "solo.m4a", "solo-1", sidecar=True)

    delete(library, "solo-1", monkeypatch)

    assert read_library(library)["items"] == []
    assert not (library / "media" / "solo.m4a").exists()
    assert not (library / "media" / "solo.words.json").exists()
    assert not (library / "thumbnails" / "solo-1.jpg").exists()


def test_delete_keeps_media_another_item_still_uses(library, monkeypatch):
    """The regression: clip names truncate bounds to whole seconds, so two
    cuts a fraction apart collide and several items share one file."""
    add_item(library, "shared.m4a", "first", sidecar=True)
    add_item(library, "shared.m4a", "second")

    delete(library, "first", monkeypatch)

    assert (library / "media" / "shared.m4a").exists(), \
        "deleting one item destroyed media the other still references"
    assert (library / "media" / "shared.words.json").exists()
    assert [i["id"] for i in read_library(library)["items"]] == ["second"]
    assert not (library / "thumbnails" / "first.jpg").exists(), \
        "thumbnails are per-item and should still go"


def test_delete_removes_media_with_the_last_reference(library, monkeypatch):
    add_item(library, "shared.m4a", "first", sidecar=True)
    add_item(library, "shared.m4a", "second")

    delete(library, "first", monkeypatch)
    delete(library, "second", monkeypatch)

    assert not (library / "media" / "shared.m4a").exists()
    assert not (library / "media" / "shared.words.json").exists(), \
        "sidecar left behind orphans beside media that no longer exists"
    assert read_library(library)["items"] == []


def test_delete_unknown_item_is_404(library, monkeypatch):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as e:
        delete(library, "no-such-id", monkeypatch)
    assert e.value.status_code == 404
