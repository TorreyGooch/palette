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
    import os

    return float(os.environ.get("QS_PULL_CACHE_GB", "6"))


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


def _evict_cache():
    """Drop cached full files beyond the size cap, video first.

    Not pure LRU: a single video pull is ~20x the size of an audio one, so
    plain LRU lets one of them evict several episodes' audio — and each of
    those is a fresh full-episode download next time anyone cuts a quote
    from them. Video is the expensive thing to keep and the cheaper thing to
    lose, since it is only needed when you actually want the picture.
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

    Priority for flow: 1) the corpus audio store (audio mode — free after
    Phase 2 transcription has run), 2) the pull cache (instant repeat pulls
    from the same episode), 3) download full stream into the cache.

    Full-file download is deliberate: yt-dlp section downloads go through
    ffmpeg's HTTP client, which YouTube throttles to a stall (measured 27+
    min for a 30s section). Video is capped at QS_PULL_MAX_HEIGHT (720).
    """
    import yt_dlp

    # The cache is keyed by episode: an empty key would make every episode
    # share one entry and serve the wrong footage under the right attribution.
    if not episode_id:
        raise ValueError("episode_id is required for media fetch/caching")

    if mode == "audio" and ep_dir is not None:
        corpus_audio = ep_dir / "audio.m4a"
        if corpus_audio.exists():
            return corpus_audio

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
        abr = _max_abr()
        fmt = (f"bestaudio[abr<={abr}]/bestaudio[ext=m4a]"
               f"/bestaudio/best")
    opts = {
        "quiet": True, "no_warnings": True, "noprogress": True,
        "format": fmt,
        "outtmpl": str(_cache_dir() / f"{episode_id}.%(ext)s"),
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

    loop = asyncio.get_event_loop()

    def _dl():
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])

    await loop.run_in_executor(None, _dl)
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


def pull(episode_id: str, start: float, end: float, mode: str = "av",
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
        ensure_library, load_library, save_library, new_palette,
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
