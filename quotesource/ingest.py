"""Episode enumeration and caption ingest.

YouTube: yt-dlp flat enumeration, then per-episode metadata + caption fetch.
RSS: feedparser; episodes marked needs_transcription (no captions exist).

Idempotent: an episode whose metadata.json exists is skipped, except episodes
whose caption fetch previously failed (status "captions_pending") which are
retried.

Throttling is deliberate and has two halves. The pause between episodes is
*jittered*, because a fixed cadence over hundreds of requests is the clearest
automation signature a client can emit. And a run gives up entirely after a
few consecutive rate-limited episodes rather than working through the list
retrying each one, which is how a soft limit gets turned into a hard one.
"""
import hashlib
import json
import os
import random
import re
import shutil
import time
from datetime import datetime, timedelta
from pathlib import Path

from . import registry
from .paths import episode_dir, ensure_root
from .transcripts import normalize_captions

SLEEP_BETWEEN_EPISODES = 2.0
SLEEP_JITTER = 0.6                 # +/- fraction of the pause, so it is not a metronome
# Retry schedules, split by who the failure is about. A 503 and a 429 are
# opposites and used to share one schedule.
BACKOFF_SCHEDULE = [30, 120]       # 5xx: the server is unwell and wants us back
TRANSPORT_BACKOFF = [2, 10]        # timeouts, resets: no signal, just noise

# Consecutive rate-limited episodes before the whole run stops. The per-episode
# backoff above survives a blip; this is what stops the loop from walking the
# rest of the channel retrying every episode against a limit that is not going
# to lift for a while.
# **A rate limit ends the run.** RFC 6585: a 429 says *this client* has sent
# too many requests. The server is healthy and rationing us specifically, so
# retrying is not merely useless - it is the behaviour limiters escalate
# against. That is the opposite of a 503, where the server is unwell and does
# want us back, and the two shared one policy until a hard 429 walked past a
# breaker set to 2.
#
# It was worse than "2 was too many". The count reset on any success, so an
# alternating limited/served pattern - the ordinary shape of a soft limit -
# could never trip it at all. The breaker was weakest exactly where it was
# most needed.
RATE_LIMIT_STOP = 1

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


def failure_kind(error, rate_limited: bool = False) -> str:
    """What kind of failure this was, so a caller need not read the prose.

    The question a run leaves behind is "retry now, or wait a day?", and
    answering it currently means eyeballing an error string. Three kinds
    actually occur:

      rate_limited - the endpoint asked us to slow down. Wait; the circuit
                     breaker has already stopped the run.
      timeout      - a read timed out. Transient, and a plain re-run fixes
                     it; three of these in one Vervaeke run all cleared on
                     the next pass.
      other        - anything else. Read it.

    Note there is no `no_captions`: a video without captions does not fail.
    It is stored with status `needs_transcription` and counts as a success,
    because the episode is in the corpus and whisper can finish the job.

    `rate_limited` comes from the exception *type* rather than its text,
    since that is certain where string matching is a guess.
    """
    if rate_limited:
        return "rate_limited"
    text = str(error or "").lower()
    if "timed out" in text or "timeout" in text:
        return "timeout"
    if "429" in text or "too many requests" in text:
        return "rate_limited"
    return "other"


class RateLimited(Exception):
    """Every attempt at one episode was refused for rate limiting."""


def _pause(base: float | None = None, jitter: float | None = None) -> float:
    """A jittered pause, in seconds.

    Requests spaced exactly 2.000s apart look like nothing a person has ever
    done. Spreading them around the mean costs nothing and removes the most
    obvious tell.
    """
    base = SLEEP_BETWEEN_EPISODES if base is None else base
    jitter = SLEEP_JITTER if jitter is None else jitter
    if base <= 0:
        return 0.0
    spread = max(0.0, min(jitter, 1.0))
    return max(0.05, random.uniform(base * (1 - spread), base * (1 + spread)))


