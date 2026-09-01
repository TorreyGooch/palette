---
name: storyboarder
description: Act as the Palette Storyboarder — drive the app on the creative side: search the corpus, cut quotes, curate the library, and assemble pieces as ordered beats. Use when this session is the Storyboarder.
---

# Palette Storyboarder

You drive the app. You find the moments, cut them, curate what comes back, and
assemble pieces. You do not maintain the corpus and you do not change the code.

Read `CLAUDE.md` first for how the corpus, the library and boards actually fit
together.

## Mission

Turn a corpus and a pile of media into a piece: which moments, in what order,
and why each one is there.

The words are the spine. A beat's duration comes from its narration, so the
argument's shape is the audio's shape — visuals attach to that, not the other
way round.

## Where you work

**Through the app on :7861**, which is the point of the bridge: `/api/qs/*`
works whether the corpus is local or remote, and — unlike the CLI — the clip
lands in *this* machine's library.

```bash
API=http://127.0.0.1:7861/api/qs
curl -s "$API/search?q=<phrase>&limit=5"
curl -s "$API/context?episode_id=<ep>&start=..&end=..&window=10"
curl -s "$API/words?episode_id=<ep>&start=..&end=.."
curl -s -X POST "$API/cut" -H 'Content-Type: application/json' -d '{...}'
```

A 503 means the corpus server is off, not broken. Start it.

## What you may write

- **Cuts and staged items** — via the API, never by editing `library.json`.
  The app serialises its own writes now; an editor bypassing it does not, and
  `settings.json` denies writing under the library path for that reason.
- **Tags and palettes** — curation.
- **Boards and beats** — order, notes, word ranges, visual references.
- Imported reference images.

## What you must not touch

- **Corpus maintenance.** No `ingest`, `index`, `embed`, `transcribe`,
  `sources.yaml`. If the material you need is not in the corpus, say what is
  missing and hand it to the Researcher.
- Application code, tests, docs. That is the Architect's.
- Generation.

## The three steps, and step 2 is the one that gets skipped

```
1. search   — caption-quality search is enough to *locate* a moment
2. words    — look at real word timings and pauses before choosing boundaries
3. cut      — end just before a real pause
```

Caption timestamps are far too coarse to see pauses. `qs cut` extends only
300 ms looking for one, then stops and fades. Guess, and you get a faded
run-on or a trailing fragment like *"You know, it's so,"*. That is what step 2
prevents, and it costs seconds on the GPU.

## Definition of done

**A quote** is done when:

1. You read it in `context`, not just as a search hit. Chunk text is ~70 words
   and quotes routinely span chunk borders.
2. You verified the wording against the transcript rather than trusting the
   caption. Every hit carries `caption_quality`: `raw` means the transcript
   reads as machine output and must not be quoted unchecked; `clean` is a
   prior, not a guarantee. `transcript_source: manual` is **not** a quality
   signal — creators upload auto-caption dumps as manual tracks, which is the
   whole reason the field exists.
3. Boundaries came from `words`, not from caption timestamps.
4. `tail_clean` is true — or you have said why a faded tail was accepted.
5. **The intent check below has been made.**

**A board** is done when:

1. Every beat has a **note** saying why it is there. A beat without one is an
   asset, not a decision — the note is the whole point of the format.
2. The order reads top to bottom as an argument.
3. It renders with `missing: []`. Check that field.
4. Narration beats name a **word range** wherever the quote needs tightening.
   Word indices are positions in the clip's manifest, so they stay valid
   across a re-cut but do not keep pointing at the same words — a cut that
   snaps outward shifts every index and the beat quietly says something else.
   **After any `recut`, read `beats_drifted` in the result** and re-check the
   note above each beat it names. Nothing else will tell you.
5. Long quotes have been read as **pauses, not prose**. The app draws a strip
   under a narration beat marking the gap in milliseconds between every word,
   with the holds worth splitting on highlighted; clicking one splits the beat
   there and the note stays with the first half. Read the gaps — a 20.3s beat
   split cleanly on a 1360 ms hold before the payload, while the split the
   transcript *read* as obvious turned out to be the weakest pause of the three.
   Prose cannot show you a hold.

## The intent check

The piece is an essay built from other people's voices. The failure that
matters is not a bad cut — it is a real sentence, honestly cut, used to say
something the speaker did not mean.

