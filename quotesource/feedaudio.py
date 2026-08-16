"""Podcast-feed audio: fetching it, and lending it to a captioned episode.

Two jobs that only exist because transcript and audio can come from different
places. A YouTube source gives captions cheaply and gets throttled for bytes; a
podcast feed gives bytes freely and carries no captions. Fetch from the feed,
then hand the file to the episode whose captions located the quote.

The lending is a hardlink into the captioned episode's own directory, because
cut._source_media looks there first — no new code path, no second copy, and
the audio stays reachable from the feed source it came from.
"""
import json
import os
import re
import shutil
import time
import urllib.request
from pathlib import Path

from .paths import data_root

AUDIO_EXTS = (".m4a", ".webm", ".opus", ".mp3", ".ogg")

CONTENT_TYPES = {
    "audio/mpeg": ".mp3", "audio/mp3": ".mp3",
    "audio/mp4": ".m4a", "audio/x-m4a": ".m4a", "audio/aac": ".m4a",
    "audio/ogg": ".ogg", "audio/opus": ".opus",
}

USER_AGENT = "quotesource/1.0"

_WORD = re.compile(r"[a-z0-9']+")


def _episodes_root() -> Path:
    return data_root() / "episodes"


def stored_audio(ep_dir: Path) -> Path | None:
    """The episode's kept audio, whatever container it arrived in."""
    return next((p for p in sorted(ep_dir.glob("audio.*"))
                 if p.suffix in AUDIO_EXTS), None)


def _norm_title(text: str) -> str:
    return " ".join(_WORD.findall((text or "").lower()))


def _read_meta(ep_dir: Path) -> dict | None:
    p = ep_dir / "metadata.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        return None


def _write_meta(ep_dir: Path, meta: dict):
    (ep_dir / "metadata.json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")


def episodes(source_id: str):
    """(ep_dir, metadata) for every episode of a source, oldest name first."""
    root = _episodes_root() / source_id
    if not root.is_dir():
        return []
    out = []
    for d in sorted(p for p in root.iterdir() if p.is_dir()):
        meta = _read_meta(d)
        if meta is not None:
            out.append((d, meta))
    return out


# ── fetching ──────────────────────────────────────────────────────────────────

def extension_for(url: str, content_type: str | None) -> str:
    """Container to save as. Content-Type wins; the URL is the fallback."""
    if content_type:
        base = content_type.split(";")[0].strip().lower()
        if base in CONTENT_TYPES:
            return CONTENT_TYPES[base]
    lowered = (url or "").lower()
    for ext in AUDIO_EXTS:
        if ext in lowered:
            return ext
    return ".mp3"


