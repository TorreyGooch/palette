"""Word-accurate clipping.

The transcript finds it; the waveform cuts it.

Caption-quality search locates a quote. We then whisper only a short window
around it (with word timestamps), and use energy analysis of the actual audio
to find true speech onset/offset — whisper's word times are attention-derived
and routinely drift 50-100ms, which at a phrase head clips the opening
consonant. Word timings locate the region; the waveform decides the cut.

Every cut clip gets a sidecar manifest with per-word timings relative to the
clip's own start, so downstream tools can place beats on specific words.
"""
import asyncio
import json
import os
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

from .search import _find_episode_dir, _ts_url

# Tunables. Tail needs more room than head: guillotined decay sounds wrong,
# while a little extra silence at the head is imperceptible.
HEAD_PAD_MS = float(os.environ.get("QS_CUT_HEAD_PAD_MS", "40"))
TAIL_PAD_MS = float(os.environ.get("QS_CUT_TAIL_PAD_MS", "80"))

# How far around the word-level boundary to hunt for the real speech edge.
SEARCH_MS = float(os.environ.get("QS_CUT_SEARCH_MS", "200"))

# A gap this long counts as a pause; shorter gaps are within-word breaks
# (stop consonants) and must not be mistaken for the end of the phrase.
MIN_SILENCE_MS = float(os.environ.get("QS_CUT_MIN_SILENCE_MS", "70"))

# Tail: how far past the requested end we may go looking for a real pause.
# Kept short on purpose — a generous value walks forward to the next pause
# and drags whole extra words in with it ("...blew me away. You know, it's
# so,"). Better to stop where asked and fade than to change the quote.
EXTEND_MS = float(os.environ.get("QS_CUT_EXTEND_MS", "300"))

# Head: how far forward we may snap to reach speech. Generous, because
# skipping leading silence only ever removes dead air — it cannot add words.
HEAD_SNAP_MS = float(os.environ.get("QS_CUT_HEAD_SNAP_MS", "1500"))

# Applied at the tail when no natural pause exists within reach, so a
# run-on speaker doesn't end in a hard truncation click.
FADE_MS = float(os.environ.get("QS_CUT_FADE_MS", "35"))

# Context given to whisper around the quote so word alignment near the
# boundaries has real audio on both sides (interior timings are the ones we
# trust; the padding exists to keep the edges out of the extrapolated zone).
WINDOW_PAD_S = float(os.environ.get("QS_CUT_WINDOW_PAD_S", "15"))

FRAME_MS = 10.0
ANALYSIS_SR = 16000
MIN_SPEECH_MS = 30.0    # sustained energy required to call something speech


# ── audio sourcing ────────────────────────────────────────────────────────────

def _source_media(episode_id: str, ep_dir: Path) -> Path:
    """Local full media for the episode: corpus audio, cache, or download."""
    from .pull import _cache_dir, _get_full_media

    corpus_audio = ep_dir / "audio.m4a"
    if corpus_audio.exists():
        return corpus_audio
    for ext in (".m4a", ".mp4"):
        cached = _cache_dir() / f"{episode_id}{ext}"
        if cached.exists():
            cached.touch()
            return cached

    meta = json.loads((ep_dir / "metadata.json").read_text(encoding="utf-8"))
    url = meta.get("audio_url") or meta.get("url", "")
    path = asyncio.run(_get_full_media(episode_id, url, "audio", ep_dir))
    if not path:
        raise RuntimeError(f"could not obtain audio for {episode_id}")
    return path


def _decode_window(src: Path, start: float, duration: float, dest: Path):
    """Decode a window to mono 16k wav for analysis and transcription."""
    subprocess.run(
        ["ffmpeg", "-y", "-ss", str(start), "-i", str(src), "-t", str(duration),
         "-ac", "1", "-ar", str(ANALYSIS_SR), "-vn", str(dest)],
        check=True, capture_output=True,
    )


# ── energy analysis ───────────────────────────────────────────────────────────

def _frame_rms(wav_path: Path):
    """Return (rms_per_frame, frame_seconds) for a mono 16k wav."""
    import wave

    import numpy as np

    with wave.open(str(wav_path), "rb") as w:
        sr = w.getframerate()
        raw = w.readframes(w.getnframes())
    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    hop = int(sr * FRAME_MS / 1000.0)
    if hop < 1 or samples.size < hop:
        return np.array([]), FRAME_MS / 1000.0
    n = samples.size // hop
    frames = samples[:n * hop].reshape(n, hop)
    rms = np.sqrt((frames ** 2).mean(axis=1) + 1e-12)
    return rms, hop / sr


