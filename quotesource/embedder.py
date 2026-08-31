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
import threading
import time
from datetime import datetime

from .indexer import connect, _ensure_schema

# The corpus is embedded with bge-large and search refuses to mix models, so a
# small default is not a gentler fallback — it is a broken one. This was set to
# bge-small while every stored vector was bge-large, which meant search failed
# for anyone whose shell did not happen to export QS_EMBED_MODEL. Defaults have
# to describe the data that exists.
DEFAULT_MODEL = "BAAI/bge-large-en-v1.5"


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


def _build_model():
    from fastembed import TextEmbedding

    # GPU path: pip install fastembed-gpu (onnxruntime-gpu). Auto-detected;
    # falls back to CPU silently if CUDA providers aren't available.
    try:
        return TextEmbedding(model_name(), providers=[
            "CUDAExecutionProvider", "CPUExecutionProvider"])
    except Exception:
        return TextEmbedding(model_name())


# Constructing the ONNX session costs ~0.9s; embedding a query with one
# already built costs ~6ms. A CLI process paid that once and exited, so it
# never mattered. A long-lived server paid it on every single search.
#
# Held only while searches keep arriving: this box shares its memory with
# ComfyUI, and a model kept resident through an idle night is memory taken
# from generation for nothing. QS_MODEL_IDLE_S=0 keeps it forever, -1
# disables caching entirely.
_MODEL_IDLE_S = float(os.environ.get("QS_MODEL_IDLE_S", "600"))

_model = None
_model_key = None
_model_used = 0.0
_model_lock = threading.Lock()
_evictor = None


def _evict_when_idle():
    """Drop the model once no search has wanted it for _MODEL_IDLE_S."""
    global _model, _model_key, _evictor

    while True:
        with _model_lock:
            if _model is None:
                _evictor = None
                return
            idle = time.monotonic() - _model_used
            if idle >= _MODEL_IDLE_S:
                _model = _model_key = None
                _evictor = None
                return
            wait = _MODEL_IDLE_S - idle
        time.sleep(min(wait, 30.0))


def _get_model():
    global _model, _model_key, _model_used, _evictor

    if _MODEL_IDLE_S < 0:
        return _build_model()

    key = model_name()
    with _model_lock:
        if _model is not None and _model_key == key:
            _model_used = time.monotonic()
            return _model

    # Built outside the lock: a first search should not block a second one
    # behind a slow session init, and building twice is merely wasteful.
    model = _build_model()

    with _model_lock:
        if _model is None or _model_key != key:
            _model, _model_key = model, key
        _model_used = time.monotonic()
        if _MODEL_IDLE_S > 0 and _evictor is None:
            _evictor = threading.Thread(target=_evict_when_idle, daemon=True)
            _evictor.start()
        return _model


def release_model():
    """Drop the cached model now. For tests and for freeing memory on demand."""
    global _model, _model_key

    with _model_lock:
        _model = _model_key = None


def stored_model(con: sqlite3.Connection) -> str | None:
    """The model the existing vectors were built with, or None if unembedded."""
    row = con.execute("SELECT value FROM embedding_meta WHERE key='model'").fetchone()
    return row[0] if row else None


def embed_stats() -> dict:
    con = connect()
    _ensure_schema(con)
    con.executescript(EMBED_SCHEMA)
    total = con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    # Count chunks that *have* a vector, not rows in the vector table. Those
    # differ: chunk_id declares a foreign key, but SQLite does not enforce one
    # unless asked, so re-chunking or removing an episode leaves vectors whose
    # chunk is gone. Counting rows made coverage exceed 1.0 - which quietly
    # cost the field its only job, since a number that can read 1.0011 can no
    # longer distinguish complete from complete-plus-stale. Joining makes the
    # ratio correct by construction rather than by clamping it afterwards.
    done = con.execute(
        "SELECT COUNT(*) FROM embeddings e JOIN chunks c ON c.id = e.chunk_id"
    ).fetchone()[0]
    vectors = con.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
    # Report what the vectors actually are, not what this shell happens to be
    # configured for — otherwise status misreports whenever QS_EMBED_MODEL is
    # unset in the calling environment but the store was built with another.
    stored = stored_model(con)
    con.close()
    stats = {"chunks": total, "embedded": done,
             "coverage": round(done / total, 4) if total else 0.0,
             "model": stored or model_name()}
    # Reported only when there are any, in the same spirit as model_mismatch
    # below: a line that is always zero stops being read. Orphans are harmless
    # to results - nothing can match a chunk that no longer exists - but they
    # are the difference between coverage being right and merely looking it.
    if vectors > done:
        stats["orphan_vectors"] = vectors - done
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
                   e.title, e.upload_date, e.url, e.caption_quality
            FROM chunks c JOIN episodes e ON e.episode_id = c.episode_id
            WHERE c.id = ?
        """, (score, cid)).fetchone()
        if row:
            hits.append(_hit(row))
    con.close()
    return hits
