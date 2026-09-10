"""The first search after a reboot, and what it used to do.

One incident, two defects, different modules.

A semantic search on the server took 2 m 07 s and the caller got
`Internal Server Error`. Neither number was what it looked like. The card was
97% full at the time and the obvious reading — the GPU is out of memory — was
wrong: the model loaded into what was left and answered in 6.2 s on the retry.

What actually happened was that `fastembed` caches its ONNX model under
`tempfile.gettempdir()/fastembed_cache`, which on Linux is `/tmp`, which does
not survive a reboot. So the first search after one re-downloaded ~1.3 GB.
That is defect one, and it is a cache that does the work twice.

Defect two is what the caller saw. A *read* timeout escapes urllib unwrapped,
so neither of the bridge's two handlers caught it, and it left the function as
an unhandled exception that FastAPI rendered as a bare 500. A slow corpus
therefore reported as a broken one — and the request it gave up on went on to
succeed.
"""
import tempfile
import urllib.error
from pathlib import Path

import pytest


# ── where the model is kept ──────────────────────────────────────────────────

def clear(monkeypatch):
    for var in ("QS_MODEL_CACHE", "XDG_CACHE_HOME"):
        monkeypatch.delenv(var, raising=False)


def test_the_cache_is_not_in_the_temp_directory(monkeypatch):
    """The whole defect in one assertion: /tmp is cleared by a reboot."""
    from quotesource.embedder import model_cache_dir

    clear(monkeypatch)
    resolved = model_cache_dir().resolve()
    tmp = Path(tempfile.gettempdir()).resolve()

    assert not resolved.is_relative_to(tmp),         f"{resolved} is under {tmp}, which a reboot empties"


def test_an_explicit_override_wins(monkeypatch, tmp_path):
    from quotesource.embedder import model_cache_dir

    clear(monkeypatch)
    monkeypatch.setenv("QS_MODEL_CACHE", str(tmp_path / "models"))

    assert model_cache_dir() == tmp_path / "models"


def test_xdg_is_honoured_where_it_is_set(monkeypatch, tmp_path):
    from quotesource.embedder import model_cache_dir

    clear(monkeypatch)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))

    assert model_cache_dir() == tmp_path / "quotesource" / "models"


def test_asking_where_it_is_does_not_create_it(monkeypatch, tmp_path):
    """Read paths never write — even the ones that only compute a path."""
    from quotesource.embedder import model_cache_dir

    clear(monkeypatch)
    monkeypatch.setenv("QS_MODEL_CACHE", str(tmp_path / "untouched"))

    model_cache_dir()

    assert not (tmp_path / "untouched").exists()


def test_building_the_model_passes_the_cache_dir_and_creates_it(
        monkeypatch, tmp_path):
    """The change that stops the re-download, proven at the call boundary.

    Without cache_dir, fastembed silently uses its own temp default and every
    assertion above becomes decorative.
    """
    import sys
    import types

    seen = {}

    class FakeEmbedding:
        def __init__(self, model_name, cache_dir=None, providers=None, **kw):
            seen["model"] = model_name
            seen["cache_dir"] = cache_dir
            seen["providers"] = providers

    fake = types.ModuleType("fastembed")
    fake.TextEmbedding = FakeEmbedding
    monkeypatch.setitem(sys.modules, "fastembed", fake)

    clear(monkeypatch)
    target = tmp_path / "models"
    monkeypatch.setenv("QS_MODEL_CACHE", str(target))

    from quotesource.embedder import _build_model

    _build_model()

    assert seen["cache_dir"] == str(target)
    assert target.is_dir(), "the download needs somewhere to land"


def test_the_gpu_preference_survives_the_change(monkeypatch, tmp_path):
    """Adding an argument is an easy way to drop one that was already there."""
    import sys
    import types

    seen = {}

    class FakeEmbedding:
        def __init__(self, model_name, cache_dir=None, providers=None, **kw):
            seen["providers"] = providers

    fake = types.ModuleType("fastembed")
    fake.TextEmbedding = FakeEmbedding
    monkeypatch.setitem(sys.modules, "fastembed", fake)

    clear(monkeypatch)
    monkeypatch.setenv("QS_MODEL_CACHE", str(tmp_path))

    from quotesource.embedder import _build_model

    _build_model()

    assert seen["providers"][0] == "CUDAExecutionProvider"


# ── what the caller is told when it is slow ──────────────────────────────────

@pytest.fixture
def bridged(monkeypatch):
    from palette_app import qs_remote

    monkeypatch.setenv("QS_REMOTE", "http://server:7862")
    return qs_remote


def raise_with(monkeypatch, qs_remote, exc):
    def boom(*a, **kw):
        raise exc

    monkeypatch.setattr(qs_remote.urllib.request, "urlopen", boom)


def test_a_read_timeout_is_reported_rather_than_escaping(monkeypatch, bridged):
    """The bug: this exception left the function uncaught and became a 500."""
    raise_with(monkeypatch, bridged, TimeoutError("timed out"))

    with pytest.raises(bridged.RemoteError) as caught:
        bridged.get("/api/qs/search", {"q": "lobster"})

    assert caught.value.status == 504, "a slow server is not an internal error"


def test_a_connect_timeout_is_not_reported_as_unreachable(monkeypatch, bridged):
    """It arrives wrapped, so it needs recognising separately or it reads as
    'cannot reach' — a different problem with different advice."""
    raise_with(monkeypatch, bridged,
               urllib.error.URLError(TimeoutError("timed out")))

    with pytest.raises(bridged.RemoteError) as caught:
        bridged.get("/api/qs/search", {"q": "lobster"})

    assert caught.value.status == 504
    assert "cannot reach" not in str(caught.value)


def test_a_genuine_connection_failure_still_says_unreachable(monkeypatch, bridged):
    """The fix must not swallow the case that was already handled correctly."""
    raise_with(monkeypatch, bridged,
               urllib.error.URLError(ConnectionRefusedError("refused")))

    with pytest.raises(bridged.RemoteError) as caught:
        bridged.get("/api/qs/search", {"q": "lobster"})

    assert caught.value.status == 503
    assert "cannot reach" in str(caught.value)


def test_the_message_says_to_retry_rather_than_to_investigate(monkeypatch, bridged):
    """The honest reading of this timeout is 'still working', and the message
    has to carry that or the reader goes looking for a fault that is not
    there."""
    raise_with(monkeypatch, bridged, TimeoutError("timed out"))

    with pytest.raises(bridged.RemoteError) as caught:
        bridged.get("/api/qs/search", {"q": "lobster"})

    message = str(caught.value)
    assert "Retry" in message
    assert "QS_REMOTE_TIMEOUT" in message, "name the knob that moves the limit"
    assert "120" in message, "and the limit it actually waited"
