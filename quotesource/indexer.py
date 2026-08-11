"""Transcript chunking and SQLite index (FTS5 now, vectors in 3b).

Incremental: each episode's transcript.json is hashed; unchanged episodes are
skipped, changed ones (e.g. whisper replacing captions) are re-chunked.
"""
import hashlib
import json
import sqlite3
from datetime import datetime
from pathlib import Path

from .paths import ensure_root

CHUNK_TARGET_WORDS = 70
CHUNK_OVERLAP_SEGMENTS = 1  # carry last N segments into the next chunk


def db_path() -> Path:
    d = ensure_root() / "index"
    d.mkdir(exist_ok=True)
    return d / "index.db"


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(db_path())
    con.execute("PRAGMA journal_mode=WAL")
    return con


SCHEMA = """
CREATE TABLE IF NOT EXISTS episodes (
    episode_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    title TEXT, description TEXT, upload_date TEXT, url TEXT,
    duration REAL, transcript_source TEXT, transcript_hash TEXT,
    indexed_at TEXT
);
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY,
    episode_id TEXT NOT NULL REFERENCES episodes(episode_id),
    start REAL, end REAL, text TEXT
);
CREATE INDEX IF NOT EXISTS idx_chunks_episode ON chunks(episode_id);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text, content='chunks', content_rowid='id', tokenize='porter unicode61'
);
"""


def _ensure_schema(con: sqlite3.Connection):
    con.executescript(SCHEMA)


def chunk_segments(segments: list[dict]) -> list[dict]:
    """Group consecutive segments into ~CHUNK_TARGET_WORDS chunks with overlap."""
    chunks = []
    cur_segs = []
    cur_words = 0
    for seg in segments:
        words = len(seg["text"].split())
        cur_segs.append(seg)
        cur_words += words
        if cur_words >= CHUNK_TARGET_WORDS:
            chunks.append(_mk_chunk(cur_segs))
            # overlap: start next chunk with the tail segments
            cur_segs = cur_segs[-CHUNK_OVERLAP_SEGMENTS:] if CHUNK_OVERLAP_SEGMENTS else []
            cur_words = sum(len(s["text"].split()) for s in cur_segs)
    if cur_segs and (not chunks or cur_segs != chunks[-1:]):
        c = _mk_chunk(cur_segs)
        if not chunks or c["text"] != chunks[-1]["text"]:
            chunks.append(c)
    return chunks


def _mk_chunk(segs: list[dict]) -> dict:
    return {
        "start": segs[0]["start"],
        "end": segs[-1]["end"],
        "text": " ".join(s["text"] for s in segs),
    }


def _transcript_hash(p: Path) -> str:
    return hashlib.sha1(p.read_bytes()).hexdigest()


def build_index(rebuild: bool = False, quiet: bool = True) -> dict:
    root = ensure_root()
    con = connect()
    _ensure_schema(con)
    if rebuild:
        con.execute("DELETE FROM chunks")
        con.execute("DELETE FROM episodes")
        con.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('delete-all')")
        con.commit()

    known = {row[0]: row[1] for row in
             con.execute("SELECT episode_id, transcript_hash FROM episodes")}

    stats = {"indexed": 0, "skipped": 0, "removed": 0, "chunks": 0}
    seen = set()

    episodes_root = root / "episodes"
    for src_dir in sorted(episodes_root.iterdir()) if episodes_root.exists() else []:
        if not src_dir.is_dir():
            continue
        for ep_dir in sorted(src_dir.iterdir()):
            tpath = ep_dir / "transcript.json"
            mpath = ep_dir / "metadata.json"
            if not tpath.exists() or not mpath.exists():
                continue
            episode_id = ep_dir.name
            seen.add(episode_id)
            thash = _transcript_hash(tpath)
            if known.get(episode_id) == thash:
                stats["skipped"] += 1
                continue

            meta = json.loads(mpath.read_text(encoding="utf-8"))
            transcript = json.loads(tpath.read_text(encoding="utf-8"))
            chunks = chunk_segments(transcript.get("segments", []))

            # replace any previous rows for this episode
            old = [r[0] for r in con.execute(
                "SELECT id FROM chunks WHERE episode_id=?", (episode_id,))]
            for cid in old:
                con.execute(
                    "INSERT INTO chunks_fts(chunks_fts, rowid, text) "
                    "SELECT 'delete', id, text FROM chunks WHERE id=?", (cid,))
            con.execute("DELETE FROM chunks WHERE episode_id=?", (episode_id,))
            con.execute("DELETE FROM episodes WHERE episode_id=?", (episode_id,))

            con.execute(
                "INSERT INTO episodes VALUES (?,?,?,?,?,?,?,?,?,?)",
                (episode_id, src_dir.name, meta.get("title"),
                 meta.get("description"), meta.get("upload_date"),
                 meta.get("url"), meta.get("duration"),
                 transcript.get("transcript_source"), thash,
                 datetime.now().isoformat()))
            for c in chunks:
                cur = con.execute(
                    "INSERT INTO chunks (episode_id, start, end, text) VALUES (?,?,?,?)",
                    (episode_id, c["start"], c["end"], c["text"]))
                con.execute(
                    "INSERT INTO chunks_fts(rowid, text) VALUES (?,?)",
                    (cur.lastrowid, c["text"]))
            stats["indexed"] += 1
            stats["chunks"] += len(chunks)
            if not quiet:
                print(f"  {episode_id}  {len(chunks)} chunks", flush=True)
            con.commit()

    # drop episodes whose transcript disappeared
    for episode_id in set(known) - seen:
        con.execute("DELETE FROM chunks WHERE episode_id=?", (episode_id,))
        con.execute("DELETE FROM episodes WHERE episode_id=?", (episode_id,))
        stats["removed"] += 1
    con.commit()

    total = con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    eps = con.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
    con.close()
    return {**stats, "total_episodes": eps, "total_chunks": total}


def index_stats() -> dict:
    p = db_path()
    if not p.exists():
        return {"exists": False}
    con = connect()
    _ensure_schema(con)
    eps = con.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
    chunks = con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    con.close()
    return {"exists": True, "episodes": eps, "chunks": chunks,
            "db_mb": round(p.stat().st_size / 1048576, 1)}
