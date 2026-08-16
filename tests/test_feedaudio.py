"""Fetching podcast audio, and lending it to a captioned episode.

No network and no ffmpeg: the download is exercised against a stubbed opener,
and the linking against real files on disk, because what matters is which
files end up where and what the metadata then claims about them.
"""
import io
import json

import pytest

from quotesource import feedaudio


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    monkeypatch.setattr(feedaudio, "data_root", lambda: tmp_path)
    (tmp_path / "episodes").mkdir()
    return tmp_path


def add_episode(corpus, source, ep_id, *, title="Ep", duration=1000,
                audio_url=None, audio_bytes=None, ext=".mp3"):
    d = corpus / "episodes" / source / ep_id
    d.mkdir(parents=True)
    meta = {"episode_id": ep_id, "title": title, "duration": duration}
    if audio_url:
        meta["audio_url"] = audio_url
    (d / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")
    if audio_bytes is not None:
        (d / f"audio{ext}").write_bytes(audio_bytes)
    return d


# ── choosing a container ──────────────────────────────────────────────────────

@pytest.mark.parametrize("ctype,expected", [
    ("audio/mpeg", ".mp3"),
    ("audio/mp4", ".m4a"),
    ("audio/x-m4a", ".m4a"),
    ("audio/ogg", ".ogg"),
    ("audio/mpeg; charset=binary", ".mp3"),
])
def test_content_type_decides_the_extension(ctype, expected):
    assert feedaudio.extension_for("https://x/y", ctype) == expected


def test_url_is_the_fallback_when_the_type_is_useless():
    assert feedaudio.extension_for("https://x/ep12.m4a", "application/octet-stream") == ".m4a"


def test_unknown_everything_defaults_to_mp3():
    assert feedaudio.extension_for("https://x/download?id=9", None) == ".mp3"


# ── finding what is already there ─────────────────────────────────────────────

def test_stored_audio_accepts_any_container(corpus):
    for ext in (".m4a", ".opus", ".ogg", ".mp3"):
        d = add_episode(corpus, "s", f"ep{ext}", audio_bytes=b"x", ext=ext)
        assert feedaudio.stored_audio(d) is not None


def test_stored_audio_ignores_unrelated_files(corpus):
    d = add_episode(corpus, "s", "ep1")
    (d / "audio.txt").write_bytes(b"not audio")
    (d / "transcript.json").write_bytes(b"{}")
    assert feedaudio.stored_audio(d) is None


def test_a_partial_download_does_not_count_as_audio(corpus):
    d = add_episode(corpus, "s", "ep1")
    (d / "audio.mp3.part").write_bytes(b"half")
    assert feedaudio.stored_audio(d) is None


# ── fetching ──────────────────────────────────────────────────────────────────

class FakeResponse(io.BytesIO):
    def __init__(self, data, ctype="audio/mpeg", length=None, url="https://x/a.mp3"):
        super().__init__(data)
        self.headers = {"Content-Length": str(length if length is not None
                                              else len(data)),
                        "Content-Type": ctype}
        self.url = url

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def test_fetch_writes_audio_and_reports_bytes(corpus, monkeypatch):
    add_episode(corpus, "feed", "e1", audio_url="https://x/a.mp3")
    monkeypatch.setattr(feedaudio.urllib.request, "urlopen",
                        lambda *a, **k: FakeResponse(b"\xff" * 2048))

    result = feedaudio.fetch_source_audio("feed", sleep_s=0)

    assert result["fetched"] == 1 and result["failed"] == 0
    assert result["bytes"] == 2048
    assert (corpus / "episodes" / "feed" / "e1" / "audio.mp3").exists()


def test_episodes_with_audio_are_skipped(corpus, monkeypatch):
    add_episode(corpus, "feed", "e1", audio_url="https://x/a.mp3",
                audio_bytes=b"already")
    monkeypatch.setattr(feedaudio.urllib.request, "urlopen",
                        lambda *a, **k: pytest.fail("should not re-download"))

    assert feedaudio.fetch_source_audio("feed", sleep_s=0)["fetched"] == 0


def test_a_truncated_download_is_discarded_not_kept(corpus, monkeypatch):
    """A short file that kept its name would look complete forever, and every
    later cut would read a mangled episode."""
    add_episode(corpus, "feed", "e1", audio_url="https://x/a.mp3")
    monkeypatch.setattr(feedaudio.urllib.request, "urlopen",
                        lambda *a, **k: FakeResponse(b"tiny", length=999999))

    result = feedaudio.fetch_source_audio("feed", sleep_s=0)

    assert result["failed"] == 1 and result["fetched"] == 0
    ep = corpus / "episodes" / "feed" / "e1"
    assert feedaudio.stored_audio(ep) is None
    assert not list(ep.glob("*.part")), "the partial file must not linger"


def test_one_failure_does_not_stop_the_run(corpus, monkeypatch):
    add_episode(corpus, "feed", "e1", audio_url="https://x/bad.mp3")
    add_episode(corpus, "feed", "e2", audio_url="https://x/good.mp3")

    def opener(req, *a, **k):
        if "bad" in req.full_url:
            raise OSError("connection reset")
        return FakeResponse(b"\x00" * 64)

    monkeypatch.setattr(feedaudio.urllib.request, "urlopen", opener)
    result = feedaudio.fetch_source_audio("feed", sleep_s=0)

    assert result["fetched"] == 1 and result["failed"] == 1
    assert result["failures"][0]["episode_id"] == "e1"


def test_limit_caps_the_run(corpus, monkeypatch):
    for i in range(5):
        add_episode(corpus, "feed", f"e{i}", audio_url=f"https://x/{i}.mp3")
    monkeypatch.setattr(feedaudio.urllib.request, "urlopen",
                        lambda *a, **k: FakeResponse(b"\x00" * 16))

    assert feedaudio.fetch_source_audio("feed", limit=2, sleep_s=0)["fetched"] == 2


# ── lending audio to a captioned episode ──────────────────────────────────────

def test_equal_durations_are_linked(corpus):
    add_episode(corpus, "yt", "v1", title="Ada Palmer on Machiavelli", duration=3600)
    add_episode(corpus, "feed", "r1", title="Ada Palmer on Machiavelli",
                duration=3600, audio_bytes=b"audio")

    result = feedaudio.link_matching("yt", "feed", apply=True)

    assert result["linked"] == 1
    linked = corpus / "episodes" / "yt" / "v1" / "audio.mp3"
    assert linked.exists() and linked.read_bytes() == b"audio"


def test_linking_records_where_the_audio_came_from(corpus):
    """The clip's transcript and its audio now have different origins, and a
    manifest that cannot say so is not auditable."""
    add_episode(corpus, "yt", "v1", title="Same Show", duration=3600)
    add_episode(corpus, "feed", "r1", title="Same Show", duration=3600,
                audio_bytes=b"a")

    feedaudio.link_matching("yt", "feed", apply=True)

    meta = json.loads((corpus / "episodes" / "yt" / "v1" / "metadata.json")
                      .read_text(encoding="utf-8"))
    prov = meta["audio_provenance"]
    assert prov["linked_from"] == "feed/r1"
    assert prov["offset_s"] == 0.0
    assert prov["alignment"] == "duration_exact"


def test_differing_durations_are_left_alone(corpus):
    """A feed with a pre-roll the upload lacks reports a longer episode, and
    every caption timestamp would be wrong by that much."""
    add_episode(corpus, "yt", "v1", title="Same Show", duration=3600)
    add_episode(corpus, "feed", "r1", title="Same Show", duration=3645,
                audio_bytes=b"a")

    result = feedaudio.link_matching("yt", "feed", apply=True)

    assert result["linked"] == 0 and result["differs"] == 1
    assert feedaudio.stored_audio(corpus / "episodes" / "yt" / "v1") is None


def test_one_second_rounding_still_links(corpus):
    """Both platforms report whole seconds and round differently."""
    add_episode(corpus, "yt", "v1", title="Same Show", duration=3600)
    add_episode(corpus, "feed", "r1", title="Same Show", duration=3601,
                audio_bytes=b"a")

    assert feedaudio.link_matching("yt", "feed", apply=True)["linked"] == 1


def test_unrelated_titles_are_not_paired(corpus):
    add_episode(corpus, "yt", "v1", title="Ada Palmer on Machiavelli", duration=3600)
    add_episode(corpus, "feed", "r1", title="Semiconductor supply chains",
                duration=3600, audio_bytes=b"a")

    result = feedaudio.link_matching("yt", "feed", apply=True)

    assert result["linked"] == 0 and result["unmatched"] == 1


def test_dry_run_changes_nothing(corpus):
    add_episode(corpus, "yt", "v1", title="Same Show", duration=3600)
    add_episode(corpus, "feed", "r1", title="Same Show", duration=3600,
                audio_bytes=b"a")

    result = feedaudio.link_matching("yt", "feed", apply=False)

    assert result["linkable"] == 1 and result["linked"] == 0
    assert feedaudio.stored_audio(corpus / "episodes" / "yt" / "v1") is None


def test_already_linked_episodes_are_not_redone(corpus):
    add_episode(corpus, "yt", "v1", title="Same Show", duration=3600,
                audio_bytes=b"already")
    add_episode(corpus, "feed", "r1", title="Same Show", duration=3600,
                audio_bytes=b"feed")

    result = feedaudio.link_matching("yt", "feed", apply=True)

    assert result["already"] == 1 and result["linked"] == 0
    assert (corpus / "episodes" / "yt" / "v1" / "audio.mp3").read_bytes() == b"already"


def test_the_link_costs_no_extra_disk(corpus):
    """Hardlink, not copy: the archive is tens of gigabytes."""
    add_episode(corpus, "yt", "v1", title="Same Show", duration=3600)
    feed_dir = add_episode(corpus, "feed", "r1", title="Same Show",
                           duration=3600, audio_bytes=b"x" * 4096)

    feedaudio.link_matching("yt", "feed", apply=True)

    src = feedaudio.stored_audio(feed_dir)
    dst = feedaudio.stored_audio(corpus / "episodes" / "yt" / "v1")
    assert src.stat().st_ino == dst.stat().st_ino


def test_a_measured_offset_is_recorded(corpus):
    """What the offset probe writes when it finds a constant pre-roll."""
    cap = add_episode(corpus, "yt", "v1", title="Same Show", duration=3600)
    feed = add_episode(corpus, "feed", "r1", title="Same Show", duration=3645,
                       audio_bytes=b"a")

    feedaudio.link(cap, feed, {"title": "Same Show"}, offset_s=45.0,
                   alignment="probed_constant", extra={"probe_fit": 0.91})

    prov = json.loads((cap / "metadata.json").read_text(encoding="utf-8"))["audio_provenance"]
    assert prov["offset_s"] == 45.0
    assert prov["alignment"] == "probed_constant"
    assert prov["probe_fit"] == 0.91
