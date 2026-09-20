"""The whisper model cache lets go of the GPU.

Measured on the server: one `qs words` call left the corpus app holding
3,930 MiB of a 12 GB card for 4 days and 20 hours, because `_model_cache`
had no eviction at all. The embedding model has had one for months; this is
the same rule for the same card.

No model is ever built here: faster_whisper is replaced by a stand-in, which
is also the only way this can run in CI at all.
"""
import sys
import types

import pytest

from quotesource import transcribe


class FakeWhisper:
    built = 0

    def __init__(self, model, device=None, compute_type=None):
        FakeWhisper.built += 1
        self.model, self.device, self.compute_type = model, device, compute_type


@pytest.fixture(autouse=True)
def fake_whisper(monkeypatch):
    """A stand-in for faster_whisper, and an empty cache for every test."""
    FakeWhisper.built = 0
    module = types.ModuleType("faster_whisper")
    module.WhisperModel = FakeWhisper
    monkeypatch.setitem(sys.modules, "faster_whisper", module)
    monkeypatch.setattr(transcribe, "_model_cache", {})
    monkeypatch.setattr(transcribe, "_used", {})
    monkeypatch.setattr(transcribe, "_evictor", None)
    monkeypatch.setenv("QS_WHISPER_DEVICE", "cpu")
    monkeypatch.setenv("QS_WHISPER_MODEL", "small")
    return FakeWhisper


# -- caching, which is why this exists at all ---------------------------------

def test_a_second_call_reuses_the_model(monkeypatch):
    """Building costs seconds, and a cut is interactive."""
    monkeypatch.setattr(transcribe, "_IDLE_S", 600.0)

    first, key = transcribe._get_model()
    again, same_key = transcribe._get_model()

    assert first is again and key == same_key
    assert FakeWhisper.built == 1


def test_a_different_model_size_is_cached_separately(monkeypatch):
    monkeypatch.setattr(transcribe, "_IDLE_S", 600.0)

    transcribe._get_model()
    transcribe._get_model("large-v3")

    assert FakeWhisper.built == 2
    assert len(transcribe._model_cache) == 2


# -- letting go ----------------------------------------------------------------

def test_an_idle_model_is_dropped(monkeypatch):
    """The bug: nothing ever removed an entry from this dict."""
    monkeypatch.setattr(transcribe, "_IDLE_S", 600.0)
    model, key = transcribe._get_model()
    last_used = transcribe._used[key]

    dropped = transcribe._drop_idle(last_used + 601)

    assert dropped == [key]
    assert transcribe._model_cache == {} and transcribe._used == {}


def test_a_model_still_in_use_is_kept(monkeypatch):
    monkeypatch.setattr(transcribe, "_IDLE_S", 600.0)
    _, key = transcribe._get_model()
    last_used = transcribe._used[key]

    assert transcribe._drop_idle(last_used + 599) == []
    assert key in transcribe._model_cache


def test_using_a_model_postpones_its_eviction(monkeypatch):
    """Otherwise a long transcription batch would drop the model mid-run."""
    monkeypatch.setattr(transcribe, "_IDLE_S", 600.0)
    clock = [1000.0]
    monkeypatch.setattr(transcribe.time, "monotonic", lambda: clock[0])

    _, key = transcribe._get_model()
    clock[0] = 1500.0
    transcribe._get_model()

    assert transcribe._drop_idle(1600.0) == [], "500s since the last use"
    assert transcribe._drop_idle(2101.0) == [key]


def test_the_next_call_builds_a_fresh_model(monkeypatch):
    monkeypatch.setattr(transcribe, "_IDLE_S", 600.0)
    _, key = transcribe._get_model()
    transcribe._drop_idle(transcribe._used[key] + 601)

    transcribe._get_model()

    assert FakeWhisper.built == 2


def test_release_models_frees_everything_now(monkeypatch):
    monkeypatch.setattr(transcribe, "_IDLE_S", 600.0)
    transcribe._get_model()
    transcribe._get_model("large-v3")

    transcribe.release_models()

    assert transcribe._model_cache == {} and transcribe._used == {}


# -- the settings ----------------------------------------------------------------

def test_zero_keeps_the_model_forever(monkeypatch):
    """What a machine doing nothing but a transcription backfill wants."""
    monkeypatch.setattr(transcribe, "_IDLE_S", 0.0)
    _, key = transcribe._get_model()

    assert transcribe._drop_idle(transcribe._used[key] + 10_000) == [key], \
        "_drop_idle is unconditional; the thread is what never starts"
    assert transcribe._evictor is None


def test_a_negative_setting_disables_caching(monkeypatch):
    monkeypatch.setattr(transcribe, "_IDLE_S", -1.0)

    first, _ = transcribe._get_model()
    again, _ = transcribe._get_model()

    assert first is not again
    assert FakeWhisper.built == 2
    assert transcribe._model_cache == {}, "nothing is held to leak"


def test_the_evictor_stops_once_the_cache_is_empty(monkeypatch):
    """It must not spin forever on an idle server."""
    monkeypatch.setattr(transcribe, "_IDLE_S", 600.0)
    monkeypatch.setattr(transcribe, "_evictor", object())
    monkeypatch.setattr(transcribe.time, "sleep",
                        lambda _: pytest.fail("should not wait on an empty cache"))

    transcribe._evict_when_idle()

    assert transcribe._evictor is None


def test_a_missing_faster_whisper_still_says_how_to_install_it(monkeypatch):
    monkeypatch.setitem(sys.modules, "faster_whisper", None)
    monkeypatch.setattr(transcribe, "_IDLE_S", 600.0)

    with pytest.raises(RuntimeError, match="pip install faster-whisper"):
        transcribe._get_model()
