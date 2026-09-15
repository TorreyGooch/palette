# Contact sheets and storyboards

Looking at a whole video, and assembling a piece as beats: fields, narration binding, timing, splitting and rendering. Shared context — the two machines, how work reaches them, and the rules every agent follows — is in `AGENTS.md`.

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
any of its texts, or generated candidates — at least one of them. Requiring the image is what once made a quote with
no picture impossible to write down, and that is the wrong shape for an essay
built from other people's words, where the argument's spine is what is *said*
and the pictures attach to it.

**A board says what the piece is; a beat says what is in front of the camera.**

| where | field | holds |
|---|---|---|
| board | `name` | what to call it |
| board | `description` | the whole video in plain language |
| board | `aspect` | the frame shape, width / height (`0.5625` is 9:16). The page draws panels in it and a render uses it unless the request names another. Absent means 16:9, which is what every older board rendered as |
| board | *(a whole-video prompt)* | **not built** — waiting on the video model that will consume it |
| beat | `note` | why this beat is here — the audit trail |
| beat | `image_prompt` | what is in this shot, and how it is shot |
| beat | `video_prompt` | how this shot moves; authored last |

A beat briefly had a `description` of its own, split from `image_prompt` on
the argument that plain language survives a change of model and a prompt does
not. Three beats of real writing broke it: the split holds for *how a thing is
shot* and collapses for *what is in it*. "Single lobster on wet dark rock"
became "single lobster on wet black basalt", and all the prompt added was
styling — the subject got written twice.

The better reading is that the thing wanting a plain-language account was
never the shot; it was the **piece**. So the description moved up to the board,
where it belongs and where it outlives everything under it: shots get recut,
prompts get rewritten for a new model, references get regenerated, and what
the video is about does not move.

What the merge knowingly gives up: rewriting a beat's prompt for a new model
now takes the plain-language account of that shot with it. Weighed and
accepted. `note` stayed separate and survived the same test — "the argument is
about mechanism, so look at it the way a biologist would" is not the same kind
of sentence as anything you would hand a model.

**A beat's old `description` is folded into its `image_prompt` on read**,
description first, and never dropped: on a beat with no picture yet it *is*
the thinking, and it is the part nobody could reconstruct. Migrated on read
rather than by a script, because a board is content and rewriting someone's
files to suit a schema change is the worse trade.

**The order of work** runs down that table. Say what the piece is, then per
beat: write the image prompt, read it before it is spent, generate references,
choose one. The video prompt is written last, because that format depends on
everything else being decided. The app scaffolds the video first and iterates
on the inputs after.

**A beat exists if it is seen, heard, asked for, or has candidates** — any
one of them. A beat written only as a sentence about what
should happen is the earliest and most useful kind, and dropping it on save
would delete the thinking silently. That has happened once already.

**What the rendered PNG says.** Under the title, the board's `description`;
under each panel, that beat's **video prompt** — what happens in the shot —
and the one-line caption. The note and the image prompt are not printed. The
render used to print both in full, and a board of real writing became a wall
of text that buried its pictures; they stay on the page, where the work
happens, and the PNG hands over what the video should do. A beat that speaks
and has no picture still shows its quote. A beat with no picture but with any
writing or references reads "nothing shot yet" and is **not** reported in
`missing[]`: no image was lost. One that asked for an image *and* lost it
still is. An explicit empty title drops the description with it.

**Candidates.** `candidates` holds generated references that have not been
chosen between; `item_id` holds the one that was. A beat may have several and
select none, which is the normal state after an unattended generation run —
**generating never selects.** Choosing is a judgement, and it stays with a
person unless someone explicitly asks otherwise. Selecting one leaves the
others listed, so a choice can be reconsidered without spending the GPU again.

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

**A beat carried on by the next one runs until that one starts.** A beat on
its own is measured first word to last. Split, the pause between the halves
belonged to neither and fell out of the timeline — a 20.3s beat split at a
1.36s hold became 6.96 + 11.98. Video is generated from these times and the
audio is laid back alongside afterwards, so the global timing is what has to
be right, and it was a second and a half short at exactly the speaker's hold.
When the next beat continues the same clip at the next word, this one now ends
where that one begins and reports the silence it absorbed as
`narration.hold_s`; the halves add up to the whole and the pause stays on the
picture already up. Derived on read, so boards split before this are timed
correctly too. Beats on different clips, or with words skipped between them,
keep their own length.

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
POST   /api/storyboards/{id}/panels/{beat}/split  {"at_word": N}  // additive
POST   /api/storyboards/{id}/render   {"cols": 3, "tile_width": 360,
                                       "aspect": 1.7777, "padding": 16,
                                       "max_width": 2048, "title": "..."}
```

`PATCH` replaces the panel list **wholesale** — reorder, edit and delete all
arrive as one new list. Panels carry their own ids, so a full replace costs
the same as a diff and cannot get out of step with what the user is looking
at. A beat with **neither** a visual nor a narration is dropped; a blank
`timecode` clears rather than becoming zero.

**A `PATCH` never erases candidates it does not mention.** References attach
minutes after a page loaded the board, and until it reloads every autosave
carries that beat's old list — so typing a note while the GPU rendered used to
erase the images it had just made. Stored candidates a payload leaves out are
kept; a beat the payload leaves out is still deleted, because that is a
decision rather than a list the page had not heard about. Nothing removes a
candidate today; if that is ever wanted it needs its own additive call.

**Every board write holds `library_lock` and is atomic.** `save_board` was a
plain overwrite with no lock, so a generate attaching references and an
autosave landing together could lose one write or leave half a file. It now
shares `write_json_atomic` with `save_library`.

**Splitting a beat.** A long quote is often several shots, and the shape for
that already existed: beats can bind one clip with back-to-back word ranges,
each with its own prompts, references and duration. `split` makes that one
call. `at_word` is the index of the word that **starts** the new beat, which is
inserted directly after on the same clip; it must fall strictly inside the
beat's range, and a beat with no narration or no word manifest is refused.
Everything written on the original — note, prompts, chosen image, candidates —
stays on the first half, since guessing which half a sentence was about is
worse than a blank to be written. On the page, click **any word** in the
strip under a beat's audio — not only one after a pause, since a shot changes
where the picture needs it to; consecutive beats on one quote are marked
**continues N**, and the earlier one's duration includes any pause before the
next.

A split resolves "the whole clip" to explicit indices, so a later recut that
adds words does not stretch the halves; `beats_drifted` reports that case as
for any beat.

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

**Layout worth knowing.** Panels are letterboxed into one uniform box in the
board's `aspect`, so an image of another shape sits inside it instead of being
stretched - a 3:4 reference on a 9:16 board gets bars above and below. The grid never
gets wider than it has panels for — two panels at `cols: 3` render two wide,
not a third of an empty canvas. Row height follows the tallest caption *in
that row*, so one panel carrying a paragraph does not pad out every other row.