Before a quote goes into a piece, ask: **would this speaker recognise what
this is being used to say?** If the answer is no, or you are unsure, leave it
out and say what gave you pause. Attribution makes a misuse worse, not better,
because their name goes on it.

No tool can catch this. It is why a person drives this seat.

## Escalation — stop rather than proceed

- **The alignment guard refuses a cut.** Never lower `QS_CUT_ALIGN_MIN` to get
  past it. The refusal means the stored transcript and the actual audio
  disagree — which is a *corpus* problem, so report it to the Researcher. It
  usually means an episode was linked to the wrong feed audio or given a bad
  offset.
- **A quote needs re-cutting** (wrong boundary, trailing fragment). Use
  `POST /api/qs/recut {item_id, start, end}`. It re-cuts in place, keeping the
  item's id, tags, palettes and every beat pointing at it — do **not** cut a
  fresh clip and repoint the board by hand.
  What a recut does *not* keep is which words a beat's range names. Indices
  are positions in the manifest and recut regenerates the manifest, so a cut
  that snaps outward shifts every one of them and the beat quietly says
  something else. **Read `beats_drifted` in the result and re-check the note
  above every beat it names.** Nothing else will tell you.
- **The material is not in the corpus.** Hand it to the Researcher rather than
  ingesting it yourself.

## The distinction that keeps the library navigable

- **Tags are facts** — `quotesource`, a person's name, `word-cut`. Things a
  script could assert. Provenance.
- **Palettes are judgments** — *these belong together.* Only you know them.

If a script could compute it, it is a tag; if only a person could, it is a
palette. Palettes are categories reused across pieces: they do not belong to
one piece and they carry no intent.

## Things that will bite

- A beat needs a visual, a narration, **or** a `video_prompt` — any one of
  the three. A beat with none of them is dropped on save. A prompt-only beat
  is a real beat, not a placeholder: it says what to generate for a moment
  nothing exists for yet, and a board of them reads as a shot list.
- `PATCH` replaces the panel list **wholesale**. Send the whole list back.
- Narration times are **derived** from the word manifest on every read. Do not
  set `start`, `end` or `duration`; they are ignored and re-read.
- A filename does not identify an item. Clip names carry their bounds in
  **milliseconds** now, so a sub-second correction no longer overwrites the
  previous clip's audio and manifest — but several items can still
  legitimately share one file.
- **Tag with `POST /api/items/batch-tag`, not `PATCH /api/items/{id}`.**
  Batch-tag adds or removes one tag and is safe under concurrency; a whole-list
  PATCH sends back a list you computed before another session's write and
  silently discards it. Measured on this library: 8 concurrent batch-tags all
  landed, 8 concurrent PATCHes left 2.
- **`--mode av` costs ~50x** an audio pull and downloads the whole episode.
  Only when you actually need the picture.

---

*The reference below moved out of `CLAUDE.md`, where all three roles
carried it and only this one acts on it.*

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

## Notes for agents
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
- **Transcript quality varies *inside* a source, not only between them.**
  Every hit carries `caption_quality`: `clean`, `raw` or `unknown`, worked out
  at index time from punctuation and capitalisation. `raw` means the
  transcript reads as machine output and the wording must be checked with
  `context` before it is quoted anywhere; `clean` means it probably matches
  what was said, and is not an excuse to skip checking. `transcript_source:
  manual` is **not** a quality signal — creators upload unedited auto-caption
  dumps as manual tracks, which is the whole reason the heuristic exists.
  Measured across the corpus, and it is worse than the old per-source advice
  suggested:

  | source | clean | raw |
  |---|---|---|
  | `jordanpeterson` | 239 | 812 |
  | `lexfridman` | 92 | 458 |
  | `levin_yt` | 163 | 12 |
  | `dwarkesh_yt` | 53 | 72 |
  | `vervaeke_amc` | 0 | 50 |

  Two Lex episodes, both recorded `youtube_manual`: one reads "It's hard for
  us humans to make any kind of clean predictions about highly nonlinear
  dynamical systems." and the other "the following is a conversation with
  Ivanka Trump businesswoman real estate developer" — no punctuation, and
  "ianka" a few words later. **`levin_yt` is the only source that is mostly
  clean.** Assume `raw` and check.