def _threshold(rms):
    """Speech threshold from an estimated noise floor.

    Podcast SNR is generous, so a floor taken from the quiet decile plus a
    margin separates speech from room tone without any model.
    """
    import numpy as np

    floor = float(np.percentile(rms, 10))
    peak = float(np.percentile(rms, 95))
    # geometric-ish midpoint, biased toward the floor
    return max(floor * 3.0, floor + (peak - floor) * 0.08)


def _speech_runs(rms, frame_s: float):
    """Contiguous speech runs as (start_idx, end_idx_exclusive).

    Gaps shorter than MIN_SILENCE_MS are closed first, so the stop inside
    "black dot" doesn't read as the end of the phrase.
    """
    import numpy as np

    thresh = _threshold(rms)
    speech = rms > thresh
    min_sil = max(1, int((MIN_SILENCE_MS / 1000.0) / frame_s))
    min_run = max(1, int((MIN_SPEECH_MS / 1000.0) / frame_s))

    # close short gaps
    closed = speech.copy()
    i = 0
    while i < closed.size:
        if not closed[i]:
            j = i
            while j < closed.size and not closed[j]:
                j += 1
            if i > 0 and j < closed.size and (j - i) < min_sil:
                closed[i:j] = True
            i = j
        else:
            i += 1

    runs = []
    i = 0
    while i < closed.size:
        if closed[i]:
            j = i
            while j < closed.size and closed[j]:
                j += 1
            if (j - i) >= min_run:
                runs.append((i, j))
            i = j
        else:
            i += 1
    return runs


def snap_to_speech(rms, frame_s: float, region_start: float,
                   region_end: float) -> dict:
    """Find true speech onset/offset around a word-level region.

    Rather than taking the last loud frame in a fixed window — which just
    cuts wherever the window happens to fall when a speaker runs on — this
    locates the speech *run* containing each boundary and uses its true
    edges. When the run continues past EXTEND_MS beyond the requested end
    there is no natural pause to land on; that is reported rather than
    papered over, and the caller fades the tail.
    """
    import numpy as np

    if rms.size == 0:
        return {"onset": region_start, "offset": region_end,
                "head_clean": False, "tail_clean": False}

    runs = _speech_runs(rms, frame_s)
    if not runs:
        return {"onset": region_start, "offset": region_end,
                "head_clean": False, "tail_clean": False}

    search = SEARCH_MS / 1000.0
    extend = EXTEND_MS / 1000.0

    def t(i):
        return i * frame_s

    # ── onset: start of the run covering region_start, else the next run.
    # Searching out to `extend` matters: a requested start sitting in a long
    # pause would otherwise keep all that dead air.
    onset, head_clean = region_start, False
    for a, b in runs:
        if t(a) <= region_start + search and t(b) > region_start:
            onset, head_clean = t(a), True
            break
        if t(a) > region_start:
            if t(a) - region_start <= HEAD_SNAP_MS / 1000.0:
                onset, head_clean = t(a), True
            break

    # ── offset: end of the run covering region_end
    offset, tail_clean = region_end, False
    for a, b in runs:
        if t(a) <= region_end and t(b) >= region_end - search:
            if t(b) - region_end <= extend:
                offset, tail_clean = t(b), True
            else:
                # speaker runs on well past the quote: no pause to land on
                offset, tail_clean = region_end, False
            break
        if t(a) > region_end:
            offset, tail_clean = region_end, False
            break

    if offset <= onset:
        return {"onset": region_start, "offset": region_end,
                "head_clean": False, "tail_clean": False,
                "prev_end": None, "next_start": None}

    # Neighbouring speech, so the caller can cap its padding: the pause we
    # landed in may be shorter than the pad, and padding into the next
    # utterance is what "clean pause, 0 ms trailing silence" looks like.
    prev_end = next((t(b) for a, b in runs if t(b) <= onset + 1e-6), None)
    for a, b in runs:
        if t(b) <= onset + 1e-6:
            prev_end = t(b)
    next_start = next((t(a) for a, b in runs if t(a) >= offset - 1e-6), None)

    return {"onset": onset, "offset": offset,
            "head_clean": head_clean, "tail_clean": tail_clean,
            "prev_end": prev_end, "next_start": next_start}