def _request_gap() -> float:
    """Seconds yt-dlp waits between its own requests within one fetch.

    Without it a single episode fires its metadata and caption requests back
    to back, so a polite gap *between* episodes still brackets a burst.
    """
    try:
        return float(os.environ.get("QS_DOWNLOAD_SLEEP_S", "1"))
    except ValueError:
        return 1.0


def _is_rate_limit(exc: Exception) -> bool:
    s = str(exc).lower()
    return "429" in s or "rate" in s or "too many requests" in s


_SERVER_SIDE = ("500", "502", "503", "504", "bad gateway",
                "service unavailable", "gateway time-out", "internal error")
_TRANSPORT = ("timed out", "timeout", "connection reset", "connection aborted",
              "temporary failure in name resolution", "connection refused",
              "remote end closed")


COOLDOWN_FILE = "youtube-cooldown.json"
BUDGET_FILE = "youtube-budget.json"


def _max_per_hour() -> int:
    return int(os.environ.get("QS_MAX_PER_HOUR", "30"))


def _max_per_day() -> int:
    return int(os.environ.get("QS_MAX_PER_DAY", "200"))


class BudgetExhausted(RuntimeError):
    """Today's allowance is spent. Not a limit - the point is never meeting one."""


def budget_path() -> Path:
    return ensure_root() / BUDGET_FILE


def _read_ledger() -> list:
    """Request timestamps from the last day, oldest first."""
    path = budget_path()
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return []
    cutoff = datetime.now() - timedelta(days=1)
    out = []
    for stamp in raw if isinstance(raw, list) else []:
        try:
            when = datetime.fromisoformat(stamp)
        except (TypeError, ValueError):
            continue
        if when > cutoff:
            out.append(when)
    return sorted(out)


def budget_state() -> dict:
    """What has been spent, and when the next request would be allowed.

    Density is what trips a limiter, not volume: the 429 that started this
    arrived at ~120 requests inside 25 minutes, well under the ~300/day that
    had been the working figure. Jitter fixed the cadence signature and did
    nothing about rate, which is what this is for.
    """
    now = datetime.now()
    ledger = _read_ledger()
    hour = [w for w in ledger if w > now - timedelta(hours=1)]
    per_hour, per_day = _max_per_hour(), _max_per_day()

    # A cap on its own is not spacing. Thirty an hour permits thirty inside
    # one minute and then an idle hour, which is precisely the shape that drew
    # the limit: ~120 requests in 25 minutes, far under the daily figure. So
    # the hourly allowance is also a minimum gap between requests, and the
    # requests come out evenly instead of in a burst.
    spacing = 3600.0 / per_hour if per_hour > 0 else 0.0
    wait = 0.0
    if ledger:
        wait = max(wait, spacing - (now - ledger[-1]).total_seconds())
    if len(hour) >= per_hour:
        # The window is full; a slot frees when its oldest entry ages out.
        wait = max(wait, (hour[0] + timedelta(hours=1) - now).total_seconds())
    return {
        "hour": len(hour), "hour_max": per_hour,
        "day": len(ledger), "day_max": per_day,
        "day_exhausted": len(ledger) >= per_day,
        "spacing_s": round(spacing, 1),
        "wait_s": round(max(0.0, wait), 1),
    }


