"""Embedding layer: local ONNX embeddings (fastembed) over index chunks.

Vectors live in the same SQLite db as the FTS index, one float32 blob per
chunk. Search is exact brute-force cosine via numpy — at low-hundreds of
episodes (a few hundred thousand chunks) this stays well under a second,
and there is no ANN index to corrupt or rebuild.

`qs embed` is resumable: only chunks without vectors are embedded, so an
interrupted run continues where it stopped, and `qs index` re-chunking an
episode (e.g. whisper replacing captions) naturally queues its new chunks.
"""
import os
import sqlite3
from datetime import datetime

from .indexer import connect, _ensure_schema

DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"


def model_name() -> str:
    return os.environ.get("QS_EMBED_MODEL", DEFAULT_MODEL)


EMBED_SCHEMA = """
CREATE TABLE IF NOT EXISTS embeddings (
    chunk_id INTEGER PRIMARY KEY REFERENCES chunks(id),
    vector BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS embedding_meta (
    key TEXT PRIMARY KEY, value TEXT
);
"""


def _ensure_embed_schema(con: sqlite3.Connection):
    con.executescript(EMBED_SCHEMA)
    row = con.execute("SELECT value FROM embedding_meta WHERE key='model'").fetchone()
    if row and row[0] != model_name():
        raise RuntimeError(
            f"index was embedded with model '{row[0]}' but current model is "
            f"'{model_name()}'. Re-embed with: qs embed --reset"
        )
    con.execute(
        "INSERT OR REPLACE INTO embedding_meta VALUES ('model', ?)", (model_name(),))
    con.commit()


def _get_model():
    from fastembed import TextEmbedding

    # GPU path: pip install fastembed-gpu (onnxruntime-gpu). Auto-detected;
    # falls back to CPU silently if CUDA providers aren't available.
    try:
        return TextEmbedding(model_name(), providers=[
            "CUDAExecutionProvider", "CPUExecutionProvider"])
    except Exception:
        return TextEmbedding(model_name())


def stored_model(con: sqlite3.Connection) -> str | None:
    """The model the existing vectors were built with, or None if unembedded."""
    row = con.execute("SELECT value FROM embedding_meta WHERE key='model'").fetchone()
    return row[0] if row else None


def embed_stats() -> dict:
    con = connect()
    _ensure_schema(con)
    con.executescript(EMBED_SCHEMA)
    total = con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    done = con.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
    # Report what the vectors actually are, not what this shell happens to be
    # configured for — otherwise status misreports whenever QS_EMBED_MODEL is
    # unset in the calling environment but the store was built with another.
    stored = stored_model(con)
    con.close()
    stats = {"chunks": total, "embedded": done,
             "coverage": round(done / total, 4) if total else 0.0,
             "model": stored or model_name()}
    if stored and stored != model_name():
        stats["model_mismatch"] = (
            f"vectors are '{stored}' but QS_EMBED_MODEL is '{model_name()}'; "
            f"search will refuse until they agree")
    return stats


def embed_pending(batch_size: int = 256, limit: int | None = None,
                  reset: bool = False, quiet: bool = False) -> dict:
    import numpy as np

    con = connect()
    _ensure_schema(con)
    if reset:
        con.executescript(EMBED_SCHEMA)
        con.execute("DELETE FROM embeddings")
        con.execute("DELETE FROM embedding_meta")
        con.commit()
    _ensure_embed_schema(con)

    pending = con.execute("""
        SELECT c.id, c.text FROM chunks c
        LEFT JOIN embeddings e ON e.chunk_id = c.id
        WHERE e.chunk_id IS NULL
        ORDER BY c.id
    """).fetchall()
    if limit is not None:
        pending = pending[:limit]

    if not pending:
        con.close()
        return {**embed_stats(), "newly_embedded": 0}

    if not quiet:
        print(f"embedding {len(pending)} chunks with {model_name()}…", flush=True)

    model = _get_model()
    done = 0
    t0 = datetime.now()
    for i in range(0, len(pending), batch_size):
        batch = pending[i:i + batch_size]
        texts = [t for _, t in batch]
        vectors = list(model.embed(texts, batch_size=batch_size))
        rows = [(cid, np.asarray(v, dtype=np.float32).tobytes())
                for (cid, _), v in zip(batch, vectors)]
        con.executemany("INSERT OR REPLACE INTO embeddings VALUES (?,?)", rows)
        con.commit()
        done += len(batch)
        if not quiet and (i // batch_size) % 20 == 0:
            rate = done / max((datetime.now() - t0).total_seconds(), 1)
            remain = (len(pending) - done) / max(rate, 1)
            print(f"  {done}/{len(pending)}  ({rate:.0f}/s, ~{remain/60:.0f} min left)",
                  flush=True)

    con.close()
    return {**embed_stats(), "newly_embedded": done}


def semantic_search(query: str, source=None, person=None, after=None,
                    before=None, limit: int = 20) -> list[dict]:
    import numpy as np

    from .search import _shared_filters, _hit

    con = connect()
    _ensure_schema(con)
    _ensure_embed_schema(con)

    clauses, params = _shared_filters(source, person, after, before)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = con.execute(f"""
        SELECT e2.chunk_id, e2.vector FROM embeddings e2
        JOIN chunks c ON c.id = e2.chunk_id
        JOIN episodes e ON e.episode_id = c.episode_id
        {where}
    """, params).fetchall()
    if not rows:
        con.close()
        return []

    ids = np.array([r[0] for r in rows], dtype=np.int64)
    mat = np.frombuffer(b"".join(r[1] for r in rows), dtype=np.float32)
    mat = mat.reshape(len(rows), -1)

    model = _get_model()
    qv = np.asarray(next(iter(model.query_embed(query))), dtype=np.float32)

    # cosine similarity (bge vectors are normalized; normalize defensively)
    qv = qv / (np.linalg.norm(qv) or 1.0)
    norms = np.linalg.norm(mat, axis=1)
    norms[norms == 0] = 1.0
    sims = (mat @ qv) / norms

    top = np.argsort(-sims)[:limit]
    top_ids = [(int(ids[i]), float(sims[i])) for i in top]

    hits = []
    for cid, score in top_ids:
        row = con.execute("""
            SELECT c.episode_id, e.source_id, c.start, c.end, c.text, ?,
                   e.title, e.upload_date, e.url
            FROM chunks c JOIN episodes e ON e.episode_id = c.episode_id
            WHERE c.id = ?
        """, (score, cid)).fetchone()
        if row:
            hits.append(_hit(row))
    con.close()
    return hits