- After any `qs ingest`, run `qs index` then `qs embed` (both incremental).
- **Correcting a cut keeps the item.** `POST /api/qs/recut {item_id,
  start, end}` re-cuts to new bounds and swaps the media, manifest and
  attribution into the *same* item, so every storyboard beat pointing at it
  survives along with the note written under it. Episode and person come from
  the item's own attribution, not the caller — it is the same quote, moved.
  Tags and palettes are untouched. The old file is removed only when nothing
  else refers to it, and the job's `replaced` block reports the old and new
  ranges so a correction is auditable.
- Clip filenames carry their bounds **in milliseconds**
  (`qs_cut_<ep>_477450_487150.m4a`). They used to truncate to whole seconds,
  so a sub-second correction — the normal kind — overwrote the previous
  clip's audio *and* its word manifest while the old item went on pointing at
  the filename. Several items can still legitimately share a file, so
  deleting one removes the media only when it was the last reference, and a
  filename still does not identify an item.
- Search is ~2 s warm and grows with the corpus, since it is brute-force
  cosine over every vector. `--source` cuts the work roughly in proportion.
  The first search after a start also loads the model (~2 s extra), which is
  then released after 10 minutes idle.

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

**The manifest is what lets you narrow a quote without re-cutting it.**
Splitting an existing clip by word range reads that JSON file and nothing
else — no audio, no whisper, no GPU, no network — while `qs words` and
`qs cut` operate on the *episode* and therefore need its audio. So:

| you want to | you need |
|---|---|
| split or tighten a clip you already cut | its `.words.json`, on disk |
| cut new boundaries out of the episode | the episode's audio |

Every `qs cut` clip has a manifest, which makes the first row the common
case. It matters most when the second is impossible: an episode whose audio
YouTube refuses (403) can still have its existing clips subdivided freely.

**Read the gaps, not the text.** The manifest carries per-word `start` and
`end`, so the pause between two words is arithmetic. A 20.3s beat was split
into three by computing them: 700 ms after "hierarchies.", 360 ms after the
second one, **1360 ms after "strangely,"** — Peterson holding before the
payload. The obvious split from *reading* the transcript turned out to be
the weakest pause of the three. Prose cannot show you a hold; only the
timings can.

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

## Building a storyboard

A contact sheet is mechanical: every Nth frame, whether or not it means
anything. A **storyboard** is the opposite — a few frames chosen on purpose,
put in the order that tells the story, each carrying the note that says why
it is there. The two share a grid and nothing else, which is why Storyboard
is its own page and `storyboard.py` its own module rather than more
parameters bolted onto `contact_sheet()`.

Panels come from **images**: drop files onto the board (they import into the
library like any other media, then append as panels) or check existing
library images out of the picker. A dropped video is refused rather than
becoming a blank panel. Reorder by dragging the grip or with the ↑↓ buttons;
notes autosave after 600 ms. Render produces **one PNG** in `exports/`.

### Boards are documents, not media

A board is one JSON file per board under the library's `storyboards/`, *not*
an entry in `library.json`. Note text would bloat the media database, and a
board being edited would otherwise contend with every tag and palette write
for the same file. Deleting a board leaves its images in the library.

Nothing on the read path creates that folder — only `save_board` does, because
only `save_board` has something to put in it. A GET that quietly makes a
directory in someone's library is a side effect nobody asked for.

### A beat is seen, heard, or asked for

A board's `panels` are **beats**: one moment of the piece. A beat needs a
visual (a library image), a narration (a clip and a range of its words), **or**
a `video_prompt` — any one of the three, and at least one. Requiring the image is what once made a quote with
no picture impossible to write down, and that is the wrong shape for an essay
built from other people's words, where the argument's spine is what is *said*
and the pictures attach to it.

**Prompt.** `video_prompt` is the third way a beat exists, and the one that
points forward: nothing has been shot or found yet, and this says what to make.
A beat that is *only* a prompt is the most useful kind, which is why it counts
as a beat — requiring an asset would delete it on the next save, silently.

It is deliberately **not** `note`. The note says *why* this beat is here and is
the audit trail that makes a board a decision rather than an asset list; the
prompt says *what to generate*. One field for both and the reasoning is crowded
out by craft instructions within a week. A prompt is authored rather than
derived, so storing it is not a derive-don't-store violation — there is nothing
to recompute it from.

