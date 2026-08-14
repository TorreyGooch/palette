# palette + quotesource

Two things live in this repo:

1. **palette** — web app (`launch.bat`, http://127.0.0.1:7861) for a visual
   reference library: import images/video, carve clips (keyframe workflow),
   tag items, group them into *palettes* (named collections, many-to-many),
   export contact sheets (single, or a paged series covering a whole video)
   and trimmed videos for diffusion workflows. Data lives
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

The `./qs` wrapper finds an interpreter that actually has `faster-whisper`
rather than trusting `python3` — the system one can import quotesource
fine, so commands work right up until whisper reports itself "not
installed" when it is installed in another environment. `QS_PYTHON`
overrides the search.

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
| `qs pull <ep> --range a b [--mode av] [--rough] [--palette P] [--person X] [--pad s] [--outbox D]` | fetch + stage onto a palette. **audio by default**; `--mode av` costs ~50x more |
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
mirror.** They cover `status`, `search`, `words`, `context`, `pull`, `cut`,
`warm`, `discard` and `server`, work identically whether the corpus is local
or remote, and — unlike the CLI — put the resulting clip in *this* machine's
library. Reach for the CLI only for corpus maintenance: `ingest`, `index`,
`embed`, `transcribe`.

## Investigation patterns

**Fuzzy recall** ("X said something about Y somewhere"):
1. `qs search "<paraphrase>" --person X` — try 2–3 phrasings; scores are
   cosine (~0.5–0.9), compare within a result set, not across queries.
2. `qs grep '<distinctive rare terms>' --person X` in parallel — auto-caption
   transcripts miss words, so grep and search are complementary, not ranked.
3. For each candidate: `qs context <ep> <start> --window 20` and read — the
   chunk text is ~70 words; the actual quote often spans chunk borders.
4. Only after reading context: `qs pull <ep> --range <start> <end>
   --palette "<board>" --person "X"`. Add `--mode av` only if you need the
   picture — it downloads the whole episode at 720p.

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
- First `pull` from an episode downloads its full media; later pulls and
  cuts from that episode need no network at all, because the audio is kept
  beside the episode rather than cached. `--mode av` downloads video into a
  small evictable cache instead, so a repeat video pull may re-download.
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

**Bandwidth: a pull downloads the whole episode**

yt-dlp section downloads stall (measured 27+ min for a 30 s section), so the
whole episode is fetched and cut locally. That is what trips YouTube's rate
limiting, and it is why **audio is the default and `--mode av` is the
flag**:

| | one pull, 2 h episode | second cut, same episode |
|---|---|---|
| audio (default) | ~50 MB | **free** |
| `--mode av` | **~2.5 GB** | free while cached |

- **Only pass `--mode av` when you need the picture.** Same quote, ~50x the
  data. Narration needs sound.
- **Episode audio is kept, not cached** — it lands beside the episode as
  `audio.*` under a 40 GB ceiling (~1,250 episodes). Measured: a second cut
  from a stored episode moved 28 KB, not 88 MB.
- It is also what `qs transcribe` consumes, so pulling a quote pre-stages
  that episode for whisper.
- Video is cached separately and evictable (4 GB), so a video pull can never
  displace audio that is expensive to fetch again.
- The progress line says what a pull will cost before it spends it, and says
  when the episode is already local and costs nothing.
- Tunables: `QS_AUDIO_MAX_ABR` (80 kbps ceiling — resolves to ~49 kbps in
  practice; whisper resamples to 16 kHz anyway), `QS_AUDIO_STORE_GB` (80),
  `QS_PULL_CACHE_GB` (4, video), `QS_DOWNLOAD_RATE` (e.g. `2M`),
  `QS_DOWNLOAD_SLEEP_S` (1), `QS_PULL_MAX_HEIGHT` (720).
- If throttling starts: set `QS_DOWNLOAD_RATE=1M` and stop av pulls before
  reaching for anything cleverer.

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

Step 2 has an endpoint too — use it, don't guess from caption timestamps:

```bash
curl -s "$API/words?episode_id=PWasTAtR6Ns&start=477&end=490"
#   {"words": [... {"word": "you", "start": 487.26, "gap_before": 0.2}],
#    "pauses": [...]}
```

Whisper runs on that window only, so it is seconds on the GPU rather than a
transcription job. Pick an end that already sits just before a real pause.

**What the job does:** the server cuts the clip, your machine downloads it
with its `.words.json` manifest, registers it locally with attribution and
palette, then tells the server to discard its copy. Poll `stage` to follow
along; `item` holds the finished library entry. A 410 while polling means
the server restarted and the job is gone — start it again.

### Clips headed for the video pipeline

A cut is born on the machine that also runs the video model, so a clip
destined for generation would otherwise travel to the desktop and back to
move four directories. `--outbox` drops a copy in a staging folder on the
server as the clip is written:

```bash
qs cut <ep> --range a b --outbox ~/narration-outbox     # on the server
export QS_OUTBOX=~/narration-outbox                     # or set it once
```

Same field in the API body: `{"outbox": "~/narration-outbox"}`.

Off unless asked, and deliberately **not** the generator's own input
folder — that fills with everything a pipeline is fed and stops being
curatable. This is a tray you copy *from*. The discard step only touches
the server's `media/`, so an outbox copy survives the hand-off.

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

## Looking at a whole video as contact sheets

Export → Contact Sheet samples every Nth frame into a grid. Leave **rows
per sheet** blank and you get the old behaviour, one sheet however tall it
needs to be. Set it and the same sampling is paged into a series, which is
how you hand a full video to a session for visual or cinematic analysis.
Sampling does not change when you page: tile *k* is the same source frame
either way, so rows only decides where the grid breaks.

Renders land on disk under the library's `exports/`; nothing is downloaded
through the browser. A series gets **its own folder** so it can be handed
over whole — the result panel and the history row both offer its path:

```
exports/<clip>_sheet_<stamp>/     # ← the thing to point a session at
    index.json
    sheet_p001.jpg
    sheet_p002.jpg
    ...
```

A single sheet is one file and stays loose in `exports/`. If two series are
rendered inside the same second the later one gets `_2` appended rather
than merging into the first folder.

```jsonc
// POST /api/export/contact-sheet
{"item_id": "...", "every_n": 24, "cols": 4, "rows": 6,
 "tile_width": 320, "padding": 8, "order": "rows", "labels": true,
 "max_width": 2048, "start": null, "end": null}
```

Response carries `dir` / `dir_path` (the series folder, null for a single
sheet), `filenames` (every sheet, in order, relative to `exports/`),
`index`, `frames`, `sheet_count`, and a `sheets[]` entry per page with
`grid`, `width`, `height`, `first_frame` / `last_frame` and `start_time` /
`end_time`.

A series writes `index.json` into its own folder — source, fps, duration,
the sampling and layout used, and the same per-sheet ranges. **Read the
index first.** It is the only thing that says which seconds of the video a
given sheet covers; the JPEGs alone can't tell you. Sheet names inside it
are bare (`sheet_p001.jpg`), relative to the folder holding them, so the
folder survives being renamed or moved somewhere else entirely.

`labels: true` burns the absolute source frame number and timecode into
each tile (`1440  1:00.1`). Absolute, not per-sheet and not relative to
`start` — a label is only useful if you can seek to it. Without labels a
sheet is prettier; with them you can name a moment precisely, which is
usually what the analysis is for.

**Picking the numbers.** `every_n` is in frames, so at 24 fps `every_n: 24`
is one tile per second; the estimate under the button turns your settings
into tiles, sheets and seconds-per-sheet before you commit. Aim for sheets
that stay legible — 4×6 at 320 px is ~1.3 k × 1.1 k and around 100–200 KB,
which reads well and costs little to attach. A five-minute video at one
tile per 2.5 s is about a dozen sheets.
