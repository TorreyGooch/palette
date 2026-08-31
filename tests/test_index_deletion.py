"""Removing an episode has to empty three tables, not one.

A chunk is referenced from `chunks_fts` (external-content FTS5, which keeps
its own term index and must be told separately) and from `embeddings` (keyed
on chunk_id, with a foreign key SQLite does not enforce unless asked). Deleting
the chunk row alone leaves both behind.

That is where the orphaned vectors came from. Re-indexing an episode whose
transcript changed - a whisper backfill - minted new chunk ids and stranded
every old one, which pushed reported coverage to 1.0011 on the live corpus:
a number that can exceed 1.0 can no longer say "results may be incomplete".
"""
import pytest

from quotesource.embedder import EMBED_SCHEMA
from quotesource.indexer import _ensure_schema, connect, forget_episode


@pytest.fixture
def index(tmp_path, monkeypatch):
    """One episode, two chunks, both embedded and both in the FTS table."""
    monkeypatch.setenv("QUOTESOURCE_DATA", str(tmp_path))
    con = connect()
    _ensure_schema(con)
    con.executescript(EMBED_SCHEMA)
    con.execute("INSERT INTO episodes (episode_id, source_id, title, "
                "upload_date, url) VALUES ('EP', 'src', 't', '20260101', 'u')")
    for text in ("lobsters have serotonin", "and antidepressants work"):
        cur = con.execute("INSERT INTO chunks (episode_id, start, end, text) "
                          "VALUES ('EP', 0.0, 1.0, ?)", (text,))
        con.execute("INSERT INTO chunks_fts(rowid, text) VALUES (?,?)",
                    (cur.lastrowid, text))
        con.execute("INSERT INTO embeddings (chunk_id, vector) VALUES (?,?)",
                    (cur.lastrowid, b"\x00" * 16))
    con.commit()
    yield con
    con.close()


def counts(con):
    return {
        "chunks": con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0],
        "episodes": con.execute("SELECT COUNT(*) FROM episodes").fetchone()[0],
        "vectors": con.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0],
        "fts": con.execute("SELECT COUNT(*) FROM chunks_fts "
                           "WHERE chunks_fts MATCH 'lobsters'").fetchone()[0],
    }


def test_the_fixture_is_actually_populated(index):
    """Otherwise every assertion below passes for the wrong reason."""
    assert counts(index) == {"chunks": 2, "episodes": 1, "vectors": 2, "fts": 1}


def test_forgetting_an_episode_empties_all_three_tables(index):
    dropped = forget_episode(index, "EP")

    assert dropped == 2
    assert counts(index) == {"chunks": 0, "episodes": 0, "vectors": 0, "fts": 0}


def test_no_vector_outlives_its_chunk(index):
    """The invariant coverage depends on: it cannot exceed 1.0 if this holds."""
    forget_episode(index, "EP")

    orphans = index.execute(
        "SELECT COUNT(*) FROM embeddings e "
        "LEFT JOIN chunks c ON c.id = e.chunk_id WHERE c.id IS NULL"
    ).fetchone()[0]
    assert orphans == 0


def test_another_episode_is_untouched(index):
    cur = index.execute("INSERT INTO episodes (episode_id, source_id, title, "
                        "upload_date, url) VALUES ('OTHER','src','t','x','u')")
    cur = index.execute("INSERT INTO chunks (episode_id, start, end, text) "
                        "VALUES ('OTHER', 0.0, 1.0, 'kept')")
    index.execute("INSERT INTO embeddings (chunk_id, vector) VALUES (?,?)",
                  (cur.lastrowid, b"\x00" * 16))
    index.commit()

    forget_episode(index, "EP")

    assert counts(index)["chunks"] == 1
    assert counts(index)["vectors"] == 1


def test_forgetting_an_episode_that_is_not_there_is_not_an_error(index):
    assert forget_episode(index, "NOPE") == 0
    assert counts(index)["chunks"] == 2


def test_it_works_on_an_index_that_was_never_embedded(tmp_path, monkeypatch):
    """`embeddings` belongs to the embedder and may simply not exist yet."""
    monkeypatch.setenv("QUOTESOURCE_DATA", str(tmp_path))
    con = connect()
    _ensure_schema(con)
    con.execute("INSERT INTO episodes (episode_id, source_id, title, "
                "upload_date, url) VALUES ('EP','src','t','x','u')")
    cur = con.execute("INSERT INTO chunks (episode_id, start, end, text) "
                      "VALUES ('EP', 0.0, 1.0, 'hello')")
    con.execute("INSERT INTO chunks_fts(rowid, text) VALUES (?,?)",
                (cur.lastrowid, "hello"))
    con.commit()

    assert forget_episode(con, "EP") == 1
    assert con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 0
    con.close()
