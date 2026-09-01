"""qs pull — turn a verified hit into a staged item on a palette.

Range snapping: the requested range expands outward to sentence boundaries
read from the transcript (capped extension); sentence-level precision is the
contract, trimming happens downstream. Auto-captions often lack punctuation,
so the walk falls back to segment boundaries at the cap.

audio mode: fetches audio-only for the padded section (corpus audio store
arrives in Phase 2; on-demand fetch keeps pull working now).
av mode: fetches only the needed video section (yt-dlp section download),
then cuts to the exact snapped range. Full video is never retained.
"""
import asyncio
import json
import tempfile
from pathlib import Path

from .search import _find_episode_dir, _ts_url

SENTENCE_END = (".", "!", "?", "…", '."', '!"', '?"')
MAX_EXTENSION_S = 12.0
FETCH_PAD_S = 6.0


def snap_range(segments: list[dict], start: float, end: float) -> dict:
    idxs = [i for i, s in enumerate(segments)
            if s["end"] > start and s["start"] < end]
    if not idxs:
        raise ValueError(f"no transcript segments in range {start}-{end}")
    i0, i1 = idxs[0], idxs[-1]

    j = i0
    while (j > 0
           and not segments[j - 1]["text"].rstrip().endswith(SENTENCE_END)
           and segments[i0]["start"] - segments[j - 1]["start"] < MAX_EXTENSION_S):
        j -= 1
    k = i1
    while (k < len(segments) - 1
           and not segments[k]["text"].rstrip().endswith(SENTENCE_END)
           and segments[k]["end"] - segments[i1]["end"] < MAX_EXTENSION_S):
        k += 1

    return {
        "start": segments[j]["start"],
        "end": segments[k]["end"],
        "quote_text": " ".join(s["text"] for s in segments[j:k + 1]),
    }


ROUGH_PAD_S = 10.0

# Cap video resolution for pulls: full-file download must stay reasonable.
# Override with QS_PULL_MAX_HEIGHT.
def _max_height() -> int:
    import os

    return int(os.environ.get("QS_PULL_MAX_HEIGHT", "720"))


def _cache_dir() -> Path:
    from .paths import ensure_root

    d = ensure_root() / "cache"
    d.mkdir(exist_ok=True)
    return d


def _cache_gb() -> float:
    """Ceiling for cached video. Small on purpose: video is the luxury, and
    episode audio is kept elsewhere where eviction cannot reach it."""
    import os

    return float(os.environ.get("QS_PULL_CACHE_GB", "4"))


def _max_abr() -> int:
    """Audio bitrate ceiling, kbps. Speech into a video mix; 80 is plenty."""
    import os

    return int(os.environ.get("QS_AUDIO_MAX_ABR", "80"))


def _rate_limit():
    """Bytes/sec ceiling for downloads, or None. Accepts 2M, 500K, or bytes."""
    import os

    raw = (os.environ.get("QS_DOWNLOAD_RATE") or "").strip().upper()
    if not raw:
        return None
    mult = {"K": 1024, "M": 1024 ** 2, "G": 1024 ** 3}.get(raw[-1])
    try:
        return int(float(raw[:-1]) * mult) if mult else int(float(raw))
    except ValueError:
        return None


def _sleep_between() -> float:
    import os

    return float(os.environ.get("QS_DOWNLOAD_SLEEP_S", "1"))


def estimate_mb(duration_s: float | None, mode: str) -> float | None:
    """Roughly what pulling this episode will cost, before doing it.

    Worth showing rather than discovering: an av pull of a long episode is
    gigabytes to extract seconds, and audio is ~20x cheaper for the same
    quote.
    """
    if not duration_s:
        return None
    kbps = 2500 if mode == "av" else _max_abr()
    return duration_s * kbps / 8 / 1024


def _audio_store_gb() -> float:
    """Ceiling for kept episode audio.

    A YouTube pull caps its own bitrate and lands around 32 MB an episode. A
    podcast enclosure is whatever the publisher shipped — ~50 MB at 128 kbps,
    and archiving a back catalogue means hundreds at once, so the old 40 GB
    was reachable in a single afternoon. 80 GB is roughly 1,600 podcast
    episodes; still a number that fails loudly rather than quietly eating a
    shared disk."""
    import os

    return float(os.environ.get("QS_AUDIO_STORE_GB", "80"))


def stored_audio(ep_dir: Path) -> Path | None:
    """The episode's kept audio, whatever container it arrived in."""
    if ep_dir is None:
        return None
    for path in sorted(ep_dir.glob("audio.*")):
        if path.suffix.lower() in (".m4a", ".webm", ".opus", ".mp3", ".ogg"):
            return path
    return None


