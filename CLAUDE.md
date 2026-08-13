# palette + quotesource

Two things live in this repo:

1. **palette** — web app (`launch.bat`, http://127.0.0.1:7861) for a visual
   reference library: import images/video, carve clips (keyframe workflow),
   tag items, group them into *palettes* (named collections, many-to-many),
   export contact sheets / trimmed videos for diffusion workflows. Data lives
   in a user-chosen library folder (`config.json` → `library_path`);
   `library.json` inside it is the whole database.
2. **quotesource (`qs`)** — spoken-word sourcing layer. Maintains a corpus of
   YouTube channels / RSS podcasts with timestamped transcripts, exposes
   search primitives, and stages verified segments onto palettes with
   attribution.

## Read this first: it runs on two machines

**The corpus is not on this machine.** It lives on the GPU server with the
transcripts, the embeddings and whisper; the media library lives here, where
video can actually be scrubbed. They are joined over the tailnet, and only
small things cross: a search query is ~1 KB, a cut clip a few hundred KB.

| | desktop (here) | server (`100.102.79.115`) |
|---|---|---|
| media library, palettes, exports | **yes** | no |
| corpus, index, embeddings | no | **yes** (~1.9 GB) |
| whisper, embedding, ComfyUI | no | **yes** (RTX 3060) |
| the app you look at | `:7861` | `:7862`, API only |

Consequences worth internalising before you start:

- **`qs` on this machine cannot see the corpus.** The CLI reads it off disk
  and cannot proxy, so `qs search` here exits 1 telling you where it went.
  Use the app's endpoints (below), or run `qs` over ssh on the server.
- **The server is started on demand.** It is not a service — that box shares
  memory and GPU with generation. If search returns 503, the server is simply
  off; start it (below) rather than debugging.
- **Do not browse `:7862`.** It serves an explanation page, not the app.
  Anything staged there lands in the *server's* library, not yours.

## Starting a creative session

```bash
# 1. is the corpus server up?  (start|stop|restart|status)
curl -s -X POST http://127.0.0.1:7861/api/qs/server \
     -H 'Content-Type: application/json' -d '{"action":"start"}'
```

Or press **Start** on the Quotes page — same endpoint. The card shows what
it costs: app RAM, free machine memory, VRAM, GPU load.

**What it actually costs**, because the two figures are far apart: **~63 MB
idle**, but **~3.3 GB after a search** — the embedding model plus one pass
over the vectors. That is released about 10 minutes after the last search
(`QS_MODEL_IDLE_S`), so an idle server is cheap and a busy one is not. Stop
it outright before a long generation run if you want the memory back now.

```bash
# 2. confirm the corpus answers and see how much of it is embedded
curl -s 'http://127.0.0.1:7861/api/qs/status' | jq '.totals, .embeddings'

# 3. search, read context, cut - all through the local app, which forwards
curl -s 'http://127.0.0.1:7861/api/qs/search?q=<phrase>&limit=5'
```

Everything under `/api/qs/*` on **:7861** works whether the corpus is local
or remote; that is the whole point of the bridge. Prefer it to the CLI.

**What is in the corpus right now:** ~1,639 episodes — Jordan Peterson
(1,079) and Lex Fridman (560, full episodes only; clip re-uploads are
filtered out by `min_duration`). 385k chunks, 100% embedded with
`bge-large-en-v1.5`. Lex's transcripts are human-made and punctuated;
Peterson's are mostly YouTube auto-captions, so expect missing punctuation
and the occasional mis-hearing there.

## qs commands (all emit JSON; `--pretty` for humans)

**These run on the server**, over ssh or in a shell there:

```bash
ssh torrey@100.102.79.115
cd ~/palette && source ~/.palette-env && ./qs <command>
```

`source ~/.palette-env` is not optional: it carries `QS_EMBED_MODEL` and
`LD_LIBRARY_PATH`. Without it, search refuses (model mismatch) rather than
returning nonsense, and whisper silently drops to CPU.

| command | purpose |
|---|---|
| `qs sources list\|add\|remove` | registry (`sources.yaml` at the data root, hand-editable) |
| `qs ingest <source-id> [--limit N] [--min-duration 30m]` / `--all` | fetch episode metadata + captions; idempotent, throttled, resumable |
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

Errors: `{"error": msg}` on stderr, exit 1 (2 for usage).

**The `/api/qs/*` endpoints on :7861 are the primary interface, not a
mirror.** They cover `status`, `search`, `context`, `pull`, `cut`, `warm`,
`discard` and `server`, work identically whether the corpus is local or
remote, and — unlike the CLI — put the resulting clip in *this* machine's
library. Reach for the CLI only for corpus maintenance (`ingest`, `index`,
`embed`, `transcribe`) and for `words`, which has no endpoint yet.

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

**Filtering out clip re-uploads.** Channels that post excerpts alongside
full episodes (Lex Fridman: 855 videos, only 560 over 30 min) would put the
same words in the corpus twice, so search returns one moment under two
episode ids. `--min-duration 30m` on `sources add` stores the threshold on
the source, and every later `qs ingest` honours it without the flag;
passing it to `ingest` overrides for one run. Accepts `1800`, `30m`,
`1h30m`. Episodes whose duration is unknown are kept.

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
- Transcript quality is per-source, not uniform: Lex's are human-made and
  punctuated, Peterson's are mostly YouTube auto-captions with missing
  punctuation and occasional mis-hearings. 34 episodes have no captions at
  all and are queued as `needs_transcription`. Verify wording via
  `context` before presenting anything as a quote.
- After any `qs ingest`, run `qs index` then `qs embed` (both incremental).
- **Clip filenames truncate their bounds to whole seconds**, so two cuts a
  fraction of a second apart collide on one name and several library items
  can share a file. Deleting an item only removes the media when it was the
  last reference. Don't assume filename identifies an item.
- Search is ~2 s warm and grows with the corpus, since it is brute-force
  cosine over every vector. `--source` cuts the work roughly in proportion.
  The first search after a start also loads the model (~2 s extra), which is
  then released after 10 minutes idle.

**Things that will waste your time if you don't know them**
- A `503` from search means the corpus server is stopped, not broken.
- `qs` run on the desktop exits 1 and tells you the corpus is elsewhere.
  That is the guard working, not a misconfiguration.
- `/api/qs/status` reports `palette.version` and `palette.capabilities` for
  both ends (`remote_palette` when bridged). If something documented here
  is missing, check the two sides are on the same build — `.\deploy.ps1
  -Check` answers that in one command.
- `tail_clean: false` in a cut's diagnostics means no natural pause was
  within reach and the tail was faded. Usable, but moving the end to just
  before a real pause is better — that is what step 2 is for.

## Cutting quotes for narration

On the server whisper runs on the GPU and picks `large-v3` by itself. On a
CPU-only machine set `QS_WHISPER_MODEL=small` or better — `base` mis-hears
(it produced "black dog" for "black dot").

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

**From the desktop, the same three steps over the app** — and this is the
form to prefer, because the clip lands in *your* library rather than the
server's:

```bash
API=http://127.0.0.1:7861/api/qs
curl -s "$API/search?q=a+defeated+lobster+given+antidepressants&limit=5"
curl -s "$API/context?episode_id=PWasTAtR6Ns&start=477&end=490&window=10"

# cut is a job: POST returns a job_id, then poll until done
curl -s -X POST "$API/cut" -H 'Content-Type: application/json' -d '{
  "episode_id":"PWasTAtR6Ns","start":477.45,"end":487.15,
  "palette":"Narration","person":"Jordan Peterson"}'
curl -s "$API/pull/<job_id>"        # same polling endpoint for pull and cut
```

There is no `words` endpoint yet, so for step 2 either ssh over and run
`qs words`, or accept that a boundary guessed from caption timestamps
usually needs a second attempt.

**What the job does:** the server cuts the clip, your machine downloads it
with its `.words.json` manifest, registers it locally with attribution and
palette, then tells the server to discard its copy. Poll `stage` to follow
along; `item` holds the finished library entry. A 410 while polling means
the server restarted and the job is gone — start it again.

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
