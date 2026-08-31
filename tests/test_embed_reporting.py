"""Status must describe the vectors on disk, not the calling shell.

Reporting the ambient QS_EMBED_MODEL made status claim bge-small while the
store held bge-large, which is actively misleading when deciding whether a
re-embed is needed.
"""
import pytest


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    """A minimal index db with one chunk and a recorded embedding model."""
    monkeypatch.setenv("QUOTESOURCE_DATA", str(tmp_path))
    from quotesource.embedder import EMBED_SCHEMA
    from quotesource.indexer import _ensure_schema, connect

    con = connect()
    _ensure_schema(con)
    con.executescript(EMBED_SCHEMA)
    con.execute("INSERT INTO episodes (episode_id, source_id, title, "
                "upload_date, url) VALUES ('EP', 'src', 't', '20260101', 'u')")
    con.execute("INSERT INTO chunks (episode_id, start, end, text) "
                "VALUES ('EP', 0.0, 1.0, 'hello')")
    cid = con.execute("SELECT id FROM chunks").fetchone()[0]
    con.execute("INSERT INTO embeddings (chunk_id, vector) VALUES (?, ?)",
                (cid, b"\x00" * 16))
    con.execute("INSERT OR REPLACE INTO embedding_meta VALUES ('model', ?)",
                ("BAAI/bge-large-en-v1.5",))
    con.commit()
    con.close()
    return tmp_path


def test_reports_the_stored_model_not_the_environment(corpus, monkeypatch):
    # Set explicitly: deleting the var would now leave the default agreeing
    # with the store, and the test would pass without proving anything.
    monkeypatch.setenv("QS_EMBED_MODEL", "BAAI/bge-small-en-v1.5")
    from quotesource.embedder import embed_stats

    stats = embed_stats()
    assert stats["model"] == "BAAI/bge-large-en-v1.5"
    assert stats["embedded"] == 1


def test_default_matches_the_corpus_that_exists(monkeypatch):
    """A default that disagrees with the stored vectors is not a fallback, it
    is an outage: search refuses to mix models, so anyone whose shell lacked
    QS_EMBED_MODEL got no search at all."""
    monkeypatch.delenv("QS_EMBED_MODEL", raising=False)
    from quotesource.embedder import DEFAULT_MODEL, model_name

    assert DEFAULT_MODEL == "BAAI/bge-large-en-v1.5"
    assert model_name() == DEFAULT_MODEL


def test_warns_when_the_environment_disagrees(corpus, monkeypatch):
    monkeypatch.setenv("QS_EMBED_MODEL", "BAAI/bge-small-en-v1.5")
    from quotesource.embedder import embed_stats

    stats = embed_stats()
    assert "model_mismatch" in stats
    assert "bge-large" in stats["model_mismatch"]


def test_no_warning_when_they_agree(corpus, monkeypatch):
    monkeypatch.setenv("QS_EMBED_MODEL", "BAAI/bge-large-en-v1.5")
    from quotesource.embedder import embed_stats

    assert "model_mismatch" not in embed_stats()


def test_falls_back_to_env_when_nothing_is_stored(tmp_path, monkeypatch):
    monkeypatch.setenv("QUOTESOURCE_DATA", str(tmp_path))
    monkeypatch.setenv("QS_EMBED_MODEL", "BAAI/bge-base-en-v1.5")
    from quotesource.embedder import embed_stats

    stats = embed_stats()
    assert stats["model"] == "BAAI/bge-base-en-v1.5"
    assert stats["embedded"] == 0


# ── coverage must be able to say "incomplete", which means it cannot exceed 1 ──

def orphan_a_vector(root):
    """Leave a vector whose chunk is gone, as a re-chunk or removal does.

    chunk_id declares a foreign key, but SQLite does not enforce one unless
    asked, so deleting the chunk leaves the row behind.
    """
    from quotesource.indexer import connect

    con = connect()
    con.execute("INSERT INTO embeddings (chunk_id, vector) VALUES (9999, ?)",
                (b"\x00" * 16,))
    con.commit()
    con.close()


def test_coverage_cannot_exceed_one(corpus):
    """A field that reads 1.0011 can no longer say results may be incomplete."""
    from quotesource.embedder import embed_stats

    orphan_a_vector(corpus)
    stats = embed_stats()

    assert stats["coverage"] <= 1.0
    assert stats["coverage"] == 1.0, "the one real chunk is embedded"


def test_orphaned_vectors_are_reported_as_their_own_number(corpus):
    from quotesource.embedder import embed_stats

    orphan_a_vector(corpus)
    stats = embed_stats()

    assert stats["orphan_vectors"] == 1
    assert stats["chunks"] == 1
    assert stats["embedded"] == 1, "embedded counts chunks, not vector rows"


def test_a_clean_store_does_not_mention_orphans(corpus):
    """A line that is always present and always zero stops being read."""
    from quotesource.embedder import embed_stats

    assert "orphan_vectors" not in embed_stats()


def test_an_unembedded_chunk_still_reads_as_incomplete(corpus):
    """The case coverage exists for must survive the fix."""
    from quotesource.embedder import embed_stats
    from quotesource.indexer import connect

    con = connect()
    con.execute("INSERT INTO chunks (episode_id, start, end, text) "
                "VALUES ('EP', 1.0, 2.0, 'second')")
    con.commit()
    con.close()
    orphan_a_vector(corpus)

    stats = embed_stats()
    assert stats["coverage"] == 0.5
    assert stats["orphan_vectors"] == 1
