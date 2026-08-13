"""Episode enumeration and caption ingest.

YouTube: yt-dlp flat enumeration, then per-episode metadata + caption fetch.
RSS: feedparser; episodes marked needs_transcription (no captions exist).

Idempotent: an episode whose metadata.json exists is skipped, except episodes
whose caption fetch previously failed (status "captions_pending") which are
retried. Politely throttled; 429s back off hard.
"""
import hashlib
import json
import re
import time
from datetime import datetime
from pathlib import Path

from . import registry
from .paths import episode_dir, ensure_root
from .transcripts import normalize_captions

SLEEP_BETWEEN_EPISODES = 2.0
BACKOFF_SCHEDULE = [60, 180, 600]  # seconds, on rate-limit errors

# Caption statuses:
#   captions            - transcript.json from YouTube captions
#   whisper             - transcript.json from whisper (Phase 2)
#   needs_transcription - no captions available; whisper queue
#   captions_pending    - caption fetch failed transiently; retry next ingest


def _log(quiet: bool, msg: str):
    if not quiet:
        print(f"  {msg}", flush=True)


def _write_metadata(ep_dir: Path, meta: dict):
    (ep_dir / "metadata.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8"
    )


def load_metadata(ep_dir: Path) -> dict | None:
    p = ep_dir / "metadata.json"
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def episode_status(ep_dir: Path) -> str:
    meta = load_metadata(ep_dir)
    if not meta:
        return "unknown"
    if (ep_dir / "transcript.json").exists():
        t = json.loads((ep_dir / "transcript.json").read_text(encoding="utf-8"))
        src = t.get("transcript_source", "")
        return "whisper" if src == "whisper" else "captions"
    return meta.get("status", "unknown")


def _is_rate_limit(exc: Exception) -> bool:
    s = str(exc).lower()
    return "429" in s or "rate" in s or "too many requests" in s


# ── YouTube ───────────────────────────────────────────────────────────────────

def _enumerate_youtube(url: str, source_type: str) -> list[dict]:
    import yt_dlp

    if source_type == "youtube_channel" and "/videos" not in url and "playlist" not in url:
        url = url.rstrip("/") + "/videos"

    opts = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "extract_flat": "in_playlist",
        "skip_download": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    entries = info.get("entries") or []
    out = []
    for e in entries:
        if not e or not e.get("id"):
            continue
        out.append({
            "episode_id": e["id"],
            "url": e.get("url") or f"https://www.youtube.com/watch?v={e['id']}",
            "title": e.get("title") or "",
            "duration": e.get("duration"),
        })
    return out


def _fetch_youtube_episode(source: dict, entry: dict, quiet: bool) -> dict:
    """Fetch full metadata + captions for one episode. Returns metadata dict."""
    import yt_dlp

    ep_dir = episode_dir(source["id"], entry["episode_id"])
    ep_dir.mkdir(parents=True, exist_ok=True)

    opts = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "skip_download": True,
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": ["en", "en-US", "en-GB", "en-orig"],
        "subtitlesformat": "json3/vtt/best",
        "outtmpl": str(ep_dir / "captions.raw"),
    }

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(entry["url"], download=True)

    manual_subs = info.get("subtitles") or {}
    has_manual = any(k.startswith("en") for k in manual_subs)
    auto_subs = info.get("automatic_captions") or {}
    has_auto = any(k.startswith("en") for k in auto_subs)

    meta = {
        "episode_id": entry["episode_id"],
        "source_id": source["id"],
        "title": info.get("title") or entry["title"],
        "description": info.get("description") or "",
        "upload_date": info.get("upload_date"),  # YYYYMMDD
        "duration": info.get("duration"),
        "url": info.get("webpage_url") or entry["url"],
        "uploader": info.get("uploader"),
        "ingested_at": datetime.now().isoformat(),
        "caption_kind": "manual" if has_manual else ("auto" if has_auto else "none"),
        "status": "needs_transcription",
    }

    raw_files = list(ep_dir.glob("captions.raw*"))
    if raw_files:
        transcript_source = "youtube_manual" if has_manual else "youtube_auto"
        t = normalize_captions(ep_dir, entry["episode_id"], source["id"], transcript_source)
        if t:
            meta["status"] = "captions"
            _log(quiet, f"{entry['episode_id']}  captions ({meta['caption_kind']}, {len(t['segments'])} segments)")
        else:
            meta["status"] = "captions_pending"
            _log(quiet, f"{entry['episode_id']}  caption parse failed, will retry")
    elif has_manual or has_auto:
        # captions exist upstream but fetch produced nothing -> transient
        meta["status"] = "captions_pending"
        _log(quiet, f"{entry['episode_id']}  captions listed but not fetched, will retry")
    else:
        _log(quiet, f"{entry['episode_id']}  no captions; queued for whisper")

    _write_metadata(ep_dir, meta)
    return meta


# ── RSS ───────────────────────────────────────────────────────────────────────

def _rss_episode_id(entry) -> str:
    guid = entry.get("id") or entry.get("link") or entry.get("title", "")
    return "rss-" + hashlib.sha1(guid.encode("utf-8")).hexdigest()[:12]


