"""Search primitives: grep (FTS5), context, episode-info.
Semantic search (qs search) arrives with the embedding layer.

Hit shape (shared by grep and search):
{episode_id, source_id, start, end, text, score,
 episode_title, upload_date, url, url_ts, caption_quality}

`caption_quality` is `clean`, `raw` or `unknown` - a prompt to verify, not a
verdict. `raw` means the transcript reads as machine output and the wording
should be checked with `context` before it is quoted anywhere.
"""
import json
import sqlite3
from pathlib import Path

from .indexer import connect, _ensure_schema
from .paths import episode_dir
from .registry import list_sources


def _person_episode_filter(person: str) -> tuple[str, list]:
    """Match source people lists + episode title/description text."""
    person_sources = [
        s["id"] for s in list_sources()
        if any(person.lower() in p.lower() for p in s.get("people", []))
    ]
    clause = "(e.title LIKE ? OR e.description LIKE ?"
    params: list = [f"%{person}%", f"%{person}%"]
    if person_sources:
        clause += f" OR e.source_id IN ({','.join('?' * len(person_sources))})"
        params += person_sources
    clause += ")"
    return clause, params


def _shared_filters(source=None, person=None, after=None, before=None):
    clauses, params = [], []
    if source:
        clauses.append("e.source_id = ?")
        params.append(source)
    if person:
        c, p = _person_episode_filter(person)
        clauses.append(c)
        params += p
    if after:
        clauses.append("e.upload_date >= ?")
        params.append(after.replace("-", ""))
    if before:
        clauses.append("e.upload_date <= ?")
        params.append(before.replace("-", ""))
    return clauses, params


def _ts_url(url: str, start: float) -> str:
    if not url:
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}t={int(start)}s"


def _hit(row) -> dict:
    (episode_id, source_id, start, end, text, score,
     title, upload_date, url, quality) = row
    return {
        "episode_id": episode_id,
        "source_id": source_id,
        "start": start,
        "end": end,
        "text": text,
        "score": round(float(score), 4),
        "episode_title": title,
        "upload_date": upload_date,
        "url": url,
        "url_ts": _ts_url(url, start),
        # Whether this wording is worth trusting. transcript_source says who
        # uploaded the captions, which turns out not to answer that.
        "caption_quality": quality or "unknown",
    }


def grep(terms: str, source=None, person=None, after=None, before=None,
         limit: int = 20) -> list[dict]:
    con = connect()
    _ensure_schema(con)
    clauses, params = _shared_filters(source, person, after, before)
    where = (" AND " + " AND ".join(clauses)) if clauses else ""
    sql = f"""
        SELECT c.episode_id, e.source_id, c.start, c.end, c.text,
               -bm25(chunks_fts) AS score,
               e.title, e.upload_date, e.url, e.caption_quality
        FROM chunks_fts
        JOIN chunks c ON c.id = chunks_fts.rowid
        JOIN episodes e ON e.episode_id = c.episode_id
        WHERE chunks_fts MATCH ?{where}
        ORDER BY bm25(chunks_fts)
        LIMIT ?
    """
    try:
        rows = con.execute(sql, [terms] + params + [limit]).fetchall()
    except sqlite3.OperationalError:
        # FTS5 syntax error (stray operators etc.) -> retry as quoted phrase
        safe = '"' + terms.replace('"', ' ') + '"'
        rows = con.execute(sql, [safe] + params + [limit]).fetchall()
    con.close()
    return [_hit(r) for r in rows]


def context(episode_id: str, timestamp: float | None = None,
            range_: tuple[float, float] | None = None,
            window: float = 30.0) -> dict:
    ep_dir = _find_episode_dir(episode_id)
    if not ep_dir:
        raise FileNotFoundError(f"episode '{episode_id}' not found")
    transcript = json.loads((ep_dir / "transcript.json").read_text(encoding="utf-8"))
    meta = json.loads((ep_dir / "metadata.json").read_text(encoding="utf-8"))

    if range_:
        lo, hi = range_
    else:
        lo, hi = timestamp - window, timestamp + window

    segs = [s for s in transcript["segments"] if s["end"] >= lo and s["start"] <= hi]
    return {
        "episode_id": episode_id,
        "source_id": meta.get("source_id"),
        "episode_title": meta.get("title"),
        "url": meta.get("url"),
        "url_ts": _ts_url(meta.get("url", ""), max(lo, 0)),
        "window": [round(lo, 3), round(hi, 3)],
        "transcript_source": transcript.get("transcript_source"),
        "segments": segs,
    }


def episode_info(episode_id: str) -> dict:
    ep_dir = _find_episode_dir(episode_id)
    if not ep_dir:
        raise FileNotFoundError(f"episode '{episode_id}' not found")
    meta = json.loads((ep_dir / "metadata.json").read_text(encoding="utf-8"))
    info = dict(meta)
    tpath = ep_dir / "transcript.json"
    if tpath.exists():
        t = json.loads(tpath.read_text(encoding="utf-8"))
        segs = t.get("segments", [])
        info["transcript"] = {
            "transcript_source": t.get("transcript_source"),
            "segments": len(segs),
            "words": sum(len(s["text"].split()) for s in segs),
            "first_start": segs[0]["start"] if segs else None,
            "last_end": segs[-1]["end"] if segs else None,
        }
    else:
        info["transcript"] = None
    return info


def _find_episode_dir(episode_id: str) -> Path | None:
    from .paths import ensure_root

    episodes_root = ensure_root() / "episodes"
    if not episodes_root.exists():
        return None
    for src_dir in episodes_root.iterdir():
        cand = src_dir / episode_id
        if cand.is_dir():
            return cand
    return None