def edge_report(rms, frame_s: float, cut_start: float, cut_end: float) -> dict:
    """Measurements for verifying a cut without listening to it."""
    import numpy as np

    if rms.size == 0:
        return {}
    thresh = _threshold(rms)

    def idx(t):
        return int(np.clip(round(t / frame_s), 0, rms.size - 1))

    # Exclude the frame straddling cut_end: it can contain the first sample
    # of the next utterance and report 0ms trailing silence for a clip whose
    # audio actually decays cleanly.
    a = idx(cut_start)
    b = max(a, int(cut_end / frame_s) - 1)
    seg = rms[a:b + 1]
    if seg.size == 0:
        return {}
    speech = seg > thresh
    lead = int(np.argmax(speech)) if speech.any() else seg.size
    trail = int(np.argmax(speech[::-1])) if speech.any() else seg.size
    return {
        # silence at each edge of the cut, in ms
        "lead_silence_ms": round(lead * frame_s * 1000, 1),
        "trail_silence_ms": round(trail * frame_s * 1000, 1),
        # energy right at the boundary relative to threshold: a high value at
        # the head means we likely started mid-consonant
        "head_energy_ratio": round(float(seg[0] / thresh), 2),
        "tail_energy_ratio": round(float(seg[-1] / thresh), 2),
    }


# ── whisper word timings ──────────────────────────────────────────────────────

def window_words(wav_path: Path, model_size: str | None = None) -> list[dict]:
    """Word timings for a short window, relative to the window start."""
    from .transcribe import _get_model

    model, (mname, device, compute) = _get_model(model_size)
    segments, _info = model.transcribe(
        str(wav_path), word_timestamps=True, vad_filter=False)
    words = []
    for seg in segments:
        for w in (getattr(seg, "words", None) or []):
            text = w.word.strip()
            if not text:
                continue
            words.append({
                "word": text,
                "start": round(w.start, 3),
                "end": round(w.end, 3),
            })
    return words


def word_map(episode_id: str, start: float, end: float,
             pad: float = 3.0, model_size: str | None = None) -> dict:
    """Word timings and the pauses between them, in absolute source time.

    Use this to choose cut boundaries: `qs cut` ends where you tell it, and
    the natural place to end is just before a pause. Caption timestamps are
    too coarse to see those.
    """
    ep_dir = _find_episode_dir(episode_id)
    if not ep_dir:
        raise FileNotFoundError(f"episode '{episode_id}' not found")
    src = _source_media(episode_id, ep_dir)

    win_start = max(0.0, start - pad)
    win_dur = (end + pad) - win_start

    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "w.wav"
        _decode_window(src, win_start, win_dur, wav)
        words = window_words(wav, model_size)

    out, prev_end = [], None
    for w in words:
        gap = None if prev_end is None else round(w["start"] - prev_end, 3)
        out.append({
            "word": w["word"],
            "start": round(win_start + w["start"], 3),
            "end": round(win_start + w["end"], 3),
            "gap_before": gap,
        })
        prev_end = w["end"]

    pauses = [
        {"after_word": out[i - 1]["word"], "at": out[i - 1]["end"],
         "gap": w["gap_before"]}
        for i, w in enumerate(out)
        if i > 0 and w["gap_before"] and w["gap_before"] >= 0.15
    ]
    return {"episode_id": episode_id, "window": [round(win_start, 3),
                                                 round(win_start + win_dur, 3)],
            "words": out, "pauses": pauses}


def _match_quote_region(words: list[dict], want_start: float,
                        want_end: float) -> tuple[float, float]:
    """Word-level region covering the requested span."""
    inside = [w for w in words if w["end"] > want_start and w["start"] < want_end]
    if not inside:
        return want_start, want_end
    return inside[0]["start"], inside[-1]["end"]


# ── main entry ────────────────────────────────────────────────────────────────

