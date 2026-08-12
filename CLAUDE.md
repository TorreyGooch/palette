# palette + quotesource

Two things live in this repo:

1. **palette** — local web app (`python run.py`, http://127.0.0.1:7861) for a
   visual reference library: import images/video, carve clips (keyframe
   workflow), tag items, group them into *palettes* (named collections,
   many-to-many), export contact sheets / trimmed videos for diffusion
   workflows. Data lives in a user-chosen library folder (`config.json` →
   `library_path`); `library.json` inside it is the whole database.
2. **quotesource (`qs`)** — headless spoken-word sourcing layer. Maintains a
   corpus of YouTube channels / RSS podcasts with timestamped transcripts,
   exposes search primitives, and stages verified segments onto palettes with
   attribution. CLI: `qs.bat …` from repo root (or `python -m quotesource …`).

## qs commands (all emit JSON; `--pretty` for humans)

| command | purpose |
|---|---|
| `qs sources list\|add\|remove` | registry (`sources.yaml` at the data root, hand-editable) |
| `qs ingest <source-id> [--limit N]` / `--all` | fetch episode metadata + captions; idempotent, throttled, resumable |
| `qs episodes <source-id>` | per-episode transcript status |
| `qs status` | corpus totals, index size, embedding coverage, disk |
| `qs index [--rebuild]` | chunk + FTS index; incremental (transcript-hash keyed) |
| `qs embed [--limit N] [--reset]` | embedding batch job; resumable |
| `qs grep "<fts5 query>"` | keyword search (BM25). Phrases `"like this"`, `OR`, `NOT`, `prefix*` |
| `qs search "<query>"` | semantic search (meaning, not words) |
| `qs context <ep> <ts> [--window s]` / `--range a b` | raw transcript around a point — verify quotes here |
| `qs episode-info <ep>` | full metadata + transcript stats |
| `qs transcribe <ep>` / `--batch [--source id] [--limit N]` | whisper backfill; resumable, disk-floor guarded |
| `qs pull <ep> --range a b --mode audio\|av [--rough] [--palette P] [--person X] [--pad s]` | fetch + stage onto a palette |
| `qs words <ep> --range a b [--pad s]` | word timings + pauses; use to pick cut boundaries |
| `qs cut <ep> --range a b [--palette P] [--person X] [--model m] [--no-stage]` | word-accurate audio clip + per-word manifest |

Shared filters on grep/search: `--source <id>`, `--person <name>` (matches
source `people` lists and episode title/description), `--after/--before
YYYY-MM-DD`, `--limit N`.

Hit shape: `{episode_id, source_id, start, end, text, score, episode_title,
upload_date, url, url_ts}`. `qs search` JSON wraps hits with `coverage`
(fraction of chunks embedded — treat <1.0 as "results may be incomplete").

Errors: `{"error": msg}` on stderr, exit 1 (2 for usage). Web UI mirror of
search/context/pull lives on the Quotes page (`/api/qs/*` endpoints).

## Investigation patterns

**Fuzzy recall** ("X said something about Y somewhere"):
1. `qs search "<paraphrase>" --person X` — try 2–3 phrasings; scores are
   cosine (~0.5–0.9), compare within a result set, not across queries.
2. `qs grep '<distinctive rare terms>' --person X` in parallel — auto-caption
   transcripts miss words, so grep and search are complementary, not ranked.
3. For each candidate: `qs context <ep> <start> --window 20` and read — the
   chunk text is ~70 words; the actual quote often spans chunk borders.
4. Only after reading context: `qs pull <ep> --range <start> <end> --mode av
   --palette "<board>" --person "X"`.

**Exact-quote hunting**: `qs grep '"the exact phrase"'` first (note inner
quotes for FTS5 phrase match). If it misses (caption wording drift), fall
back to `qs search` with the phrase — then verify wording via `qs context`
before quoting anywhere.

**Notes for agents**
- Ranges you pass to `pull` are snapped *outward* to sentence boundaries
  (capped ~12 s each way) and the staged item's `attribution.range` /
  `attribution.quote_text` record what was actually cut. Don't pre-pad.
- `--rough` (fast, default in the UI) stream-copies: no re-encode, original
  quality, but the file starts at the preceding keyframe — up to ~20 s
  before the quote. `attribution.quote_offset` says where the quote begins
  inside the file; use it when trimming. Omit `--rough` for an exact cut
  that starts on the quote (slower, re-encoded).
- First `pull` from an episode downloads its full media (~1–3 min, capped
  at 720p) into an LRU cache; later pulls from the same episode take
  seconds. Once `qs transcribe` has run, audio pulls read the corpus audio
  store and need no network at all.
- Staged items land in `library.json` with `attribution` (person, show,
  episode, date, timestamped URL, quote text, transcript provenance), tags
  `quotesource` + person, type `audio` or `video`.
- `qs cut` is the one to use when a quote becomes narration. See
  "Cutting quotes for narration" below — it has the full workflow.
- Most transcripts are YouTube auto-captions until the whisper backfill
  (Phase 2) runs: expect missing punctuation and occasional mis-hearings;
  verify anything you plan to present as a quote.
- After any `qs ingest`, run `qs index` then `qs embed` (both incremental).

## Cutting quotes for narration

Use `small` or better — `base` mis-hears (it produced "black dog" for
"black dot"). Set it once: `QS_WHISPER_MODEL=small`.

**The workflow, in order. Step 2 is the one that is easy to skip and
should not be.**

```bash
# 1. find the quote (caption-quality search is enough to locate it)
qs search "a defeated lobster given antidepressants" --limit 5 --pretty

# 2. look at the real word timings and pauses before choosing boundaries
qs words PWasTAtR6Ns --range 477 490 --pretty
#    477.42  if
#    ...
#    486.86  away.
#    487.44  You                  <== PAUSE 240ms

# 3. cut ending just before a pause
qs cut PWasTAtR6Ns --range 477.45 487.15 \
    --palette "Narration" --person "Jordan Peterson" --pretty
```

Why step 2 matters: `qs cut` ends where you tell it. It will extend only
`QS_CUT_EXTEND_MS` (300 ms) looking for a pause, then stop and fade. Pick
an end that already sits just before a real pause and the tail is clean;
guess from caption timestamps and you get a faded run-on, or a trailing
fragment like "You know, it's so,". Caption timestamps are far too coarse
to see pauses — that is what `qs words` is for.

**Reading the output.** `head_clean` / `tail_clean` are the two that
matter. `tail_clean: false` means no natural pause existed and the tail
was faded (`tail_faded_ms`) — usable, but a real pause is better, so
consider moving the end. `words_dropped_at_edges > 0` means a partial
word was excluded from the manifest; check the quote still reads whole.
`lead_silence_ms` much above the head pad means dead air.

**Tunables** (all env vars): `QS_CUT_HEAD_PAD_MS` (40),
`QS_CUT_TAIL_PAD_MS` (80), `QS_CUT_SEARCH_MS` (200),
`QS_CUT_MIN_SILENCE_MS` (70, what counts as a pause),
`QS_CUT_EXTEND_MS` (300, tail reach), `QS_CUT_HEAD_SNAP_MS` (1500, how
far forward the head may snap to skip dead air), `QS_CUT_FADE_MS` (35),
`QS_CUT_WINDOW_PAD_S` (15, whisper context).

### The manifest

`qs cut` writes `<clip>.words.json` beside the audio. This is the
downstream contract — it is what lets visual beats land on specific words.

```jsonc
{
  "clip": "qs_cut_<ep>_<start>_<end>.m4a",
  "duration": 10.34,                     // seconds
  "created": "2026-08-12T09:46:30",
  "attribution": {
    "person": "Jordan Peterson",
    "show": "jordanpeterson",            // source id in sources.yaml
    "episode_id": "7InNdewQwwc",
    "episode_title": "...",
    "episode_date": "20240619",          // YYYYMMDD
    "source_url_ts": "https://...&t=225s",
    "range": [225.32, 235.66],           // absolute seconds in the episode
    "precision": "word_accurate",
    "quote_text": "...",                 // exactly the words in the clip
    "transcript_provenance": "whisper_window"
  },
  "words": [                             // TIMES ARE RELATIVE TO THE CLIP
    {"word": "the", "start": 0.02, "end": 0.1}
  ],
  "cut_diagnostics": { "head_clean": true, "tail_clean": true,
                       "tail_faded_ms": 0, "words_dropped_at_edges": 0,
                       "lead_silence_ms": 40.0, "trail_silence_ms": 80.0 }
}
```

**`words[].start` / `.end` are relative to the clip's own first sample
(0 = clip start), not to the source episode.** Use them directly against
the audio file. `attribution.range` is the only field in episode time.
Every word listed is fully present in the audio; partial words at the
edges are dropped rather than reported with times that run past the end.
