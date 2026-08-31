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


def test_missing_episode_audio_explains_itself(monkeypatch):
    """Captions alone cannot give word timings, and the 500 never said so.

    `context` on the same episode answers fine from the transcript on disk, so
    a bare CUDA-shaped error sent a session looking at the model and the
    server when the actual gap was one absent audio file.
    """
    from palette_app import main

    monkeypatch.delenv("QS_REMOTE", raising=False)
    monkeypatch.setattr(main, "_remote", lambda: None)

    def no_audio(*a, **k):
        raise RuntimeError("could not obtain audio for mO9LUWs5M60")

    monkeypatch.setattr("quotesource.cut.word_map", no_audio)

    with pytest.raises(HTTPException) as raised:
        main.qs_words("mO9LUWs5M60", 1988.0, 2016.0)

    assert raised.value.status_code == 502
    assert "could not obtain audio" in raised.value.detail
    assert "qs pull" in raised.value.detail, "it must say what to do"


def test_a_genuinely_absent_episode_is_still_a_404(monkeypatch):
    """Not in the corpus and no audio yet are different answers."""
    from palette_app import main

    monkeypatch.delenv("QS_REMOTE", raising=False)
    monkeypatch.setattr(main, "_remote", lambda: None)

    def missing(*a, **k):
        raise FileNotFoundError("episode 'NOPE' not found")

    monkeypatch.setattr("quotesource.cut.word_map", missing)

    with pytest.raises(HTTPException) as raised:
        main.qs_words("NOPE", 0.0, 1.0)
    assert raised.value.status_code == 404