def cut_quote(episode_id: str, start: float, end: float,
              palette_name: str | None = None, person: str | None = None,
              model_size: str | None = None, stage: bool = True,
              progress_cb=None) -> dict:
    """Whisper a window, snap to the waveform, cut, manifest, stage."""
    from palette_app.config import get_library_path
    from palette_app.library import (
        load_library, save_library, new_palette, register_media_file,
    )

    def _progress(msg):
        if progress_cb:
            progress_cb(msg)

    ep_dir = _find_episode_dir(episode_id)
    if not ep_dir:
        raise FileNotFoundError(f"episode '{episode_id}' not found")
    meta = json.loads((ep_dir / "metadata.json").read_text(encoding="utf-8"))

    _progress("locating audio")
    src = _source_media(episode_id, ep_dir)

    win_start = max(0.0, start - WINDOW_PAD_S)
    win_dur = (end + WINDOW_PAD_S) - win_start

    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "window.wav"
        _progress(f"decoding {win_dur:.0f}s window")
        _decode_window(src, win_start, win_dur, wav)

        _progress("whisper word timings")
        words = window_words(wav, model_size)

        # requested span, expressed in window-relative time
        rel_start, rel_end = start - win_start, end - win_start
        w_start, w_end = _match_quote_region(words, rel_start, rel_end)

        _progress("snapping to speech boundaries")
        rms, frame_s = _frame_rms(wav)
        snap = snap_to_speech(rms, frame_s, w_start, w_end)
        onset, offset = snap["onset"], snap["offset"]

        # Pad, but never past the neighbouring speech: the pause we landed
        # in can be shorter than the pad.
        cut_start = max(0.0, onset - HEAD_PAD_MS / 1000.0)
        if snap.get("prev_end") is not None:
            cut_start = max(cut_start, snap["prev_end"] + 0.005)
        cut_end = min(win_dur, offset + TAIL_PAD_MS / 1000.0)
        if snap.get("next_start") is not None:
            # leave part of the pause intact rather than butting against the
            # next word, when the pause is shorter than the pad
            cut_end = min(cut_end, offset + (snap["next_start"] - offset) * 0.6)
        report = edge_report(rms, frame_s, cut_start, cut_end)
        report["head_clean"] = snap["head_clean"]
        report["tail_clean"] = snap["tail_clean"]

    duration = cut_end - cut_start
    abs_start = win_start + cut_start
    abs_end = win_start + cut_end

    # Words re-based to the clip's own start. A word must be essentially
    # whole to appear here: a half-audible word at either edge would put a
    # downstream visual beat on audio the clip does not contain.
    clip_words = []
    dropped = 0
    for w in words:
        if w["end"] <= cut_start or w["start"] >= cut_end:
            continue
        span = w["end"] - w["start"]
        inside = min(w["end"], cut_end) - max(w["start"], cut_start)
        if span > 0 and inside / span < 0.6:
            dropped += 1
            continue
        clip_words.append({
            "word": w["word"],
            "start": round(max(0.0, w["start"] - cut_start), 3),
            "end": round(min(cut_end, w["end"]) - cut_start, 3),
        })
    quote_text = " ".join(w["word"] for w in clip_words)

    lib_root = get_library_path()
    if not lib_root:
        raise RuntimeError("palette library not configured — run the app once")

    filename = f"qs_cut_{episode_id}_{int(abs_start)}_{int(abs_end)}.m4a"
    dest = lib_root / "media" / filename
    _progress("cutting clip")
    cmd = ["ffmpeg", "-y", "-ss", str(abs_start), "-i", str(src),
           "-t", str(duration)]
    if not report.get("tail_clean") and FADE_MS > 0:
        fade = FADE_MS / 1000.0
        cmd += ["-af", f"afade=t=out:st={max(0.0, duration - fade):.3f}:d={fade:.3f}"]
    cmd += ["-c:a", "aac", "-b:a", "192k", "-vn", str(dest)]
    subprocess.run(cmd, check=True, capture_output=True)

    url = meta.get("url", "")
    attribution = {
        "person": person,
        "show": meta.get("source_id"),
        "episode_id": episode_id,
        "episode_title": meta.get("title"),
        "episode_date": meta.get("upload_date"),
        "source_url_ts": _ts_url(url, abs_start),
        "range": [round(abs_start, 3), round(abs_end, 3)],
        "precision": "word_accurate",
        "quote_text": quote_text,
        "transcript_provenance": "whisper_window",
    }

    manifest = {
        "clip": filename,
        "duration": round(duration, 3),
        "created": datetime.now().isoformat(),
        "attribution": attribution,
        "words": clip_words,
        "cut_diagnostics": {
            **report,
            "words_dropped_at_edges": dropped,
            "tail_faded_ms": (FADE_MS if not report.get("tail_clean") else 0),
            "head_pad_ms": HEAD_PAD_MS,
            "tail_pad_ms": TAIL_PAD_MS,
            "word_region": [round(win_start + w_start, 3),
                            round(win_start + w_end, 3)],
            "snapped_to": [round(win_start + onset, 3),
                           round(win_start + offset, 3)],
        },
    }
    manifest_path = dest.with_suffix(".words.json")
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")

    result = {"filename": filename, "path": str(dest),
              "manifest": str(manifest_path), **manifest}

    if not stage:
        return result

    _progress("staging")
    quote_short = quote_text[:70].rstrip()
    title = f'“{quote_short}…” — {meta.get("title", episode_id)[:60]}'
    item = asyncio.run(register_media_file(lib_root, filename, title))

    lib = load_library(lib_root)
    it = next(i for i in lib["items"] if i["id"] == item["id"])
    it["url"] = _ts_url(url, abs_start)
    it["attribution"] = attribution
    it["manifest"] = manifest_path.name
    tags = ["quotesource", "word-cut"]
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

    result["item_id"] = it["id"]
    return result
