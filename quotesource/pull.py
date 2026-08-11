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


async def _fetch_youtube_section(url: str, start: float, end: float,
                                 mode: str, dest: Path) -> bool:
    """Download a padded section, then cut to the exact range."""
    import yt_dlp

    from palette_app.api.media import extract_clip, _run

    sec_start = max(0.0, start - FETCH_PAD_S)
    sec_end = end + FETCH_PAD_S

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp) / ("section.mp4" if mode == "av" else "section.m4a")
        if mode == "av":
            fmt = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"
        else:
            fmt = "bestaudio[ext=m4a]/bestaudio/best"
        opts = {
            "quiet": True, "no_warnings": True, "noprogress": True,
            "format": fmt,
            "outtmpl": str(tmp_path),
            "download_ranges": yt_dlp.utils.download_range_func(
                None, [[sec_start, sec_end]]),
            "force_keyframes_at_cuts": True,
        }
        if mode == "av":
            opts["merge_output_format"] = "mp4"

        loop = asyncio.get_event_loop()

        def _dl():
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])

        await loop.run_in_executor(None, _dl)
        if not tmp_path.exists():
            cand = list(Path(tmp).glob("section*"))
            if not cand:
                return False
            tmp_path = cand[0]

        offset = start - sec_start
        dur = end - start
        if mode == "av":
            return await extract_clip(tmp_path, dest, offset, offset + dur)
        code, _, _ = await _run([
            "ffmpeg", "-y", "-ss", str(offset), "-i", str(tmp_path),
            "-t", str(dur), "-c:a", "aac", "-b:a", "128k", "-vn", str(dest),
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
         pad: float = 0.0) -> dict:
    """Snap, fetch, stage. Returns staged item JSON (with attribution)."""
    from palette_app.config import get_library_path
    from palette_app.library import (
        load_library, save_library, new_palette, register_media_file,
    )

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

    ext = "mp4" if mode == "av" else "m4a"
    filename = f"qs_{episode_id}_{int(s)}_{int(e)}.{ext}"
    dest = lib_root / "media" / filename

    url = meta.get("url", "")
    if meta.get("source_id") and url.startswith("http") and "youtube" in url:
        ok = asyncio.run(_fetch_youtube_section(url, s, e, mode, dest))
    elif mode == "audio" and meta.get("audio_url"):
        ok = asyncio.run(_fetch_rss_audio(meta["audio_url"], s, e, dest))
    elif meta.get("audio_url"):
        raise ValueError("av mode is not available for RSS episodes (audio only)")
    else:
        ok = asyncio.run(_fetch_youtube_section(url, s, e, mode, dest))
    if not ok:
        raise RuntimeError("segment fetch/cut failed")

    quote_short = snapped["quote_text"][:70].rstrip()
    title = f'“{quote_short}…” — {meta.get("title", episode_id)[:60]}'
    item = asyncio.run(register_media_file(lib_root, filename, title))

    # attach attribution + palette + tags directly
    lib = load_library(lib_root)
    it = next(i for i in lib["items"] if i["id"] == item["id"])
    it["url"] = _ts_url(url, s)
    it["attribution"] = {
        "person": person,
        "show": meta.get("source_id"),
        "episode_id": episode_id,
        "episode_title": meta.get("title"),
        "episode_date": meta.get("upload_date"),
        "source_url_ts": _ts_url(url, s),
        "range": [round(s, 3), round(e, 3)],
        "quote_text": snapped["quote_text"],
        "transcript_provenance": transcript.get("transcript_source"),
    }
    tags = ["quotesource"]
    if person:
        tags.append(person)
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
