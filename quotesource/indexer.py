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


def forget_episode(con: sqlite3.Connection, episode_id: str) -> int:
    """Remove every trace of one episode from the index. Returns chunks dropped.

    Three tables hang off a chunk and deleting from one is not deleting from
    the others:

      chunks_fts   is an external-content FTS5 table, so a plain DELETE on
                   chunks leaves its terms pointing at rowids that no longer
                   exist. It has to be told, in its own syntax, first.
      embeddings   is keyed on chunk_id and declares a foreign key, but
                   SQLite does not enforce one unless asked - so vectors
                   simply outlive their chunks.

    That second one is where the orphaned vectors came from: re-indexing an
    episode whose transcript changed (a whisper backfill, say) mints new
    chunk ids and stranded every old vector, which is what pushed reported
    coverage above 1.0. Deleting a chunk without its vector is an incomplete
    delete, not a policy choice, so it happens here in one place.
    """
    # An index that was never built has no tables to clear, and building one
    # here to discover that would be a write on what is otherwise a read.
    if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                       "AND name='chunks'").fetchone():
        return 0
    dropped = con.execute(
        "SELECT COUNT(*) FROM chunks WHERE episode_id=?", (episode_id,)
    ).fetchone()[0]
    con.execute(
        "INSERT INTO chunks_fts(chunks_fts, rowid, text) "
        "SELECT 'delete', id, text FROM chunks WHERE episode_id=?", (episode_id,))
    # embeddings is the embedder's table, not this module's, so it may simply
    # not exist yet on an index that has never been embedded. Asked for, not
    # assumed - and not created here either, since owning a table means
    # owning its schema.
    if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                   "AND name='embeddings'").fetchone():
        con.execute(
            "DELETE FROM embeddings WHERE chunk_id IN "
            "(SELECT id FROM chunks WHERE episode_id=?)", (episode_id,))
    con.execute("DELETE FROM chunks WHERE episode_id=?", (episode_id,))
    con.execute("DELETE FROM episodes WHERE episode_id=?", (episode_id,))
    return dropped


PUNCT_PER_WORD = 0.02
CAPITAL_RATIO = 0.02


def _transcript_text(transcript: dict) -> str:
    """Everything said in an episode, as one string."""
    return " ".join((seg.get("text") or "")
                    for seg in (transcript or {}).get("segments", []))


def caption_quality(text: str) -> str:
    """`clean` if a transcript reads as written, `raw` if it reads as machine.

    `transcript_source: manual` looks like a quality signal and is not one:
    creators routinely upload an unedited auto-caption dump as a manual track,
    so both extremes turn up inside a single source. One Vervaeke episode
    gives "Plato is deeply influenced by the natural philosophers" and another
    gives "my my contention and what i'm going to argue is it's no
    coincidence" - and both are recorded the same way.

    What actually separates them is punctuation and capitals, because an
    auto-caption stream has neither. Two ratios rather than one: a transcript
    can carry sentence marks with no capitals, and lowercase prose with commas
    is still not something to quote from without checking.

    This is a **verification prompt, not a verdict**. `raw` means read the
    context before quoting; `clean` means the wording is probably as spoken.
    Neither replaces `qs context`.
    """
    words = (text or "").split()
    if len(words) < 20:
        # Too little to judge. Saying "raw" would cry wolf on every short
        # transcript; saying "clean" would vouch for something unexamined.
        return "unknown"
    marks = sum(text.count(c) for c in ".?!")
    capitals = sum(1 for w in words if w[:1].isupper())
    if (marks / len(words) >= PUNCT_PER_WORD
            and capitals / len(words) >= CAPITAL_RATIO):
        return "clean"
    return "raw"


def _ensure_schema(con: sqlite3.Connection):
    con.executescript(SCHEMA)
    # CREATE TABLE IF NOT EXISTS will not add a column to a table that already
    # exists, so the one migration this index has ever needed is done here.
    # It is safe to do silently precisely because this database is derived:
    # everything in it can be rebuilt from the transcripts on disk, which is
    # why caption_quality lives here and not in metadata.json.
    columns = {row[1] for row in con.execute("PRAGMA table_info(episodes)")}
    if "caption_quality" not in columns:
        con.execute("ALTER TABLE episodes ADD COLUMN caption_quality TEXT")
        con.commit()


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
    # Episodes indexed before caption_quality existed. Filled in below without
    # re-chunking: re-indexing them would mint new chunk ids and strand every
    # vector already computed for them, which is hours of GPU time to answer a
    # question the transcript on disk can answer for free.
    unrated = {row[0] for row in con.execute(
        "SELECT episode_id FROM episodes WHERE caption_quality IS NULL")}

    stats = {"indexed": 0, "skipped": 0, "removed": 0, "chunks": 0, "rated": 0}
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
                if episode_id in unrated:
                    transcript = json.loads(tpath.read_text(encoding="utf-8"))
                    con.execute(
                        "UPDATE episodes SET caption_quality=? "
                        "WHERE episode_id=?",
                        (caption_quality(_transcript_text(transcript)),
                         episode_id))
                    stats["rated"] = stats.get("rated", 0) + 1
                continue

            meta = json.loads(mpath.read_text(encoding="utf-8"))
            transcript = json.loads(tpath.read_text(encoding="utf-8"))
            chunks = chunk_segments(transcript.get("segments", []))

            # replace any previous rows for this episode
            forget_episode(con, episode_id)

            # Named rather than positional: a bare VALUES(...) breaks the
            # moment the table gains a column, which it just did.
            con.execute(
                "INSERT INTO episodes (episode_id, source_id, title, "
                "description, upload_date, url, duration, transcript_source, "
                "transcript_hash, indexed_at, caption_quality) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (episode_id, src_dir.name, meta.get("title"),
                 meta.get("description"), meta.get("upload_date"),
                 meta.get("url"), meta.get("duration"),
                 transcript.get("transcript_source"), thash,
                 datetime.now().isoformat(),
                 caption_quality(_transcript_text(transcript))))
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
        forget_episode(con, episode_id)
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
