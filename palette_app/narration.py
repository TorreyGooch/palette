"""Reading the word manifest a cut leaves beside its audio.

`qs cut` writes <clip>.words.json holding every word with times relative to the
clip's own first sample. Until now nothing read it: the file was written, moved
between machines, and deleted with its item, but no part of the app ever opened
it. This is the consumer.

The point of reading it is that a beat can name a *range of words* rather than a
pair of seconds. Word indices survive re-cutting the same quote and mean
something to a person ("from 'lobster' to 'antidepressants'"); raw seconds do
neither.

Not every clip has one. `qs pull` stages audio without a manifest, and a hand
imported file never had one, so binding has to work without word timings and
simply fall back to the whole clip.
"""
import json
from pathlib import Path
from typing import Optional

AUDIO_TYPES = ("audio", "video")


def manifest_path(media_path: Path) -> Path:
    """The sidecar beside a clip. qs_cut_x_1_2.m4a -> qs_cut_x_1_2.words.json"""
    return media_path.with_suffix(".words.json")


def load_manifest(media_path: Path) -> Optional[dict]:
    """The clip's word manifest, or None when it never had one."""
    path = manifest_path(media_path)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    return data if isinstance(data, dict) else None


def word_list(manifest: Optional[dict]) -> list:
    if not manifest:
        return []
    words = manifest.get("words")
    return words if isinstance(words, list) else []


def clamp_range(count: int, start, end) -> tuple:
    """A usable inclusive [start, end] index pair over `count` words.

    Indices come from a client and are therefore wrong sooner or later: out of
    range, reversed, or absent. None means "from the beginning" / "to the end",
    so a beat that names neither bound is the whole clip.
    """
    if count <= 0:
        return (0, -1)
    lo = 0 if start is None else int(start)
    hi = count - 1 if end is None else int(end)
    if lo > hi:
        lo, hi = hi, lo
    return (max(0, min(lo, count - 1)), max(0, min(hi, count - 1)))


def with_gaps(words: list, lo: int, hi: int) -> list:
    """The words of a span, each carrying the silence before it.

    The pause between two words is arithmetic on times the manifest already
    holds, and it is the one thing prose cannot show you. A 20.3s beat was
    split into three by reading these: 700 ms after "hierarchies.", 360 ms
    after the second, and 1360 ms after "strangely," - the speaker holding
    before the payload. The split that looked obvious from *reading* the
    transcript was the weakest pause of the three.

    `gap_before` on the first word of a span is measured from the previous
    word in the clip, so a span that starts mid-sentence says so. It is None
    only at the very start of the clip, where there is nothing to measure
    from.
    """
    out = []
    for index in range(lo, hi + 1):
        word = words[index]
        gap = None
        if index > 0:
            previous = words[index - 1]
            gap = round(max(0.0, float(word.get("start", 0.0))
                            - float(previous.get("end", 0.0))), 3)
        out.append({
            "index": index,
            "word": str(word.get("word", "")),
            "start": round(float(word.get("start", 0.0)), 3),
            "end": round(float(word.get("end", 0.0)), 3),
            "gap_before": gap,
        })
    return out


def summarize(words: list, start=None, end=None) -> Optional[dict]:
    """Times and text for an inclusive word range, or None if there are none."""
    lo, hi = clamp_range(len(words), start, end)
    chosen = words[lo:hi + 1]
    if not chosen:
        return None
    first, last = chosen[0], chosen[-1]
    text = " ".join(str(w.get("word", "")) for w in chosen).strip()
    begin, finish = float(first.get("start", 0.0)), float(last.get("end", 0.0))
    return {
        "word_start": lo,
        "word_end": hi,
        "start": round(begin, 3),
        "end": round(finish, 3),
        "duration": round(max(0.0, finish - begin), 3),
        "text": text,
        "word_count": len(chosen),
        # What the page needs to show where the pauses are, and to let someone
        # split a beat on one. Only the span's words: a board of five beats
        # would otherwise carry five copies of every clip's full word list.
        "words": with_gaps(words, lo, hi),
    }


def bind(media_dir: Path, item: dict, word_start=None, word_end=None) -> dict:
    """What a beat needs in order to speak this clip.

    Always returns a binding: without a manifest there are no word times, so
    the beat gets the whole clip and says so via `precision`. The caller can
    then still lay the beat out in time, which is the thing that matters.
    """
    filename = (item or {}).get("filename")
    binding = {
        "item_id": (item or {}).get("id"),
        "filename": filename,
        "title": (item or {}).get("title"),
        "precision": "clip",
        "word_start": None,
        "word_end": None,
        "start": 0.0,
        "end": None,
        "duration": None,
        "text": None,
        "word_count": 0,
        "words": [],
        # How many words the clip has in all, as distinct from how many this
        # beat uses. Choosing indices needs the ceiling: word_count alone
        # reads as the total and is not, so "6 - 14 of 9" is what you get.
        "word_total": 0,
    }
    if not filename:
        return binding

    manifest = load_manifest(Path(media_dir) / filename)
    words = word_list(manifest)
    binding["word_total"] = len(words)
    if words:
        span = summarize(words, word_start, word_end)
        if span:
            binding.update(span)
            binding["precision"] = "word"
            return binding

    # No manifest, or an empty one: the beat is the whole clip.
    duration = (item or {}).get("duration")
    if manifest and manifest.get("duration") is not None:
        duration = manifest["duration"]
    if duration is not None:
        binding["end"] = round(float(duration), 3)
        binding["duration"] = round(float(duration), 3)
    if manifest:
        quote = (manifest.get("attribution") or {}).get("quote_text")
        if quote:
            binding["text"] = quote
    return binding


def lay_out(beats: list) -> list:
    """Absolute start/end for each beat, laid end to end on the narration.

    The words are the spine, so time comes from the audio wherever audio
    exists. A beat with no narration has no duration of its own to contribute
    and holds at the current position rather than inventing one.
    """
    out, cursor = [], 0.0
    for beat in beats:
        narration = beat.get("narration")
        length = (narration or {}).get("duration")
        row = {"id": beat.get("id"), "at": round(cursor, 3), "duration": length}
        if length:
            cursor = round(cursor + float(length), 3)
        row["until"] = round(cursor, 3)
        out.append(row)
    return out