def _download(url: str, ep_dir: Path, timeout: float = 120.0) -> tuple[Path, int]:
    """Fetch to audio.<ext>, via .part so a kill cannot leave a short file."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        declared = int(resp.headers.get("Content-Length") or 0)
        final = ep_dir / f"audio{extension_for(resp.url, resp.headers.get('Content-Type'))}"
        part = final.with_name(final.name + ".part")
        got = 0
        with open(part, "wb") as fh:
            while chunk := resp.read(1 << 20):
                fh.write(chunk)
                got += len(chunk)
    # A truncated download that kept its name would look complete forever.
    if declared and got < declared * 0.95:
        part.unlink(missing_ok=True)
        raise IOError(f"short read: {got} of {declared} bytes")
    os.replace(part, final)
    return final, got


def fetch_source_audio(source_id: str, limit: int | None = None,
                       sleep_s: float = 2.0, progress=None) -> dict:
    """Download audio for every episode of a feed source that lacks it.

    Resumable by construction: an episode that already has audio is skipped,
    so re-running after an interruption costs nothing.
    """
    todo = [(d, m) for d, m in episodes(source_id)
            if m.get("audio_url") and not stored_audio(d)]
    if limit is not None:
        todo = todo[:limit]

    result = {"source": source_id, "considered": len(todo),
              "fetched": 0, "failed": 0, "bytes": 0, "failures": []}
    for index, (ep_dir, meta) in enumerate(todo, 1):
        try:
            path, got = _download(meta["audio_url"], ep_dir)
            result["fetched"] += 1
            result["bytes"] += got
            if progress:
                progress(f"[{index}/{len(todo)}] {got / 1048576:.1f} MB  "
                         f"{(meta.get('title') or '')[:52]}")
        except Exception as exc:
            result["failed"] += 1
            result["failures"].append(
                {"episode_id": meta.get("episode_id"), "error": str(exc)})
            if progress:
                progress(f"[{index}/{len(todo)}] FAILED {exc}")
        if sleep_s:
            time.sleep(sleep_s)
    return result


# ── lending it to a captioned episode ─────────────────────────────────────────

def match_by_title(title: str, candidates, min_ratio: float = 0.60):
    """Closest (ep_dir, meta) by title, or None. candidates: [(dir, meta)]."""
    import difflib

    wanted = _norm_title(title)
    best, score = None, 0.0
    for ep_dir, meta in candidates:
        ratio = difflib.SequenceMatcher(
            None, wanted, _norm_title(meta.get("title"))).ratio()
        if ratio > score:
            best, score = (ep_dir, meta), ratio
    return (best, score) if best and score >= min_ratio else (None, score)


def plan_links(caption_source: str, feed_source: str, tolerance: float = 1.0):
    """Which captioned episodes can safely borrow feed audio.

    Equal durations do not prove the timelines align, but unequal ones prove
    they do not: a feed carrying a pre-roll the upload lacks reports a longer
    episode. Pairs that differ by more than `tolerance` are left for the
    offset probe, which measures the shift instead of assuming none.
    """
    feed = [(d, m) for d, m in episodes(feed_source) if stored_audio(d)]
    plan = {"link": [], "differs": [], "unmatched": [], "already": []}
    for ep_dir, meta in episodes(caption_source):
        if stored_audio(ep_dir) or meta.get("audio_provenance"):
            plan["already"].append(ep_dir.name)
            continue
        match, score = match_by_title(meta.get("title"), feed)
        if not match:
            plan["unmatched"].append(ep_dir.name)
            continue
        feed_dir, feed_meta = match
        a, b = meta.get("duration") or 0, feed_meta.get("duration") or 0
        delta = (a - b) if (a and b) else None
        row = {"episode_id": ep_dir.name, "delta": delta, "title_score": round(score, 3),
               "caption_dir": ep_dir, "feed_dir": feed_dir, "feed_meta": feed_meta}
        if delta is not None and abs(delta) <= tolerance:
            plan["link"].append(row)
        else:
            plan["differs"].append(row)
    return plan


def link(caption_dir: Path, feed_dir: Path, feed_meta: dict,
         offset_s: float = 0.0, alignment: str = "duration_exact",
         extra: dict | None = None) -> Path:
    """Hardlink feed audio beside a captioned episode and record where from."""
    src = stored_audio(feed_dir)
    if src is None:
        raise FileNotFoundError(f"no stored audio in {feed_dir}")
    dst = caption_dir / f"audio{src.suffix}"
    if not dst.exists():
        try:
            os.link(src, dst)
        except OSError:          # different filesystem, or no link support
            shutil.copy2(src, dst)

    meta = _read_meta(caption_dir) or {}
    provenance = {
        "linked_from": f"{feed_dir.parent.name}/{feed_dir.name}",
        "feed_title": feed_meta.get("title"),
        "offset_s": offset_s,
        "alignment": alignment,
    }
    if extra:
        provenance.update(extra)
    meta["audio_provenance"] = provenance
    _write_meta(caption_dir, meta)
    return dst


def link_matching(caption_source: str, feed_source: str,
                  tolerance: float = 1.0, apply: bool = False) -> dict:
    plan = plan_links(caption_source, feed_source, tolerance)
    result = {
        "caption_source": caption_source, "feed_source": feed_source,
        "tolerance": tolerance,
        "linkable": len(plan["link"]), "differs": len(plan["differs"]),
        "unmatched": len(plan["unmatched"]), "already": len(plan["already"]),
        "linked": 0,
        "deltas": sorted(r["delta"] for r in plan["differs"]
                         if r["delta"] is not None),
    }
    if apply:
        for row in plan["link"]:
            link(row["caption_dir"], row["feed_dir"], row["feed_meta"])
            result["linked"] += 1
    return result
