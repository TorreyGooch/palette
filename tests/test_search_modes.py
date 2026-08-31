"""A GPU fault must not take out keyword search, which never touches the GPU.

Semantic search needs CUDA and the embedding model; `mode=grep` is FTS5 over
SQLite on the CPU. When cuBLAS failed to initialise on the corpus server,
every semantic query 500'd with a CUDA error - and a session reading that
concluded the corpus was unreachable and stopped, with a working search path
one parameter away.

The capability was there the whole time. What was missing was any way to
learn that from the failure.
"""
import pytest
from fastapi import HTTPException


@pytest.fixture
def local(monkeypatch):
    """Answer from this machine's corpus rather than forwarding."""
    monkeypatch.delenv("QS_REMOTE", raising=False)
    monkeypatch.setattr("palette_app.main._remote", lambda: None)


def exploding_gpu(*a, **k):
    raise RuntimeError("cuBLAS init failed: CUBLAS_STATUS_NOT_INITIALIZED")


def test_keyword_search_survives_a_dead_gpu(local, monkeypatch):
    """The invariant: these two failure domains are separate, so keep them so."""
    from palette_app import main

    monkeypatch.setattr("quotesource.embedder.embed_stats", exploding_gpu)
    monkeypatch.setattr("quotesource.embedder.semantic_search", exploding_gpu)
    monkeypatch.setattr("quotesource.search.grep",
                        lambda q, **k: [{"text": "a hit", "episode_id": "EP"}])

    out = main.qs_search("the meaning crisis", mode="grep")

    assert out["mode"] == "grep"
    assert out["hits"] == [{"text": "a hit", "episode_id": "EP"}]


def test_a_semantic_failure_names_the_path_that_still_works(local, monkeypatch):
    from palette_app import main

    monkeypatch.setattr("quotesource.embedder.embed_stats",
                        lambda: {"embedded": 5, "coverage": 1.0})
    monkeypatch.setattr("quotesource.embedder.semantic_search", exploding_gpu)

    with pytest.raises(HTTPException) as raised:
        main.qs_search("the meaning crisis")

    detail = raised.value.detail
    assert "mode=grep" in detail, "it must say what to try instead"
    assert "cuBLAS" in detail, "and must not swallow the actual cause"


def test_a_keyword_failure_does_not_recommend_itself(local, monkeypatch):
    """Suggesting the mode that just failed would be worse than saying nothing."""
    from palette_app import main

    monkeypatch.setattr("quotesource.search.grep", exploding_gpu)

    with pytest.raises(HTTPException) as raised:
        main.qs_search("the meaning crisis", mode="grep")

    assert "mode=grep" not in raised.value.detail


def test_the_missing_embeddings_refusal_is_left_alone(local, monkeypatch):
    """An empty index is a different problem, and already says the right thing."""
    from palette_app import main

    monkeypatch.setattr("quotesource.embedder.embed_stats",
                        lambda: {"embedded": 0, "coverage": 0.0})

    with pytest.raises(HTTPException) as raised:
        main.qs_search("the meaning crisis")

    assert raised.value.status_code == 409
    assert "qs embed" in raised.value.detail
