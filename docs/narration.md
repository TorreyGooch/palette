# Cutting quotes for narration

Word-accurate cuts, word timings and pauses, and the `.words.json` manifest every downstream step depends on. Shared context — the two machines, how work reaches them, and the rules every agent follows — is in `AGENTS.md`.

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
#   {"words":  [... {"index": 41, "word": "you", "start": 487.26,
#                    "end": 487.44, "gap_before": 0.2}],
#    "pauses": [... {"after_index": 40, "after_word": "away.",
#                    "next_index": 41, "next_word": "you",
#                    "at": 487.06, "gap": 0.2}]}
```

**This view is a preview of the cut, and now actually agrees with one.** It
had two silent disagreements. It ignored `audio_provenance.offset_s`, which
`cut` applies wherever it touches the audio — so on the **55 episodes carrying
a measured offset** (up to −61 s) it read a different passage than the cut
would take, and the alignment guard cannot catch that because the *cut* is
correctly offset and passes. And it defaulted to a narrower window than the
cut's, while whisper is stable for a given window and disagrees between window
widths — observed inserting a word a wider pass does not have, which shifts
every index after it.

So `pad` now defaults to `QS_CUT_WINDOW_PAD_S`, and the response reports
`window_pad_s`, `audio_offset_s`, `matches_cut_window` and `min_gap`. Pass a
smaller pad only for a cheaper look you do not intend to cut from —
`matches_cut_window` will say `false`. `min_gap` is reported because an empty
`pauses` list and "no pauses above this threshold" otherwise look identical.

**A pause names the words beside it by index, not only by spelling.** Selection
happens in seconds and everything durable stores positions, so a caller handed
only `after_word` has to search the list by string to get back to a number —
and a word that occurs twice in the window makes that ambiguous. `after_index`
is the word to end a cut on; `next_index` is the word to start the next one on.

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

**Ask the app for a clip's words rather than reading the file.**
`GET /api/items/{item_id}/words` returns the manifest's words *indexed*, with
the pauses between them already computed:

```bash
curl -s "http://127.0.0.1:7861/api/items/<item_id>/words?min_gap=0.15"
#   {"word_count": 42, "precision": "word_accurate",
#    "words":  [{"index": 0, "word": "the", "start": 0.02, "end": 0.1,
#                "gap_before": null}, ...],
#    "pauses": [{"after_index": 5, "after_word": "hierarchies.",
#                "next_index": 6, "next_word": "And", "gap": 0.7}, ...]}
```

`pauses` is usually the only field you need — it is the "where can this be
split" answer, and the indices in it go straight into a beat's `word_start` /
`word_end`. This reads the sidecar and nothing else: no audio, no whisper, no
GPU, no network, so it still answers for an episode whose media YouTube
refuses. A clip with no manifest (`qs pull` writes none) comes back
`precision: "clip"` and says so rather than returning an empty list that reads
as "no speech".

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
