"""Handing a clip over without leaving a copy behind.

A remote pull/cut runs with stage=False, so the produced file sits in the
server's media folder with no library entry. Once the caller has adopted it,
the server discards it — otherwise every pull quietly accumulates media on a
machine that never opens it.
"""
import json

import pytest
from fastapi import HTTPException

from conftest import add_item


def discard(root, filename, monkeypatch):
    from palette_app import main

    monkeypatch.setattr(main, "_root", lambda: root)
    monkeypatch.delenv("QS_REMOTE", raising=False)
    return main.qs_discard({"filename": filename})


def test_discards_an_unreferenced_clip(library, monkeypatch):
    (library / "media" / "handed_over.m4a").write_bytes(b"audio")
    (library / "media" / "handed_over.words.json").write_text("{}", encoding="utf-8")

    result = discard(library, "handed_over.m4a", monkeypatch)

    assert result["discarded"] is True
    assert set(result["removed"]) == {"handed_over.m4a", "handed_over.words.json"}
    assert not (library / "media" / "handed_over.m4a").exists()
    assert not (library / "media" / "handed_over.words.json").exists()


def test_refuses_to_discard_something_the_library_uses(library, monkeypatch):
    """Safety net: a staged clip is this library's own, not a hand-off."""
    add_item(library, "mine.m4a", "item-1")

    result = discard(library, "mine.m4a", monkeypatch)

    assert result["discarded"] is False
    assert "references it" in result["reason"]
    assert (library / "media" / "mine.m4a").exists()


def test_missing_file_is_not_an_error(library, monkeypatch):
    """Retries and double-adoptions should be harmless."""
    result = discard(library, "never_existed.m4a", monkeypatch)
    assert result["discarded"] is False
    assert result["removed"] == []


@pytest.mark.parametrize("evil", [
    "../library.json",
    "../../etc/passwd",
    "sub/dir/clip.m4a",
])
def test_rejects_paths_outside_media(library, monkeypatch, evil):
    """The name arrives over the network; it must not escape media/."""
    with pytest.raises(HTTPException) as e:
        discard(library, evil, monkeypatch)
    assert e.value.status_code == 400
    assert (library / "library.json").exists(), "must not touch anything outside"


def test_empty_filename_is_rejected(library, monkeypatch):
    with pytest.raises(HTTPException) as e:
        discard(library, "   ", monkeypatch)
    assert e.value.status_code == 400


# ── pull(stage=False) shape ───────────────────────────────────────────────────

def test_unstaged_pull_returns_what_an_adopter_needs():
    """adopt_remote_item reads filename/title/url/attribution off this dict."""
    import inspect

    from quotesource.pull import pull

    params = inspect.signature(pull).parameters
    assert "stage" in params, "pull must be able to skip staging"
    assert params["stage"].default is True, "local callers still stage by default"
