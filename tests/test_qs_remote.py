"""The remote bridge.

These cover the three bugs that only appeared once a real cut ran through
it: the sidecar name, palette ids crossing libraries, and reusing stale
local audio.
"""
import json

import pytest


# ── manifest naming ───────────────────────────────────────────────────────────

def test_manifest_replaces_the_extension():
    """qs cut uses with_suffix; appending gave a name that 404s."""
    from palette_app.qs_remote import manifest_name

    assert manifest_name("qs_cut_ABC_10_20.m4a") == "qs_cut_ABC_10_20.words.json"
    assert manifest_name("clip.mp4") == "clip.words.json"


def test_manifest_prefers_the_path_the_remote_reports():
    from palette_app.qs_remote import manifest_name

    got = manifest_name("clip.m4a",
                        {"manifest": "/home/torrey/palette-library/media/other.words.json"})
    assert got == "other.words.json", "should use the basename the remote gave"


# ── remote_base ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("value,expected", [
    ("http://host:7862", "http://host:7862"),
    ("host:7862", "http://host:7862"),        # scheme filled in
    ("http://host:7862/", "http://host:7862"),  # trailing slash trimmed
    ("  ", None),
    ("", None),
])
def test_remote_base_normalises(monkeypatch, value, expected):
    from palette_app import qs_remote

    monkeypatch.setenv("QS_REMOTE", value)
    assert qs_remote.remote_base() == expected


def test_remote_base_unset_means_local(monkeypatch):
    from palette_app import qs_remote

    monkeypatch.delenv("QS_REMOTE", raising=False)
    assert qs_remote.remote_base() is None


# ── adopting a produced clip ──────────────────────────────────────────────────

@pytest.fixture
def stub_transfer(monkeypatch, library):
    """Stand in for the network and for ffmpeg-backed registration."""
    from palette_app import qs_remote

    fetched = []

    def fake_fetch(filename, dest, timeout=600.0):
        fetched.append(filename)
        if filename.endswith(".words.json"):
            dest.write_text(json.dumps({"words": [{"word": "hi", "start": 0.0,
                                                   "end": 0.1}]}), encoding="utf-8")
        else:
            dest.write_bytes(b"REMOTE-AUDIO")
        return True

    monkeypatch.setattr(qs_remote, "fetch_file", fake_fetch)

    async def fake_register(root, filename, title, url=None):
        from palette_app.library import load_library, new_item, save_library

        item = new_item(filename, title, url, 1.0, None)
        lib = load_library(root)
        lib["items"].append(item)
        save_library(root, lib)
        return item

    monkeypatch.setattr("palette_app.library.register_media_file", fake_register)
    return fetched


def adopt(root, remote_item, **kw):
    import asyncio

    from palette_app.qs_remote import adopt_remote_item

    return asyncio.run(adopt_remote_item(root, remote_item, **kw))


def test_adopt_applies_palette_by_name_not_remote_id(library, stub_transfer):
    """Palettes are stored as ids; the remote's ids mean nothing here."""
    from conftest import read_library

    item = adopt(library, {"filename": "clip.m4a", "title": "a quote",
                           "palettes": ["REMOTE-PALETTE-ID"],
                           "attribution": {"person": "Jordan Peterson"}},
                 palette_name="Narration", person="Jordan Peterson")

    lib = read_library(library)
    names = [p["name"] for p in lib["palettes"]]
    assert names == ["Narration"], "palette should be created locally by name"

    local_id = lib["palettes"][0]["id"]
    assert item["palettes"] == [local_id]
    assert "REMOTE-PALETTE-ID" not in item["palettes"], \
        "a remote palette id points at nothing in this library"


def test_adopt_carries_attribution_and_tags(library, stub_transfer):
    item = adopt(library, {"filename": "clip.m4a", "title": "a quote",
                           "attribution": {"person": "Jordan Peterson",
                                           "episode_id": "ABC"}},
                 person="Jordan Peterson", kind="cut")

    assert item["attribution"]["episode_id"] == "ABC", "attribution travels verbatim"
    assert set(item["tags"]) == {"quotesource", "word-cut", "Jordan Peterson"}


def test_adopt_always_refetches_the_clip(library, stub_transfer):
    """A name collision must not leave stale audio beside a fresh manifest."""
    (library / "media" / "clip.m4a").write_bytes(b"STALE-LOCAL-AUDIO")

    adopt(library, {"filename": "clip.m4a", "title": "q"}, kind="cut")

    assert (library / "media" / "clip.m4a").read_bytes() == b"REMOTE-AUDIO", \
        "kept stale audio; word timings would describe a different clip"


def test_adopt_fetches_the_sidecar_for_a_cut(library, stub_transfer):
    adopt(library, {"filename": "clip.m4a", "title": "q"}, kind="cut")

    assert "clip.words.json" in stub_transfer
    assert (library / "media" / "clip.words.json").exists()


def test_adopt_fails_loudly_when_a_cut_has_no_manifest(library, monkeypatch):
    """Silently accepting a clip with no word timings was the original bug."""
    from palette_app import qs_remote

    def only_audio(filename, dest, timeout=600.0):
        if filename.endswith(".words.json"):
            return False
        dest.write_bytes(b"AUDIO")
        return True

    monkeypatch.setattr(qs_remote, "fetch_file", only_audio)

    with pytest.raises(qs_remote.RemoteError) as e:
        adopt(library, {"filename": "clip.m4a", "title": "q"}, kind="cut")
    assert "manifest" in str(e.value).lower()


def test_adopt_tolerates_a_pull_without_a_manifest(library, monkeypatch):
    """A plain pull produces no sidecar; that is not an error."""
    from palette_app import qs_remote

    def only_audio(filename, dest, timeout=600.0):
        if filename.endswith(".words.json"):
            return False
        dest.write_bytes(b"AUDIO")
        return True

    monkeypatch.setattr(qs_remote, "fetch_file", only_audio)

    async def fake_register(root, filename, title, url=None):
        from palette_app.library import load_library, new_item, save_library

        item = new_item(filename, title, url, 1.0, None)
        lib = load_library(root)
        lib["items"].append(item)
        save_library(root, lib)
        return item

    monkeypatch.setattr("palette_app.library.register_media_file", fake_register)

    item = adopt(library, {"filename": "clip.m4a", "title": "q"}, kind="pull")
    assert item["filename"] == "clip.m4a"
    assert "word-cut" not in item["tags"], "word-cut is a cut-only tag"
