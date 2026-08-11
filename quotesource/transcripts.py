"""Caption parsing and transcript normalization.

Normalized format (one transcript.json per episode):
{
  "episode_id": ..., "source_id": ...,
  "transcript_source": "youtube_manual" | "youtube_auto" | "whisper",
  "language": "en", "model": null,
  "segments": [{"start": 1.23, "end": 4.56, "text": "..."}],
  "normalized_at": iso8601
}
Raw fetched captions are kept alongside so re-normalization never re-fetches.
"""
import json
import re
from datetime import datetime
from pathlib import Path


def parse_json3(raw: str) -> list[dict]:
    """YouTube json3 caption format -> segments."""
    data = json.loads(raw)
    segments = []
    for ev in data.get("events", []):
        segs = ev.get("segs")
        if not segs:
            continue
        text = "".join(s.get("utf8", "") for s in segs)
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            continue
        start = ev.get("tStartMs", 0) / 1000.0
        dur = ev.get("dDurationMs", 0) / 1000.0
        segments.append({
            "start": round(start, 3),
            "end": round(start + dur, 3),
            "text": text,
        })
    return _merge_adjacent_duplicates(segments)


_TS = re.compile(
    r"(?:(\d+):)?(\d{2}):(\d{2})[.,](\d{3})\s*-->\s*(?:(\d+):)?(\d{2}):(\d{2})[.,](\d{3})"
)


def parse_vtt(raw: str) -> list[dict]:
    """WebVTT (also tolerates SRT timestamps) -> segments."""
    segments = []
    cur = None
    for line in raw.splitlines():
        line = line.strip("﻿").rstrip()
        m = _TS.search(line)
        if m:
            if cur and cur["text"]:
                segments.append(cur)
            h1, m1, s1, ms1, h2, m2, s2, ms2 = m.groups()
            start = int(h1 or 0) * 3600 + int(m1) * 60 + int(s1) + int(ms1) / 1000
            end = int(h2 or 0) * 3600 + int(m2) * 60 + int(s2) + int(ms2) / 1000
            cur = {"start": round(start, 3), "end": round(end, 3), "text": ""}
            continue
        if cur is None:
            continue
        if not line or line == "WEBVTT" or line.startswith(("NOTE", "STYLE", "Kind:", "Language:")):
            continue
        if re.fullmatch(r"\d+", line):  # SRT cue number
            continue
        # strip inline tags like <c>, <00:00:01.000>, <b>
        text = re.sub(r"<[^>]+>", "", line)
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            cur["text"] = (cur["text"] + " " + text).strip()
    if cur and cur["text"]:
        segments.append(cur)
    return _merge_adjacent_duplicates(segments)


def _merge_adjacent_duplicates(segments: list[dict]) -> list[dict]:
    """YouTube auto-captions repeat rolling text; drop exact adjacent repeats."""
    out = []
    for seg in segments:
        if out and seg["text"] == out[-1]["text"]:
            out[-1]["end"] = max(out[-1]["end"], seg["end"])
            continue
        out.append(seg)
    return out


def normalize_captions(ep_dir: Path, episode_id: str, source_id: str,
                       transcript_source: str, language: str = "en") -> dict | None:
    """Find raw captions in ep_dir, parse, write transcript.json."""
    raw_files = sorted(ep_dir.glob("captions.raw*"))
    segments = None
    for f in raw_files:
        raw = f.read_text(encoding="utf-8", errors="replace")
        try:
            if f.suffix == ".json3" or raw.lstrip().startswith("{"):
                segments = parse_json3(raw)
            else:
                segments = parse_vtt(raw)
        except Exception:
            continue
        if segments:
            break
    if not segments:
        return None
    transcript = {
        "episode_id": episode_id,
        "source_id": source_id,
        "transcript_source": transcript_source,
        "language": language,
        "model": None,
        "segments": segments,
        "normalized_at": datetime.now().isoformat(),
    }
    (ep_dir / "transcript.json").write_text(
        json.dumps(transcript, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    return transcript
