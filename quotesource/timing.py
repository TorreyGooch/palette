"""Is this word list what a person said, or something whisper made up?

A cut's alignment score compares the whole span against the stored
transcript, and an average cannot see a local failure. Measured on a real
clip (X50zezLFWWI, a 420 s window): whisper looped between 6634.44 and
6663.16, writing one sentence three extra times with every word at zero
duration and then a single "word" spanning 19.56 s, over the one passage that
carried the speaker's thesis. The span still scored 0.8169 and passed. A recut
to a 118 s window transcribed the same audio correctly.

Those failures leave marks in the timings that a real recording never does:
words that take no time to say, one word lasting many seconds, and runs of
words repeated at zero duration. They are what `timing_flags` looks for.
`window_agreement` looks for the rest: the lowest agreement over short windows
rather than one number for the whole span.

Flags, never refusals. A person decides what a flag means; the cut still
happens.

Pure and standard-library only on purpose. `quotesource` computes this when a
clip is cut and `palette_app` when a clip's words are read, and `quotesource`
must not import `palette_app` - see the structural debt in AGENTS.md.
"""
import difflib
import re

# A real word takes longer than this to say. Whisper's loops put repeated
# words at exactly zero.
ZERO_S = 0.02

# Below this many zero-duration words, it is edge noise rather than a loop.
ZERO_MIN_COUNT = 3

# The longest genuine word on the corrected recut measured 1.42 s; the
# degenerate one 19.56 s. Four seconds leaves room for a drawn-out word.
LONG_WORD_S = 4.0

# A repeated run shorter than this is ordinary speech ("no, no, no").
REPEAT_MIN_WORDS = 5

# Sliding windows for local agreement: about ten seconds of speech each.
WINDOW_WORDS = 30
WINDOW_STEP = 10

# Flag a window where less than half of what whisper heard is in the
# transcript. Not yet measured across many clips: the value is always
# reported, so the threshold can be tuned against real ones.
WINDOW_FLAG = 0.5

_WORD = re.compile(r"[a-z0-9']+")


def _token(word) -> str:
    return " ".join(_WORD.findall(str(word or "").lower()))


def _tokens(text: str) -> list[str]:
    return _WORD.findall(str(text or "").lower())


def _duration(w: dict):
    try:
        return float(w["end"]) - float(w["start"])
    except (KeyError, TypeError, ValueError):
        return None


def timing_flags(words: list) -> dict:
    """Marks in a word list that a real recording does not leave.

    Returns `suspect` (any reason at all), human-readable `reasons`, and the
    evidence behind them by word index, so a person can go straight to it.
    """
    durations = [_duration(w) if isinstance(w, dict) else None for w in words]
    zero = [i for i, d in enumerate(durations) if d is not None and d <= ZERO_S]
    zero_set = set(zero)

    longest = None
    for i, d in enumerate(durations):
        if d is not None and (longest is None or d > longest["seconds"]):
            longest = {"index": i, "word": str(words[i].get("word", "")),
                       "seconds": round(d, 3)}

    # A run that repeats earlier words, at zero duration. First occurrences
    # are remembered by n-gram, so this is one pass however long the clip is.
    tokens = [_token(w.get("word")) if isinstance(w, dict) else "" for w in words]
    n = REPEAT_MIN_WORDS
    first = {}
    flagged = set()
    for j in range(len(tokens) - n + 1):
        gram = tuple(tokens[j:j + n])
        if not all(gram):
            continue
        earlier = first.get(gram)
        zero_here = sum(1 for k in range(j, j + n) if k in zero_set)
        if earlier is not None and earlier + n <= j and zero_here * 2 >= n:
            flagged.update(range(j, j + n))
        else:
            first.setdefault(gram, j)

    runs = []
    for i in sorted(flagged):
        if runs and runs[-1]["index"] + runs[-1]["length"] == i:
            runs[-1]["length"] += 1
        else:
            runs.append({"index": i, "length": 1})
    for run in runs:
        span = words[run["index"]:run["index"] + run["length"]]
        run["text"] = " ".join(str(w.get("word", "")) for w in span)

    reasons = []
    if len(zero) >= ZERO_MIN_COUNT:
        reasons.append(f"{len(zero)} words take no time to say "
                       f"(first at word {zero[0]})")
    if longest and longest["seconds"] > LONG_WORD_S:
        reasons.append(f"one word lasts {longest['seconds']:.1f}s "
                       f"({longest['word']!r} at word {longest['index']})")
    if runs:
        first_run = runs[0]
        reasons.append(f"{len(runs)} repeated run(s) of zero-duration words, "
                       f"first at word {first_run['index']}: "
                       f"{first_run['text'][:60]!r}")

    return {"suspect": bool(reasons), "reasons": reasons,
            "zero_duration_words": len(zero), "longest_word": longest,
            "repeated_runs": runs}


def window_agreement(words: list, expected_for, window: int = WINDOW_WORDS,
                     step: int = WINDOW_STEP) -> dict | None:
    """The lowest local agreement between what was heard and the transcript.

    `expected_for(start, end)` returns the transcript text over a span in the
    same time base as the words. It usually returns whole caption segments
    that merely overlap the span - more text than the window holds - so the
    score is the share of *heard* words found in order in that text. Extra
    context costs nothing; invented or repeated words cannot all be matched.

    None when the clip is shorter than one window: the whole-span score
    already covers it.
    """
    if len(words) < window:
        return None
    starts = list(range(0, len(words) - window + 1, step))
    if starts[-1] != len(words) - window:
        starts.append(len(words) - window)   # the tail is always examined

    worst = None
    for lo in starts:
        chunk = words[lo:lo + window]
        heard = [t for w in chunk for t in _tokens(w.get("word"))]
        if not heard:
            continue
        try:
            span = (float(chunk[0]["start"]), float(chunk[-1]["end"]))
        except (KeyError, TypeError, ValueError):
            continue
        expected = _tokens(expected_for(*span))
        if expected:
            matcher = difflib.SequenceMatcher(None, expected, heard, autojunk=False)
            matched = sum(b.size for b in matcher.get_matching_blocks())
            score = matched / len(heard)
        else:
            score = 0.0
        if worst is None or score < worst["min"]:
            worst = {"min": round(score, 4), "words": [lo, lo + window - 1],
                     "at": [round(span[0], 3), round(span[1], 3)]}
    if worst is None:
        return None
    worst["flagged"] = worst["min"] < WINDOW_FLAG
    worst["window_words"] = window
    return worst