def _evict_audio_store():
    """Trim kept episode audio to the ceiling, oldest use first.

    Kept rather than cached because an evicted episode means a fresh
    full-episode download the next time anyone cuts from it — the exact
    traffic that draws rate limiting. At ~32 MB an episode the ceiling is
    generous enough that this rarely runs.
    """
    from .paths import data_root

    episodes = data_root() / "episodes"
    if not episodes.exists():
        return
    files = [p for src in episodes.iterdir() if src.is_dir()
             for ep in src.iterdir() if ep.is_dir()
             for p in [stored_audio(ep)] if p]

    cap = _audio_store_gb() * 1024 ** 3
    total = sum(f.stat().st_size for f in files)
    for f in sorted(files, key=lambda p: p.stat().st_atime):
        if total <= cap:
            break
        total -= f.stat().st_size
        f.unlink(missing_ok=True)


def _evict_cache():
    """Drop cached video beyond the size cap, least recently used first.

    Only video lives here now — audio is kept per episode instead. The two
    are budgeted apart on purpose: one 446 MB video pull is worth nine
    episodes of audio, and sharing a budget let a luxury evict the thing
    that is expensive to fetch again.
    """
    files = list(_cache_dir().glob("*"))
    cap = _cache_gb() * 1024 ** 3
    total = sum(f.stat().st_size for f in files)
    if total <= cap:
        return

    def priority(path):
        is_video = path.suffix.lower() in (".mp4", ".mkv", ".webm")
        return (0 if is_video else 1, path.stat().st_atime)

    for f in sorted(files, key=priority):
        if total <= cap:
            break
        total -= f.stat().st_size
        f.unlink(missing_ok=True)


async def _get_full_media(episode_id: str, url: str, mode: str,
                          ep_dir: Path) -> Path | None:
    """Return a local full copy of the episode's media for cutting.

    Audio is **kept**, beside the episode, not cached: at ~32 MB an episode
    the whole corpus is ~52 GB, while an eviction costs a fresh full-episode
    download the next time anyone cuts from it — the traffic that draws rate
    limiting. It is also exactly what `qs transcribe` consumes, so a pull
    pre-stages that episode for whisper.

    Video is still cached and evictable: ~20x the size, and only wanted when
    you actually need the picture.

    Full-file download is deliberate: yt-dlp section downloads go through
    ffmpeg's HTTP client, which YouTube throttles to a stall (measured 27+
    min for a 30s section). Video is capped at QS_PULL_MAX_HEIGHT (720).
    """
    import yt_dlp

    # Keyed by episode: an empty key would make every episode share one
    # entry and serve the wrong footage under the right attribution.
    if not episode_id:
        raise ValueError("episode_id is required for media fetch/caching")

    if mode == "audio":
        kept = stored_audio(ep_dir)
        if kept:
            kept.touch()  # bump LRU within the store's ceiling
            return kept

    ext = "mp4" if mode == "av" else "m4a"
    cached = _cache_dir() / f"{episode_id}.{ext}"
    if cached.exists():
        cached.touch()  # bump LRU
        return cached

    if mode == "av":
        h = _max_height()
        fmt = (f"bestvideo[height<=?{h}][ext=mp4]+bestaudio[ext=m4a]"
               f"/best[height<=?{h}][ext=mp4]/best")
    else:
        # "bestaudio" fetches the fattest stream YouTube offers, which for a
        # 2-hour episode is ~130 MB to extract ten seconds of speech. Whisper
        # resamples to 16 kHz mono regardless, and the output is a spoken
        # word clip, so a modest bitrate costs nothing audible and roughly
        # halves the transfer. Falls through if nothing matches the cap.
        # m4a preferred inside the cap so the kept file is the container
        # everything downstream already expects.
        abr = _max_abr()
        fmt = (f"bestaudio[abr<={abr}][ext=m4a]/bestaudio[abr<={abr}]"
               f"/bestaudio[ext=m4a]/bestaudio/best")

    # Audio lands beside the episode and stays; video goes to the cache.
    if mode == "audio" and ep_dir is not None:
        ep_dir.mkdir(parents=True, exist_ok=True)
        outtmpl = str(ep_dir / "audio.%(ext)s")
    else:
        outtmpl = str(_cache_dir() / f"{episode_id}.%(ext)s")

    opts = {
        "quiet": True, "no_warnings": True, "noprogress": True,
        "format": fmt,
        "outtmpl": outtmpl,
        # Downloading whole episodes back to back is what a rate limiter is
        # looking for. Capping throughput and pausing between requests keeps
        # the pattern boring; neither reduces total bytes.
        "retries": 5,
        "extractor_retries": 3,
        "sleep_interval_requests": _sleep_between(),
    }
    rate = _rate_limit()
    if rate:
        opts["ratelimit"] = rate
    if mode == "av":
        opts["merge_output_format"] = "mp4"

    # A pull downloads a whole episode - ~50 MB, or ~2.5 GB for video - and
    # used to spend nothing from the budget and never look at the cooldown.
    # So a rate limit could stop every ingest for six hours while this path
    # carried on fetching gigabytes from the same host. Interactive, so it
    # does not wait for hourly spacing; it does obey a standoff and a spent
    # day, which are the parts that mean anything.
    from .ingest import await_slot, check_cooldown, is_youtube

    if is_youtube(url):
        check_cooldown()
        await_slot(True, "pull", spacing=False)

    loop = asyncio.get_event_loop()

    def _dl():
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])

    await loop.run_in_executor(None, _dl)

    if mode == "audio" and ep_dir is not None:
        kept = stored_audio(ep_dir)
        if not kept:
            return None
        _evict_audio_store()
        return kept

    if not cached.exists():
        cand = [p for p in _cache_dir().glob(f"{episode_id}.*")
                if p.suffix in (".mp4", ".m4a", ".webm", ".mkv")]
        if not cand:
            return None
        cached = cand[0]
    _evict_cache()
    return cached


