"""The words endpoint.

Picking cut boundaries is the step CLAUDE.md warns against skipping, and it
was the one primitive with no HTTP route - so the desktop had to ssh over
for it while every other step went through the app.
"""
import pytest
from fastapi import HTTPException


def test_words_is_advertised_as_a_capability():
    from palette_app import main

    assert "words" in main.CAPABILITIES


def test_local_call_reaches_word_map(monkeypatch):
    from palette_app import main

    monkeypatch.delenv("QS_REMOTE", raising=False)
    seen = {}

    def fake_word_map(episode_id, start, end, pad=0.0, model_size=None):
        seen.update(episode_id=episode_id, start=start, end=end,
                    pad=pad, model_size=model_size)
        return {"words": [{"word": "away.", "start": 486.8, "gap_before": 0.2}],
                "pauses": [486.8]}

    monkeypatch.setattr("quotesource.cut.word_map", fake_word_map)

    out = main.qs_words("EP1", 477.0, 490.0, pad=1.5, model="small")
    assert out["pauses"] == [486.8]
    assert seen == {"episode_id": "EP1", "start": 477.0, "end": 490.0,
                    "pad": 1.5, "model_size": "small"}


def test_missing_episode_is_404(monkeypatch):
    from palette_app import main

    monkeypatch.delenv("QS_REMOTE", raising=False)

    def missing(*a, **k):
        raise FileNotFoundError("episode 'NOPE' not found")

    monkeypatch.setattr("quotesource.cut.word_map", missing)
    with pytest.raises(HTTPException) as e:
        main.qs_words("NOPE", 1.0, 2.0)
    assert e.value.status_code == 404


def test_remote_call_forwards_every_parameter(monkeypatch):
    from palette_app import main, qs_remote

    monkeypatch.setenv("QS_REMOTE", "http://server:7862")
    captured = {}

    def fake_get(path, params=None, timeout=None):
        captured.update(path=path, params=params, timeout=timeout)
        return {"words": []}

    monkeypatch.setattr(qs_remote, "get", fake_get)

    main.qs_words("EP1", 10.0, 20.0, pad=2.0, model="large-v3")
    assert captured["path"] == "/api/qs/words"
    assert captured["params"]["episode_id"] == "EP1"
    assert captured["params"]["model"] == "large-v3"
    # Whisper on a window is seconds, but not the default 120s worth of
    # seconds when the model has to load first.
    assert captured["timeout"] >= 300


def test_remote_failure_keeps_its_status(monkeypatch):
    from palette_app import main, qs_remote

    monkeypatch.setenv("QS_REMOTE", "http://server:7862")

    def refuse(*a, **k):
        raise qs_remote.RemoteError("remote 404: no such episode", 404)

    monkeypatch.setattr(qs_remote, "get", refuse)
    with pytest.raises(HTTPException) as e:
        main.qs_words("NOPE", 1.0, 2.0)
    assert e.value.status_code == 404


def _local(monkeypatch):
    from palette_app import main

    monkeypatch.delenv("QS_REMOTE", raising=False)
    monkeypatch.setattr(main, "_remote", lambda: None)
    return main


def _raising(monkeypatch, exc):
    main = _local(monkeypatch)

    def boom(*a, **k):
        raise exc

    monkeypatch.setattr("quotesource.cut.word_map", boom)
    return main


def test_unfetched_audio_says_a_pull_will_fix_it(monkeypatch):
    """Captions alone cannot give word timings, and the 500 never said so."""
    main = _raising(monkeypatch,
                    RuntimeError("could not obtain audio for mO9LUWs5M60"))

    with pytest.raises(HTTPException) as raised:
        main.qs_words("mO9LUWs5M60", 1988.0, 2016.0)

    assert raised.value.status_code == 502
    assert "could not obtain audio" in raised.value.detail
    assert "qs pull" in raised.value.detail


