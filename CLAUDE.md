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
- `qs cut` is the one to use when a quote becomes narration. It whispers a
  ±15 s window around the range (no full-episode transcription needed),
  finds true speech onset/offset by energy analysis — never trusting
  whisper's word times as cut points, since they drift 50–100 ms and clip
  consonants — and cuts at onset−40 ms / offset+80 ms. Output is an `audio`
  library item plus a `<clip>.words.json` sidecar holding the attribution
  payload and per-word timings **relative to the clip's own start**. Read
  that manifest to place visual beats on specific words.
  Tunables: `QS_CUT_HEAD_PAD_MS`, `QS_CUT_TAIL_PAD_MS`, `QS_CUT_SEARCH_MS`,
  `QS_CUT_WINDOW_PAD_S`. `cut_diagnostics` in the manifest reports lead/trail
  silence and boundary energy so a cut can be checked without listening.
- Most transcripts are YouTube auto-captions until the whisper backfill
  (Phase 2) runs: expect missing punctuation and occasional mis-hearings;
  verify anything you plan to present as a quote.
- After any `qs ingest`, run `qs index` then `qs embed` (both incremental).