async def _fetch_youtube_section(url: str, start: float, end: float,
                                 mode: str, dest: Path,
                                 rough: bool = False,
                                 episode_id: str = "",
                                 ep_dir: Path | None = None) -> bool:
    from palette_app.api.media import extract_clip, _run

    pad = ROUGH_PAD_S if rough else FETCH_PAD_S
    s = max(0.0, start - pad) if rough else start
    e = end + pad if rough else end

    src = await _get_full_media(episode_id, url, mode, ep_dir)
    if not src:
        return False

    if mode == "av":
        if rough:
            # stream copy: starts at the keyframe at/before s — no re-encode
            code, _, _ = await _run([
                "ffmpeg", "-y", "-ss", str(s), "-i", str(src),
                "-t", str(e - s), "-c", "copy",
                "-avoid_negative_ts", "make_zero", str(dest),
            ])
            return code == 0 and dest.exists()
        return await extract_clip(src, dest, s, e)

    codec = ["-c", "copy"] if rough and src.suffix == ".m4a" \
        else ["-c:a", "aac", "-b:a", "128k"]
    code, _, _ = await _run([
        "ffmpeg", "-y", "-ss", str(s), "-i", str(src),
        "-t", str(e - s), *codec, "-vn", str(dest),
    ])
    return code == 0 and dest.exists()


async def _fetch_rss_audio(audio_url: str, start: float, end: float,
                           dest: Path) -> bool:
    from palette_app.api.media import _run

    code, _, _ = await _run([
        "ffmpeg", "-y", "-ss", str(start), "-i", audio_url,
        "-t", str(end - start), "-c:a", "aac", "-b:a", "128k", "-vn", str(dest),
    ])
    return code == 0 and dest.exists()