def _enumerate_rss(url: str) -> list[dict]:
    import feedparser

    feed = feedparser.parse(url)
    out = []
    for e in feed.entries:
        audio_url = None
        for link in e.get("links", []):
            if link.get("rel") == "enclosure" or (link.get("type") or "").startswith("audio"):
                audio_url = link.get("href")
                break
        pub = None
        if e.get("published_parsed"):
            pub = time.strftime("%Y%m%d", e.published_parsed)
        dur = None
        raw_dur = e.get("itunes_duration")
        if raw_dur:
            try:
                parts = [int(p) for p in str(raw_dur).split(":")]
                dur = parts[0] if len(parts) == 1 else (
                    parts[0] * 60 + parts[1] if len(parts) == 2
                    else parts[0] * 3600 + parts[1] * 60 + parts[2])
            except ValueError:
                pass
        out.append({
            "episode_id": _rss_episode_id(e),
            "title": e.get("title", ""),
            "description": re.sub(r"<[^>]+>", " ", e.get("summary", "")).strip(),
            "upload_date": pub,
            "duration": dur,
            "url": e.get("link") or audio_url or "",
            "audio_url": audio_url,
        })
    return out


def _fetch_rss_episode(source: dict, entry: dict, quiet: bool) -> dict:
    ep_dir = episode_dir(source["id"], entry["episode_id"])
    ep_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "episode_id": entry["episode_id"],
        "source_id": source["id"],
        "title": entry["title"],
        "description": entry["description"],
        "upload_date": entry["upload_date"],
        "duration": entry["duration"],
        "url": entry["url"],
        "audio_url": entry["audio_url"],
        "ingested_at": datetime.now().isoformat(),
        "caption_kind": "none",
        "status": "needs_transcription",
    }
    _write_metadata(ep_dir, meta)
    _log(quiet, f"{entry['episode_id']}  {entry['title'][:60]}  (rss, needs transcription)")
    return meta


# ── Driver ────────────────────────────────────────────────────────────────────

def ingest_source(source: dict, limit: int | None = None, quiet: bool = False,
                  min_duration: int | None = None) -> dict:
    ensure_root()
    stype = source["type"]
    if not quiet:
        print(f"[{source['id']}] enumerating {stype}…", flush=True)

    if stype in ("youtube_channel", "youtube_playlist"):
        entries = _enumerate_youtube(source["url"], stype)
    elif stype == "rss":
        entries = _enumerate_rss(source["url"])
    else:
        raise ValueError(f"unknown source type: {stype}")

    enumerated = len(entries)
    if min_duration is None:
        min_duration = registry.parse_duration(source.get("min_duration"))

    # Channels that publish clips alongside full episodes would otherwise put
    # the same words in the corpus twice. Unknown durations are kept, because
    # silently dropping a real episode is worse than admitting a clip.
    too_short = 0
    if min_duration:
        kept = []
        for entry in entries:
            duration = entry.get("duration")
            if duration is not None and duration < min_duration:
                too_short += 1
                continue
            kept.append(entry)
        entries = kept
        _log(quiet, f"[{source['id']}] {too_short} under {min_duration}s skipped, "
                    f"{len(entries)} to consider")

    result = {
        "source": source["id"],
        "enumerated": enumerated,
        "min_duration": min_duration,
        "too_short": too_short,
        "new": 0, "retried": 0, "skipped": 0, "failed": 0,
        "episodes": [],
    }

    processed = 0
    for entry in entries:
        if limit is not None and processed >= limit:
            break
        ep_dir = episode_dir(source["id"], entry["episode_id"])
        existing = load_metadata(ep_dir)
        if existing and existing.get("status") != "captions_pending":
            result["skipped"] += 1
            continue

        is_retry = existing is not None
        try:
            if stype == "rss":
                _fetch_rss_episode(source, entry, quiet)
            else:
                _fetch_with_backoff(source, entry, quiet)
            result["retried" if is_retry else "new"] += 1
            result["episodes"].append(entry["episode_id"])
        except Exception as e:
            result["failed"] += 1
            result.setdefault("failures", []).append(
                {"episode_id": entry["episode_id"], "error": str(e)})
            # failures always print, even with --quiet
            print(f"  {entry['episode_id']}  FAILED: {e}", flush=True)
        processed += 1
        if stype != "rss":
            time.sleep(SLEEP_BETWEEN_EPISODES)

    return result


def _fetch_with_backoff(source: dict, entry: dict, quiet: bool):
    last = None
    for i, backoff in enumerate([0] + BACKOFF_SCHEDULE):
        if backoff:
            _log(quiet, f"rate limited; sleeping {backoff}s")
            time.sleep(backoff)
        try:
            return _fetch_youtube_episode(source, entry, quiet)
        except Exception as e:
            last = e
            if not _is_rate_limit(e):
                raise
    raise last


def list_episodes(source_id: str) -> list[dict]:
    base = ensure_root() / "episodes" / source_id
    if not base.exists():
        return []
    out = []
    for ep_dir in sorted(base.iterdir()):
        if not ep_dir.is_dir():
            continue
        meta = load_metadata(ep_dir)
        if not meta:
            continue
        out.append({
            "episode_id": meta["episode_id"],
            "title": meta.get("title", ""),
            "upload_date": meta.get("upload_date"),
            "duration": meta.get("duration"),
            "url": meta.get("url"),
            "caption_kind": meta.get("caption_kind"),
            "status": episode_status(ep_dir),
        })
    out.sort(key=lambda e: e.get("upload_date") or "", reverse=True)
    return out