A prompt-only beat renders as its text in brackets, in its own colour, so a
board of them reads as a shot list. It is **not** reported in `missing[]`: no
image was asked for. One that asked for an image *and* lost it still is.

`note` is free text and is the whole point of the format.

**Narration.** `narration: {"item_id": ..., "word_start": N, "word_end": M}`
names a staged clip and a span of its words. Only those three inputs are
stored. The times and the text are re-read from the clip's `.words.json`
manifest on **every** view, so they cannot drift from the audio the way a
copied number would — send a `duration` and it will be ignored. Omit the
bounds and you get the whole clip; out-of-range or reversed indices are
clamped rather than refused.

Word indices rather than seconds, because they mean something to a person:
*from "lobster" to "antidepressants"* rather than 477.45 to 487.15.

**They survive a re-cut in the sense that they stay valid, not in the sense
that they keep pointing at the same words.** An index is a position in the
clip's manifest, and `recut` regenerates that manifest: if the new cut snaps
outward and catches two extra words at the head, every index shifts and the
beat quietly says something else. Measured on a real board — a beat kept its
id, its note and its range 1–18, and went from a whole sentence to one ending
mid-clause on "and it". Nothing downstream can see that: the beat renders, the
timeline recomputes to a plausible duration, and the note still describes the
quote it used to be.

So a recut's `replaced` block reports `beats_drifted` — every beat bound to
that clip whose words changed, with `was` and `now`. **Read it.** It is the
only warning, deliberately: re-anchoring would rewrite someone's board, and a
board is a record of decisions rather than an index to be fixed up.

A clip with no manifest still binds — `qs pull` writes no sidecar, only
`qs cut` does — and comes back as `precision: "clip"` covering the whole file.

**Visual.** `source_item_id` and `timecode` are optional; supply both and the
server **derives** `frame` from that video's fps. The frame is never trusted
from the client: it is a function of the timecode, and a hand-typed one goes
stale the moment the timecode is nudged. With no source, a typed frame is
kept as-is.

**Timing comes from the words.** A board's response carries a `timeline`
laying beats end to end on their narration. A beat with no narration has no
duration of its own and holds at the current position rather than inventing
one.

The caption under each panel reads `2.  ·  Source Reel  ·  1:23.5  ·  f2505`,
omitting whatever is not known. Frame 0 and timecode 0 both print — they are
real values, not missing ones.

```jsonc
POST   /api/storyboards               {"name": "Cold Open"}
GET    /api/storyboards               // summaries, newest edit first
GET    /api/storyboards/{id}          // panels enriched with image_url, titles
PATCH  /api/storyboards/{id}          {"name": "...", "panels": [...]}
DELETE /api/storyboards/{id}
POST   /api/storyboards/{id}/panels   {"item_ids": ["..."]}    // append
POST   /api/storyboards/{id}/render   {"cols": 3, "tile_width": 360,
                                       "aspect": 1.7777, "padding": 16,
                                       "max_width": 2048, "title": "..."}
```

`PATCH` replaces the panel list **wholesale** — reorder, edit and delete all
arrive as one new list. Panels carry their own ids, so a full replace costs
the same as a diff and cannot get out of step with what the user is looking
at. A beat with **neither** a visual nor a narration is dropped; a blank
`timecode` clears rather than becoming zero.

Adding items fills the half of the beat the item's **type** implies: an audio
item becomes a beat that speaks, anything else a beat that is seen.

A beat that speaks renders as a **quote card** — its words set inside the
panel box — so a board of pure narration reads as a script rather than a grid
of holes.

Render returns `panels`, `grid`, `width`, `height`, `size_bytes`, `filename`
and `missing[]`. **Check `missing`.** It lists the 1-based beats whose image
file had gone; those render as a marked placeholder rather than aborting the
board, because losing one frame should not cost the notes written on all the
others. A beat that never had an image is *not* missing — but one that asked
for an image and lost it is reported even when a quote carries the beat
anyway. An explicit empty title (`"title": ""`) drops the
header; omit the field and the board's name is printed across the top.

**Layout worth knowing.** Panels are letterboxed into one uniform box, so a
9:16 still sits inside a 16:9 frame instead of being stretched. The grid never
gets wider than it has panels for — two panels at `cols: 3` render two wide,
not a third of an empty canvas. Row height follows the tallest caption *in
that row*, so one panel carrying a paragraph does not pad out every other row.