def pull(episode_id: str, start: float, end: float, mode: str = "audio",
         palette_name: str | None = None, person: str | None = None,
         pad: float = 0.0, rough: bool = False, progress_cb=None,
         stage: bool = True, outbox: str | None = None,
         keep_working_copy: bool = True) -> dict:
    """Snap, fetch, stage. Returns staged item JSON (with attribution).
    progress_cb, if given, receives stage strings as work proceeds.

    stage=False writes the clip and returns its attribution without adding it
    to this library. A remote caller adopting the clip into its own library
    wants that: staging here too would leave a second copy on a machine that
    never looks at it."""
    from palette_app.config import get_library_path
    from palette_app.library import (
        ensure_library, library_lock, load_library, save_library, new_palette,
        register_media_file,
    )

    def _progress(stage: str):
        if progress_cb:
            progress_cb(stage)

    _progress("reading transcript")
    ep_dir = _find_episode_dir(episode_id)
    if not ep_dir:
        raise FileNotFoundError(f"episode '{episode_id}' not found")
    meta = json.loads((ep_dir / "metadata.json").read_text(encoding="utf-8"))
    tpath = ep_dir / "transcript.json"
    if not tpath.exists():
        raise FileNotFoundError(f"episode '{episode_id}' has no transcript")
    transcript = json.loads(tpath.read_text(encoding="utf-8"))

    snapped = snap_range(transcript["segments"], start, end)
    s = max(0.0, snapped["start"] - pad)
    e = snapped["end"] + pad

    lib_root = get_library_path()
    if not lib_root:
        raise RuntimeError("palette library not configured — run the app once")
    ensure_library(lib_root)

    ext = "mp4" if mode == "av" else "m4a"
    filename = f"qs_{episode_id}_{int(s)}_{int(e)}.{ext}"
    dest = lib_root / "media" / filename

    url = meta.get("url", "")
    kind = "rough (stream copy)" if rough else "exact (re-encoded)"
    # Say what this will cost before spending it. A cached episode costs
    # nothing, which is worth knowing too — it changes whether you batch
    # several quotes from one episode or spread them across many.
    cached_already = any((_cache_dir() / f"{episode_id}.{x}").exists()
                         for x in ("m4a", "mp4"))
    if cached_already:
        _progress(f"using cached media ({e - s:.0f}s span, no download)")
    else:
        mb = estimate_mb(meta.get("duration"), mode)
        cost = f", ~{mb:.0f} MB full episode" if mb else ""
        _progress(f"downloading {mode} section, {kind} "
                  f"({e - s:.0f}s span{cost})")
    if meta.get("source_id") and url.startswith("http") and "youtube" in url:
        ok = asyncio.run(_fetch_youtube_section(
            url, s, e, mode, dest, rough, episode_id, ep_dir))
    elif mode == "audio" and meta.get("audio_url"):
        ok = asyncio.run(_fetch_rss_audio(meta["audio_url"], s, e, dest))
    elif meta.get("audio_url"):
        raise ValueError("av mode is not available for RSS episodes (audio only)")
    else:
        ok = asyncio.run(_fetch_youtube_section(
            url, s, e, mode, dest, rough, episode_id, ep_dir))
    if not ok:
        raise RuntimeError("segment fetch/cut failed")

    # Rough cuts start at the keyframe at/before s, so the file can begin well
    # before the quote. Record where the quote actually sits inside the file so
    # downstream trimming doesn't have to hunt for it.
    file_start, quote_offset, file_duration = s, 0.0, e - s
    try:
        from palette_app.api.media import probe as _probe

        info = asyncio.run(_probe(dest))
        if info.get("duration"):
            file_duration = round(info["duration"], 3)
            file_start = round(e - file_duration, 3)
            quote_offset = round(max(0.0, s - file_start), 3)
    except Exception:
        pass

    # Before the staging branch: a clip pulled for the video pipeline is
    # often one the remote caller adopts and this library discards.
    from .outbox import deliver as deliver_to_outbox

    delivered = deliver_to_outbox([dest], outbox)
    if delivered:
        _progress(f"delivered to outbox ({len(delivered)} files)")

    _progress("registering in library" if stage else "preparing hand-off")
    quote_short = snapped["quote_text"][:70].rstrip()
    title = f'“{quote_short}…” — {meta.get("title", episode_id)[:60]}'
    attribution = {
        "person": person,
        "show": meta.get("source_id"),
        "episode_id": episode_id,
        "episode_title": meta.get("title"),
        "episode_date": meta.get("upload_date"),
        "source_url_ts": _ts_url(url, s),
        "range": [round(s, 3), round(e, 3)],
        "precision": "rough" if rough else "exact",
        # where the quote lives inside the staged file
        "file_start": file_start,
        "file_duration": file_duration,
        "quote_offset": quote_offset,
        "quote_text": snapped["quote_text"],
        "transcript_provenance": transcript.get("transcript_source"),
    }
    tags = ["quotesource"]
    if person:
        tags.append(person)

    if not stage:
        # Shaped like a library item so an adopting caller needs no special
        # case, but nothing is written to this library.
        result = {"filename": filename, "path": str(dest), "title": title,
                  "url": _ts_url(url, s), "attribution": attribution,
                  "tags": tags, "staged": False, "outbox": delivered}
        # See cut_quote: unstaged files sit in media/ unreferenced. A remote
        # caller still has to fetch them, so keep by default; only drop the
        # duplicate when an outbox already holds the clip.
        if not keep_working_copy and delivered:
            dest.unlink(missing_ok=True)
            result["working_copy_removed"] = True
        return result

    item = asyncio.run(register_media_file(lib_root, filename, title))

    # attach attribution + palette + tags directly
    with library_lock(lib_root):
        lib = load_library(lib_root)
        it = next(i for i in lib["items"] if i["id"] == item["id"])
        it["url"] = _ts_url(url, s)
        it["attribution"] = attribution
        it["tags"] = sorted(set(it.get("tags", []) + tags))

        if palette_name:
            pal = next((p for p in lib["palettes"]
                        if p["name"].lower() == palette_name.lower()), None)
            if not pal:
                pal = new_palette(palette_name)
                lib["palettes"].append(pal)
            if pal["id"] not in it["palettes"]:
                it["palettes"].append(pal["id"])

        save_library(lib_root, lib)
    return it