class DownloadError(Exception):
    """Stands in for yt_dlp.utils.DownloadError, which is what actually flew."""


def test_a_refused_download_is_not_a_retry(monkeypatch):
    """The bug: catching RuntimeError missed the exception actually raised.

    The fetch is yt-dlp, so a 403 arrives as DownloadError and sailed past,
    reaching the browser as a bare 500. Worse, the message it replaced told
    the reader to pull - which walks them straight into the same 403.
    """
    main = _raising(monkeypatch, DownloadError(
        "ERROR: unable to download video data: HTTP Error 403: Forbidden"))

    with pytest.raises(HTTPException) as raised:
        main.qs_words("mO9LUWs5M60", 1992.0, 2013.0)

    detail = raised.value.detail
    assert raised.value.status_code == 502
    assert "403" in detail, "the actual cause must survive"
    assert "wait, not a retry" in detail
    # It may mention a pull - to say it fails the same way. What it must not
    # do is recommend one, which is what the old message did.
    assert "will fail the same way" in detail
    assert "fetches and keeps it" not in detail


def test_a_refusal_warns_against_the_obvious_next_move(monkeypatch):
    """A 403 is exactly where someone reaches for cookies. Say not to, there."""
    main = _raising(monkeypatch, DownloadError("HTTP Error 403: Forbidden"))

    with pytest.raises(HTTPException) as raised:
        main.qs_words("EP", 0.0, 1.0)

    assert "cookies" in raised.value.detail
    assert "user agent" in raised.value.detail


@pytest.mark.parametrize("message", [
    "Video unavailable", "Private video", "Sign in to confirm you're not a bot",
    "This video has been removed by the uploader",
])
def test_other_refusals_read_the_same_way(monkeypatch, message):
    main = _raising(monkeypatch, DownloadError(message))

    with pytest.raises(HTTPException) as raised:
        main.qs_words("EP", 0.0, 1.0)
    assert "wait, not a retry" in raised.value.detail


def test_a_genuinely_absent_episode_is_still_a_404(monkeypatch):
    """Not in the corpus and not yet fetched are different answers."""
    main = _raising(monkeypatch, FileNotFoundError("episode 'NOPE' not found"))

    with pytest.raises(HTTPException) as raised:
        main.qs_words("NOPE", 0.0, 1.0)
    assert raised.value.status_code == 404


# ── what the failure does not block ───────────────────────────────────────────
#
# "word timings need this episode's audio" is true of this endpoint and reads
# as "this quote cannot be subdivided" - which is wrong, and wrong in the
# common case. Every `qs cut` clip carries a .words.json with per-word timings,
# so narrowing a beat to a shorter span is a local file read. Episode audio is
# only needed to cut something new.

@pytest.mark.parametrize("error", [
    RuntimeError("could not obtain audio for EP"),
    DownloadError("HTTP Error 403: Forbidden"),
])
def test_both_failures_say_an_existing_clip_can_still_be_resplit(monkeypatch,
                                                                 error):
    main = _raising(monkeypatch, error)

    with pytest.raises(HTTPException) as raised:
        main.qs_words("EP", 0.0, 1.0)

    detail = raised.value.detail
    assert "words.json" in detail
    assert "no audio at all" in detail


def test_resplitting_a_beat_really_does_avoid_audio_and_network(library,
                                                                monkeypatch):
    """The claim in that message, checked rather than asserted in prose."""
    from tests.test_narration import audio_item
    from palette_app import narration

    item = audio_item(library, "clip.m4a", "aud-1")
    (library / "media" / "clip.m4a").unlink()      # no audio present at all

    def explode(*a, **k):
        raise AssertionError("re-splitting must not reach the network")

    monkeypatch.setattr("quotesource.cut.word_map", explode)

    narrow = narration.bind(library / "media", item, 2, 4)

    assert narrow["precision"] == "word"
    assert narrow["word_count"] == 3
    assert narrow["duration"] > 0
