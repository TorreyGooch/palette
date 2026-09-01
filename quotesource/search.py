"""Search primitives: grep (FTS5), context, episode-info.
Semantic search (qs search) arrives with the embedding layer.

Hit shape (shared by grep and search):
{episode_id, source_id, start, end, text, score,
 episode_title, upload_date, url, url_ts, caption_quality, audio_stored, duplicate_of}

`caption_quality` is `clean`, `raw` or `unknown` - a prompt to verify, not a
verdict. `raw` means the transcript reads as machine output and the wording
should be checked with `context` before it is quoted anywhere.
"""
import json
import sqlite3
from pathlib import Path

from .feedaudio import stored_audio
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


def _audio_stored(source_id: str, episode_id: str):
    """Whether the episode's audio is already on disk. True, False, or unknown.

    This is on the hit because that is where the decision is made: `True`
    means a cut costs nothing, `False` means the first one fetches the whole
    episode (~50 MB) and may be refused. It was otherwise knowable only by
    planning a cut, spending the pull and finding out.

    **A bool rather than an enum, on purpose.** The obvious shape was
    `stored | fetchable | refused`, and two of those three are wrong.
    `fetchable` is a prediction dressed as a fact — nothing knows an episode
    can be fetched until it fetches it. `refused` is a fact about *an
    attempt*, at a moment, by us: a 403 decays, the audio never changed, and
    storing it as a property of the episode makes a verdict that goes stale
    silently. That is the same conflation that made `words` report "audio not
    stored" for a CUDA failure, and the house rule against it is already
    written down — a stored stage goes stale and then lies.

    So this answers one question about the artifact, checked at read time.
    `None` means the question could not be answered, which is the absence of a
    fact rather than a third state of the world. Evidence about attempts, if
    it is ever wanted, belongs beside it and dated — an undated `refused`
    cannot be aged by its reader; `403 on 2026-08-31` can.
    """
    try:
        return bool(stored_audio(episode_dir(source_id, episode_id)))
    except Exception:
        return None


def _hit(row) -> dict:
    (episode_id, source_id, start, end, text, score,
     title, upload_date, url, quality, duplicate_of) = row
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
        # Whether cutting it needs the network.
        "audio_stored": _audio_stored(source_id, episode_id),
        # The same conversation under another source id, or None. Search
        # otherwise returns one moment twice under two attributions with
        # nothing saying they are one thing.
        "duplicate_of": duplicate_of,
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
               e.title, e.upload_date, e.url, e.caption_quality, e.duplicate_of
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
