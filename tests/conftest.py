"""Shared fixtures.

Every test runs against a throwaway library under tmp_path. Nothing here
touches the real library, the corpus, the network, the GPU or ffmpeg — the
suite is meant to run in seconds, anywhere, with no setup.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def library(tmp_path):
    """An empty but valid library root."""
    from palette_app.library import ensure_library

    root = tmp_path / "Library"
    ensure_library(root)
    return root


def add_item(root: Path, filename: str, item_id: str, *, sidecar: bool = False,
             content: bytes = b"x" * 64, **extra) -> dict:
    """Put a media file and its library entry in place.

    Files are dummy bytes on purpose: the behaviour under test is which files
    get unlinked and which entries survive, none of which decodes the media.
    """
    (root / "media" / filename).write_bytes(content)
    if sidecar:
        sidecar_path = (root / "media" / filename).with_suffix(".words.json")
        sidecar_path.write_text(json.dumps({"words": []}), encoding="utf-8")
    (root / "thumbnails" / f"{item_id}.jpg").write_bytes(b"jpg")

    item = {"id": item_id, "filename": filename, "type": "audio",
            "title": filename, "url": None, "tags": [], "palettes": [],
            "duration": 1.0, "fps": None, "added": "2026-01-01T00:00:00"}
    item.update(extra)

    lib = json.loads((root / "library.json").read_text(encoding="utf-8"))
    lib["items"].append(item)
    (root / "library.json").write_text(json.dumps(lib, indent=2), encoding="utf-8")
    return item


def read_library(root: Path) -> dict:
    return json.loads((root / "library.json").read_text(encoding="utf-8"))