def record_request(kind: str = "fetch"):
    """Count one request against the budget, for every session to see.

    Held under a lock and rewritten atomically: two sessions ingesting would
    otherwise each read the same ledger and each write back their own, and an
    undercount is the dangerous direction to be wrong in.
    """
    from palette_app.library import library_lock

    root = ensure_root()
    with library_lock(root):
        ledger = _read_ledger()
        ledger.append(datetime.now())
        # Full precision: the spacing is arithmetic on these, and rounding
        # to the second puts up to a second of error into every gap.
        payload = json.dumps([w.isoformat() for w in ledger])
        tmp = root / (BUDGET_FILE + ".tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, root / BUDGET_FILE)


def await_slot(quiet: bool = False, kind: str = "fetch"):
    """Wait for the budget to allow one more request, or refuse for today.

    The hourly cap is spacing: at 30/hour a request is allowed about every two
    minutes, and waiting for one is the normal case during a long ingest. The
    daily cap is not something to sleep through - it raises, the run stops,
    and ingest is resumable so tomorrow costs only the remainder.

    The wait is jittered for the same reason the old pause was: requests at an
    exact interval are the clearest automation signature a client can emit,
    and a budget enforced to the second would reintroduce the metronome the
    jitter was added to remove.
    """
    state = budget_state()
    if state["day_exhausted"]:
        raise BudgetExhausted(
            f"today's budget is spent ({state['day']}/{state['day_max']} "
            f"requests in 24h). Ingest is resumable, so tomorrow costs only "
            f"the remainder. QS_MAX_PER_DAY raises it.")
    if state["wait_s"] > 0:
        wait = _pause(state["wait_s"], SLEEP_JITTER / 4)
        _log(quiet, f"budget: {state['hour']}/{state['hour_max']} this hour; "
                    f"waiting {wait:.0f}s")
        time.sleep(wait)
    record_request(kind)


def _cooldown_hours() -> float:
    return float(os.environ.get("QS_RATE_LIMIT_COOLDOWN_H", "6"))


def cooldown_path() -> Path:
    return ensure_root() / COOLDOWN_FILE


def cooldown_state() -> dict | None:
    """The active cooldown, or None. Expired ones read as None.

    Never deleted on read: a read path does not write, and knowing when the
    last limit happened is worth more than a tidy directory.
    """
    path = cooldown_path()
    if not path.exists():
        return None
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    until = state.get("until")
    if not until:
        return None
    try:
        remaining = datetime.fromisoformat(until) - datetime.now()
    except ValueError:
        return None
    if remaining.total_seconds() <= 0:
        return None
    return {**state, "remaining_s": round(remaining.total_seconds())}


def begin_cooldown(error: Exception, source_id: str = None) -> dict:
    """Record that YouTube refused us, and until when we will not ask again.

    Written to disk rather than kept in memory because every `qs ingest`
    otherwise starts with amnesia - nothing stopped a fresh run two minutes
    after a hard 429, and three sessions share this project without being able
    to see each other's requests. A file is the only thing all three can see.

    A Retry-After the server named wins over any figure we choose, because it
    is the server saying exactly what it wants.
    """
    named = retry_after_seconds(error)
    seconds = named if named else _cooldown_hours() * 3600
    state = {
        "at": datetime.now().isoformat(timespec="seconds"),
        "until": (datetime.now() + timedelta(seconds=seconds)).isoformat(
            timespec="seconds"),
        "seconds": round(seconds),
        "source": source_id,
        "reason": str(error)[:300],
        "from_retry_after": bool(named),
    }
    path = cooldown_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    return state


class InCooldown(RuntimeError):
    """YouTube asked us to stop recently enough that we still are."""


def check_cooldown():
    """Refuse before making a request. Called at the entry of anything that does.

    This is what turns "the run stopped" into "we do not go back yet". Set
    QS_IGNORE_COOLDOWN=1 to override, which is deliberately awkward: overriding
    it is asking to be limited harder.
    """
    if os.environ.get("QS_IGNORE_COOLDOWN") == "1":
        return None
    state = cooldown_state()
    if not state:
        return None
    minutes = state["remaining_s"] / 60.0
    raise InCooldown(
        f"YouTube rate-limited us at {state['at']} and we are not asking again "
        f"until {state['until']} ({minutes:.0f} min left). Ingest is resumable, "
        f"so waiting costs only time. Reason: {state.get('reason', '')[:120]}")


def failure_policy(exc: Exception) -> str:
    """Who the failure is about, which is what decides whether to knock again.

      client     the server is healthy and rationing *us* (429, and a 403 that
                 reads as a soft block). Stop. Retrying is self-incriminating.
      server     the server is unwell and wants us back (5xx). Backoff, retry.
      transport  a read timed out, a socket died. Carries no signal about
                 anyone. Retry a couple of times.
      other      unknown. Do not retry; an unrecognised error is not evidence
                 that trying again is safe.

    Classifying by *whose* problem it is, rather than by whether something
    errored, is the distinction the old single schedule was missing.
    """
    text = str(exc).lower()
    if _is_rate_limit(exc):
        return "client"
    if "403" in text or "forbidden" in text:
        return "client"
    if any(marker in text for marker in _SERVER_SIDE):
        return "server"
    if any(marker in text for marker in _TRANSPORT):
        return "transport"
    return "other"


_RETRY_AFTER = re.compile(r"retry[- ]after[:=]?\s*(\d+)", re.I)


def retry_after_seconds(exc: Exception) -> float | None:
    """A Retry-After the server named, if the error carried one.

    Authoritative when present - it beats any schedule we invent. Best effort:
    yt-dlp surfaces most failures as text, so this reads the message rather
    than a header object, and returns None whenever it cannot be sure.
    """
    match = _RETRY_AFTER.search(str(exc))
    if not match:
        return None
    try:
        seconds = float(match.group(1))
    except ValueError:
        return None
    return seconds if 0 < seconds <= 7 * 24 * 3600 else None


# ── YouTube ───────────────────────────────────────────────────────────────────

_YT_URL_ID = re.compile(
    r"(?:v=|/shorts/|youtu\.be/|/embed/|/live/)([A-Za-z0-9_-]{11})")


def youtube_id(url: str) -> str | None:
    """The 11-character video id inside a YouTube URL, or None.

    Parsed rather than resolved over the network: the id is the one thing a
    URL always carries, and asking YouTube for it would spend a request to
    learn something already in our hands.
    """
    text = (url or "").strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", text):
        return text
    match = _YT_URL_ID.search(text)
    return match.group(1) if match else None


def add_episode(url: str, source: dict, quiet: bool = False) -> dict:
    """Ingest one episode by URL, captions and metadata only.

    This exists for guests. Someone worth quoting often appears once on a show
    whose other three hundred episodes are irrelevant, and ingesting the whole
    channel to reach one conversation spends bandwidth, disk and rate limit for
    nothing. Goes through the same backoff path as a bulk ingest, so it obeys
    the same politeness.
    """
    ensure_root()
    check_cooldown()
    episode_id = youtube_id(url)
    if not episode_id:
        raise ValueError(f"not a YouTube video URL: {url!r}")

    # Looked up before anything is fetched, so the warning survives a failed
    # fetch and costs nothing. It warns and never refuses: a rejection is a
    # judgement, and the same video can legitimately be wanted for a different
    # person than the one it was declined for.
    from .rejections import rejection_for
    rejected = rejection_for(episode_id)

    ep_dir = episode_dir(source["id"], episode_id)
    existing = load_metadata(ep_dir)
    if existing and existing.get("status") != "captions_pending":
        return {"source": source["id"], "episode_id": episode_id,
                "title": existing.get("title"),
                "status": existing.get("status"),
                "already_had_it": True, "rate_limited": False,
                "rejected": rejected}

    entry = {"episode_id": episode_id,
             "url": f"https://www.youtube.com/watch?v={episode_id}",
             "title": "", "duration": None}
    await_slot(quiet)
    meta = _fetch_with_backoff(source, entry, quiet)
    row = {"source": source["id"], "episode_id": episode_id,
           "title": meta.get("title"),
           "show": meta.get("uploader"),
           "upload_date": meta.get("upload_date"),
           "duration": meta.get("duration"),
           "status": meta.get("status"),
           "already_had_it": False, "rate_limited": False,
           "rejected": rejected}
    # Reported at add time, while fixing it is still one `guest remove` away
    # rather than a re-transcription later.
    twin = find_duplicate(source, episode_id, meta.get("title"),
                          meta.get("duration"))
    if twin:
        row["possible_duplicate"] = twin
        _log(quiet, f"{episode_id}  looks like a duplicate of "
                    f"{twin['episode_id']} ({twin['title_ratio']} title match, "
                    f"same duration) - check before indexing")
    return row


DUPLICATE_TITLE_RATIO = 0.85
DUPLICATE_DURATION_S = 5.0


def find_duplicate(source: dict, episode_id: str, title: str,
                   duration) -> dict | None:
    """An episode already in this source that looks like the same recording.

    `--min-duration` keeps clip re-uploads out of a *channel* source, but an
    `episodes` source has no feed and no such filter, and two uploads of one
    talk carry different video ids - so nothing noticed. The case that
    prompted this was one 109-minute lecture added twice, where the copy that
    was kept turned out to be the one with no captions.

    Warned about, never refused. Two conference talks by the same person can
    legitimately run to the same second, and a guest source is hand-curated
    precisely because the judgement is a person's.
    """
    import difflib

    if not duration:
        return None
    base = ensure_root() / "episodes" / source["id"]
    if not base.is_dir():
        return None
    from .feedaudio import _norm_title

    wanted = _norm_title(title or "")
    for ep_dir in sorted(p for p in base.iterdir() if p.is_dir()):
        if ep_dir.name == episode_id:
            continue
        meta = load_metadata(ep_dir) or {}
        other = meta.get("duration")
        if not other or abs(float(other) - float(duration)) > DUPLICATE_DURATION_S:
            continue
        ratio = difflib.SequenceMatcher(
            None, wanted, _norm_title(meta.get("title") or "")).ratio()
        if ratio >= DUPLICATE_TITLE_RATIO:
            return {"episode_id": ep_dir.name, "title": meta.get("title"),
                    "status": meta.get("status"), "duration": other,
                    "title_ratio": round(ratio, 3)}
    return None


def find_episode(episode_id: str, source_id: str = None) -> tuple[dict, Path]:
    """Which source holds this episode, and where. Raises if it is not found.

    Searched rather than asked for, because the id is the thing a person has
    to hand - it is in the URL, in a search hit, in a clip's attribution -
    while the source it landed under is a detail of how it was added.
    """
    sources = ([registry.get_source(source_id)] if source_id
               else registry.list_sources())
    for source in sources:
        if not source:
            continue
        ep_dir = episode_dir(source["id"], episode_id)
        if ep_dir.is_dir():
            return source, ep_dir
    where = f" in source '{source_id}'" if source_id else ""
    raise LookupError(f"episode '{episode_id}' is not in the corpus{where}")


def remove_episode(episode_id: str, source_id: str = None,
                   apply: bool = False) -> dict:
    """Take one episode back out of the corpus. Reports before it acts.

    `qs guest add` could add an episode and nothing could remove one, so a
    hand-curated source had no hand-curated undo: correcting a single wrong
    pick meant dropping the whole source and re-adding everything else. The
    case that forced it was two uploads of one lecture under different video
    ids, where the copy that was kept turned out to have no captions.

    **Dry by default.** Without `apply` nothing is deleted and the report says
    what would be - deletions are worth seeing before they happen, and the
    audio in here can cost an hour of throttled fetching to replace.

    The index is cleaned in the same breath. Leaving the chunks behind would
    be worse than not removing the episode at all: search would go on
    returning quotes from something no longer on disk and no longer cuttable.
    """
    source, ep_dir = find_episode(episode_id, source_id)
    meta = load_metadata(ep_dir) or {}

    files = sorted(p for p in ep_dir.rglob("*") if p.is_file())
    audio = [p for p in files if p.stem == "audio"]
    report = {
        "episode_id": episode_id,
        "source": source["id"],
        "title": meta.get("title"),
        "status": meta.get("status"),
        "path": str(ep_dir),
        "files": len(files),
        "bytes": sum(p.stat().st_size for p in files),
        # Called out on its own because it is the expensive part: captions
        # are a few KB and refetch in seconds, audio is ~50 MB through a
        # throttled pipe.
        "stored_audio": [p.name for p in audio],
        "applied": bool(apply),
    }

    from .indexer import connect, db_path, forget_episode

    # Opening the database would create it, so a dry run against a corpus
    # that has never been indexed must not look. Read paths do not write.
    indexed = db_path().exists()

    if not apply:
        report["chunks_indexed"] = 0
        if indexed:
            con = connect()
            row = con.execute(
                "SELECT COUNT(*) FROM chunks WHERE episode_id=?",
                (episode_id,)).fetchone() if con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='chunks'").fetchone() else (0,)
            report["chunks_indexed"] = row[0]
            con.close()
        report["note"] = "nothing removed; pass --yes to apply"
        return report

    report["chunks_removed"] = 0
    if indexed:
        con = connect()
        report["chunks_removed"] = forget_episode(con, episode_id)
        con.commit()
        con.close()

    shutil.rmtree(ep_dir)
    return report


def _enumerate_episodes_source(source: dict) -> list[dict]:
    """What is already on disk for a hand-curated source.

    An `episodes` source has no feed to walk - it is whatever was added to it
    one URL at a time. Listing the contents lets `qs ingest` retry anything
    whose caption fetch failed, which is what it does for every other type.
    """
    out = []
    base = ensure_root() / "episodes" / source["id"]
    if not base.is_dir():
        return out
    for ep_dir in sorted(p for p in base.iterdir() if p.is_dir()):
        meta = load_metadata(ep_dir) or {}
        out.append({
            "episode_id": ep_dir.name,
            "url": meta.get("url") or
                   f"https://www.youtube.com/watch?v={ep_dir.name}",
            "title": meta.get("title") or "",
            "duration": meta.get("duration"),
        })
    return out


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
        "sleep_interval_requests": _request_gap(),
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
    # Before anything reaches the network. Enumerating a channel is itself a
    # request - a full walk of every video on it - and checking after that
    # meant a 317-video listing went out during an active cooldown. The
    # single most expensive call was the one the guard did not cover.
    check_cooldown()

    stype = source["type"]
    if stype in ("youtube_channel", "youtube_playlist"):
        # A full channel walk draws on the same allowance as a caption fetch,
        # and re-running an ingest re-walks it. Counted, not exempt.
        #
        # An exhausted budget here raises rather than returning a result:
        # nothing has been done yet, so there is no partial run to report and
        # the command should say it did not happen. Once the loop is running
        # the opposite is true, and it breaks with stopped: "budget" so what
        # it managed is not thrown away.
        await_slot(quiet, "enumerate")
    if not quiet:
        print(f"[{source['id']}] enumerating {stype}…", flush=True)

    if stype in ("youtube_channel", "youtube_playlist"):
        entries = _enumerate_youtube(source["url"], stype)
    elif stype == "rss":
        entries = _enumerate_rss(source["url"])
    elif stype == "episodes":
        entries = _enumerate_episodes_source(source)
    else:
        raise ValueError(f"unknown source type: {stype}")

    enumerated = len(entries)
    if min_duration is None:
        min_duration = registry.parse_duration(source.get("min_duration"))

    # Channels that publish clips alongside full episodes would otherwise put
    # the same words in the corpus twice. Unknown durations are kept, because
    # silently dropping a real episode is worse than admitting a clip.
    too_short = 0
    evidence = None
    if min_duration:
        kept, excluded_durations = [], []
        for entry in entries:
            duration = entry.get("duration")
            if duration is not None and duration < min_duration:
                too_short += 1
                excluded_durations.append(duration)
                continue
            kept.append(entry)
        entries = kept
        kept_durations = [e["duration"] for e in entries
                          if e.get("duration") is not None]
        # The threshold is recorded beside the counts because a --min-duration
        # passed for one run overrides the source's stored value, and evidence
        # measured against a different number than the one on file would
        # otherwise read as though it justified it.
        evidence = {
            "at": datetime.now().strftime("%Y-%m-%d"),
            "threshold": min_duration,
            "enumerated": enumerated,
            "excluded": too_short,
            "longest_excluded_s": (round(max(excluded_durations))
                                   if excluded_durations else None),
            "shortest_kept_s": (round(min(kept_durations))
                                if kept_durations else None),
        }
        registry.record_min_duration_evidence(source["id"], evidence)
        _log(quiet, f"[{source['id']}] {too_short} under {min_duration}s skipped, "
                    f"{len(entries)} to consider")

    result = {
        "source": source["id"],
        "enumerated": enumerated,
        "min_duration": min_duration,
        "min_duration_evidence": evidence,
        "too_short": too_short,
        "new": 0, "retried": 0, "skipped": 0, "failed": 0,
        "episodes": [],
        # None when the list was worked through; "rate_limited" when the run
        # gave up early. A caller that cannot tell the difference will treat a
        # throttled run as a complete one.
        "stopped": None,
        # Whether a rate limit was seen *at all*. `stopped` used to be read as
        # evidence that none had occurred, and it never meant that - it only
        # ever meant "no two consecutive". They are now separate answers, and
        # this is the one to check before believing a run was clean.
        "rate_limited": False,
    }

    processed = 0
    rate_limited_at = None
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
                # A podcast CDN wants you to have the file and has no limit
                # worth the name. The budget is for YouTube.
                _fetch_rss_episode(source, entry, quiet)
            else:
                await_slot(quiet)
                _fetch_with_backoff(source, entry, quiet)
            result["retried" if is_retry else "new"] += 1
            result["episodes"].append(entry["episode_id"])
        except BudgetExhausted as e:
            # Not a limit and not a failure: the allowance simply ran out.
            # Nothing to apologise for and nothing to cool down from.
            result["stopped"] = "budget"
            result["budget"] = budget_state()
            print(f"  stopping: {e}", flush=True)
            break
        except RateLimited as e:
            rate_limited_at = e
            result["failed"] += 1
            result.setdefault("failures", []).append(
                {"episode_id": entry["episode_id"], "error": str(e),
                 "kind": failure_kind(e, rate_limited=True)})
            print(f"  {entry['episode_id']}  FAILED: {e}", flush=True)
        except Exception as e:
            # An ordinary failure - no captions on this video, say - is not a
            # rate limit and must not stop the run.
            #
            # But a client-attributed one arriving by some route other than
            # _fetch_with_backoff still has to. Belt and braces: the rule is
            # "a 429 ends the run", not "a 429 raised as the right exception
            # class ends the run", and the second is the kind of rule that
            # holds until someone adds a code path.
            if failure_policy(e) == "client":
                rate_limited_at = e
            result["failed"] += 1
            result.setdefault("failures", []).append(
                {"episode_id": entry["episode_id"], "error": str(e),
                 "kind": failure_kind(e)})
            # failures always print, even with --quiet
            print(f"  {entry['episode_id']}  FAILED: {e}", flush=True)
        processed += 1

        if rate_limited_at is not None:
            # One is enough. The endpoint has said we are asking too often,
            # and the next request is the one that gets the limit extended.
            result["stopped"] = "rate_limited"
            result["rate_limited"] = True
            cooling = begin_cooldown(rate_limited_at, source.get("id"))
            result["cooldown"] = cooling
            print(f"  stopping: rate limited. Not asking YouTube again until "
                  f"{cooling['until']}. Ingest is resumable, so waiting costs "
                  f"only time.", flush=True)
            break

        if stype == "rss":
            time.sleep(_pause())        # the feed's own politeness pause

    return result


def _fetch_with_backoff(source: dict, entry: dict, quiet: bool):
    """Fetch one episode, retrying only what is worth retrying.

    The schedule is chosen by *whose* problem the failure is, because a 429
    and a 503 want opposite responses. A client-attributed refusal is never
    retried at all: it raises RateLimited on the first one, and the caller
    ends the run.

    Returns the metadata. It used to return `(metadata, hit_limit)`, where
    hit_limit said a rate limit had been survived - there is no such thing
    now, and a field that is always False is a field that will mislead
    someone.
    """
    remaining = None            # schedule, fixed by the first retryable error
    while True:
        try:
            return _fetch_youtube_episode(source, entry, quiet)
        except Exception as e:
            policy = failure_policy(e)
            if policy == "client":
                raise RateLimited(str(e)) from None
            schedule = {"server": BACKOFF_SCHEDULE,
                        "transport": TRANSPORT_BACKOFF}.get(policy)
            if schedule is None:
                raise               # unknown: not evidence that retrying is safe
            if remaining is None:
                remaining = list(schedule)
            if not remaining:
                raise
            wait = remaining.pop(0)
            _log(quiet, f"{policy} failure; retrying in {wait}s")
            time.sleep(wait)


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
