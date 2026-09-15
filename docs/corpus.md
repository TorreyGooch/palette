# The corpus: quotesource and `qs`

For whoever builds or runs `qs`: the commands, guests, investigation patterns, rate limits and the join between feed audio and captioned episodes. Shared context — the two machines, how work reaches them, and the rules every agent follows — is in `AGENTS.md`.

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
| `qs reject add\|list\|remove <url>... --reason "…" [--person X]` | record a candidate you looked at and declined. `guest add` reads it and **warns, never refuses** |
| `qs ingest <source-id> [--limit N] [--min-duration 30m]` / `--all` | fetch episode metadata + captions; idempotent, throttled, resumable |
| `qs guest add <url>... --person X` / `qs guest list` | add single episodes by URL, grouped by person |
| `qs guest remove <ep-id>... [--yes]` | take one episode back out. **Dry by default** — reports what it would delete; `--yes` applies |
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
upload_date, url, url_ts, caption_quality, audio_stored, duplicate_of}`.

**`duplicate_of`** names the same conversation under another source id, or
is null. Search otherwise returns one moment twice under two attributions
with nothing saying they are one thing — which happened to both roles on the
same day. Every member of a group points at the group's lowest episode id and
that canonical one holds null, so two hits are the same talk when either names
the other or both name the same third.

It is computed at index time, across *different* sources only (a channel
posting clips beside full episodes is `--min-duration`'s job), and recomputed
whole on every `qs index` — a stale link saying two different talks are one is
worse than no link. Matching needs the title to be ≥0.85 similar, the numbers
in the two titles to be identical ("Ep. 30" and "Ep. 33" are different
lectures) **and** the durations to agree within 90 s. That last is not
optional: of 108 real title matches, 6 disagreed on duration and all 6 were
genuinely different material — one at 3247 s against 1339 s. An episode of
unknown duration is never matched, because there is nothing to confirm the
guess with.

**It only sees what is indexed**, which is not the same as what is on disk.
An episode enters the index when it has a transcript, so an audio-only episode
awaiting whisper is invisible to the matcher. Measured on the first run: one
link found where a hand analysis of *metadata on disk* had found 108, because
176 of thoughtforms' 186 episodes have audio and no transcript. Both numbers
are right about different populations. For the Storyboarder this never bites —
anything a search can return is indexed by definition — but do not read the
count as a survey of the corpus.

**A null means "not computed" until the corpus has been linked at least once.**
`qs status` reports `index.duplicates.linked_at`, and while that is null every
`duplicate_of` on every hit is silence rather than a verdict — the column
exists from the migration, the links do not exist until `qs index` runs. Check
it before reading a null as "not a duplicate", the same way `coverage` is
checked before reading a thin result set as "nothing there".

**`audio_stored`
says whether cutting this quote needs the network**: `true` means the episode's
audio is already on disk and the cut costs nothing; `false` means the first cut
fetches the whole episode (~50 MB) and may be refused. `null` means the
question could not be answered.

It is a bool rather than `stored | fetchable | refused` on purpose.
`fetchable` is a prediction dressed as a fact — nothing knows an episode can be
fetched until it fetches it. `refused` is a fact about *an attempt*: a 403
decays, the audio never changed, and storing it as a property of the episode
is a verdict that goes stale silently. That is the same conflation that made
`words` report "audio not stored" for a CUDA failure, and **pipeline stage is
derived, never stored** already covers it. Evidence about attempts, if it is
ever wanted, belongs beside this and *dated* — an undated `refused` cannot be
aged by its reader; `403 on 2026-08-31` can. `qs search` JSON wraps hits with `coverage`
(fraction of chunks embedded — treat <1.0 as "results may be incomplete").

Errors: `{"error": msg}` on stderr, exit 1 (2 for usage).

**The `/api/qs/*` endpoints on :7861 are the primary interface, not a
mirror.** They cover `status`, `search` (semantic, or keyword with
`mode=grep`), `words`, `context`, `pull`, `cut`,
`recut`, `warm`, `discard` and `server`, work identically whether the corpus is local
or remote, and — unlike the CLI — put the resulting clip in *this* machine's
library. Reach for the CLI only for corpus maintenance: `ingest`, `index`,
`embed`, `transcribe`.

## Guests: one episode at a time

The people most worth quoting are often **guests**, not hosts. They appear once
on a show whose other three hundred episodes are irrelevant, and ingesting that
whole channel to reach one conversation spends bandwidth, disk and rate limit
for nothing.

```bash
qs guest add https://youtu.be/<id> https://youtu.be/<id2>     --person "John Vervaeke"
qs index && qs embed          # both incremental; search needs both
```

That creates (or reuses) a source of type **`episodes`** — `guest_john_vervaeke`
by default — with `people: [John Vervaeke]`. Grouping by *person* rather than by
show is the whole point: `_person_episode_filter` already treats every episode
of a source whose `people` list names someone as that person's, so
`--person "John Vervaeke"` finds these afterwards with no other change.

- An `episodes` source has **no URL and nothing to enumerate**. `qs ingest` on
  one only retries episodes already on disk whose caption fetch failed.
- The id is parsed out of the URL rather than resolved over the network, so a
  bad URL costs nothing. `watch?v=`, `youtu.be/`, `/shorts/`, `/embed/`,
  `/live/` and bare ids all work.
- Adding the same episode twice is free — it is skipped unless the previous
  attempt left it `captions_pending`.
- **Two uploads of one talk are a different problem**, since they carry
  different video ids and `--min-duration` does not apply to an `episodes`
  source. `add` now warns when an incoming episode matches one already there
  on duration (within 5s) *and* title (≥0.85), reporting `possible_duplicate`
  on the row. It warns and never refuses — two conference talks can
  legitimately run to the same second.
- **`qs guest remove` is the undo.** It reports before it acts: without
  `--yes` nothing is deleted and you see the path, file count, bytes, and
  whether the episode has `stored_audio` — which is the expensive part, since
  captions refetch in seconds and audio is ~50 MB through a throttled pipe.
  Applying also clears the episode's rows from the index, because search
  returning quotes from something no longer on disk would be worse than not
  removing it at all.
- It goes through the same backoff as a bulk ingest, so it inherits the jitter
  and reports `rate_limited` rather than opening a second unthrottled path.
- `uploader` records which show it came from, at no extra cost.

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

**A threshold records the evidence behind it.** `min_duration: 1800` is a
number with no argument attached, and the next person cannot tell a measured
choice from an oversight. So an ingest that applies one writes what it saw
back to `sources.yaml`:

```yaml
min_duration: 1800
min_duration_evidence:
  at: 2026-09-01
  threshold: 1800        # what it was measured against, which a one-run
  enumerated: 51         #   --min-duration override makes differ from above
  excluded: 1
  longest_excluded_s: 171
  shortest_kept_s: 3396
```

That reads as "51 items, one below the line, and it was a 171-second trailer",
and it is checkable. It is written at **ingest**, not at `sources add` — add
writes YAML and never touches the network, and gathering this there would mean
spending the request budget to describe a source nobody has fetched. It is
refreshed on every ingest, so if forty items later fall under a threshold that
once excluded one, the number has gone wrong for what the channel became and
the snapshot is what makes that visible.

Not a derive-don't-store violation: re-enumerating gives *today's* channel,
not the one the decision was made against, so recomputing answers a different
question.

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
  "Cutting quotes for narration" in `docs/narration.md` — it has the full workflow.
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
  `audio.*` under an 80 GB ceiling (~2,500 episodes; `QS_AUDIO_STORE_GB`,
  and see the tunables below). Measured: a second cut
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

**Staying anonymous, and knowing when to stop**

Every YouTube request this project makes is unauthenticated — no cookies, no
OAuth, no API key anywhere in the codebase. A rate limit therefore lands on an
IP and decays; no account is exposed. **Never add authentication to get past a
limit** (`--cookies-from-browser` and friends), never fake a browser user
agent, and **never enable yt-dlp impersonation** (`--impersonate`, which forges
a browser's TLS fingerprint and which recent yt-dlp versions now suggest in a
warning on caption fetches). All three convert a temporary IP annoyance into a
real identity attached to bulk downloading, and the third does it at the
transport layer, where nothing about this client is supposed to be a claim.
The only user agent sent anywhere is `quotesource/1.0`, to podcast CDNs, which
is honest self-identification rather than a disguise. When a limit blocks work the answer is to wait.

`qs ingest` throttles in two ways, and both matter:

- The pause between episodes is **jittered** (`SLEEP_BETWEEN_EPISODES` 2.0s,
  `SLEEP_JITTER` ±0.6), because requests spaced at an exact interval are the
  clearest automation signature a client can emit. `sleep_interval_requests`
  is also passed to yt-dlp so one episode's metadata and caption fetches are
  not fired back to back.
- **One rate limit ends the run, and is never knocked on twice.** RFC 6585:
  a 429 says *this client* has sent too many requests — the server is healthy
  and rationing you specifically, so retrying is the behaviour limiters
  escalate against. That is the opposite of a 503, where the server is unwell
  and does want you back. Retries are therefore chosen by **whose problem the
  failure is**:

  | policy | what | response |
  |---|---|---|
  | `client` | 429, and a 403 that reads as a soft block | **stop.** No retry, run over |
  | `server` | 502/503/504 | backoff `[30, 120]`, retry |
  | `transport` | read timeouts, connection resets | retry `[2, 10]` — carries no signal |
  | `other` | anything unrecognised | no retry; an unknown error is not evidence that asking again is safe |

- **A rate limit starts a cooldown that outlives the run.** Stopping a run is
  not the same as not going back: every `qs ingest` used to start with
  amnesia, so nothing prevented a fresh one two minutes after a hard 429.
  A `youtube-cooldown.json` at the data root records until when, and
  `ingest` and `guest add` refuse before making any request. Default 6h
  (`QS_RATE_LIMIT_COOLDOWN_H`); a `Retry-After` the server names wins over it.
  `QS_IGNORE_COOLDOWN=1` overrides, and is deliberately awkward — overriding
  it is asking to be limited harder. `qs status` shows whether one is active,
  which is the only way to know without grepping a log.

**Check `rate_limited`, not `stopped`, before believing a run was clean.**
They answer different questions: `stopped` says the run ended early,
`rate_limited` says a limit was seen at all. `stopped: null` was read as
evidence no limiting occurred and never meant that — under the old breaker it
only meant "no two consecutive". Ingest is resumable and skips what it already
has, so picking it up later costs only the remainder.

**Density, not volume, is what trips it.** A hard 429 arrived at roughly 120
requests inside 25 minutes, well under the ~300/day that had been the working
figure. Jitter fixed the *cadence* signature and does nothing about rate — so
there is now a **request budget**, shared on disk so two sessions cannot each
spend the whole allowance:

| knob | default | meaning |
|---|---|---|
| `QS_MAX_PER_HOUR` | 30 | also the **minimum gap**: 3600/30 ≈ one request every 2 min, jittered |
| `QS_MAX_PER_DAY` | 200 | a hard stop for the day |

The hourly figure is spacing, not just a ceiling — a bare cap would permit
thirty requests inside a minute and then an idle hour, which is the shape that
drew the limit. **A channel walk counts**: `qs ingest` re-enumerates the whole
channel each run, and that listing spends from the same allowance as a caption
fetch. RSS does not count — a podcast CDN wants you to have the file.

**The ledger counts requests, not episodes — it did not used to.** One episode
fetch was recorded as one request while it asked for four languages in both
manual and automatic form: up to eight caption downloads plus a metadata call.
A budget of 30/hour was therefore permitting a few hundred an hour, which is
how a limit arrived at what looked like a comfortably safe rate. The fetch now
asks what tracks exist (one request), then downloads the **one** track it will
actually use — so an episode costs 3, and a video with no English captions
costs 1 instead of drawing eight fruitless requests. That last case matters
beyond the arithmetic: `writeautomaticsub` for a language a video does not
natively carry is a request for an **on-demand auto-translation**, which is a
heavier server-side operation than handing over a stored track.

**A translation is not a track we can fetch, and is now recognised as such.**
YouTube stores one machine transcript in the spoken language and generates
every other language from it on request, so a bare `en` among a non-English
video's *automatic* captions is a job waiting to be run, not a file waiting to
be served. The timedtext endpoint takes the target language as `tlang`, so a
URL carrying it is an exact marker rather than a guess. Automatic tracks
therefore prefer the `-orig` variant and skip translations; manual tracks are
uploaded files and keep plain `en` first.

A video whose only English is translated now resolves to **no fetchable
track**, which means it is queued for whisper as `needs_transcription` rather
than left `captions_pending` and retried forever. One real episode
(`JkKUelM6K8w`, a levin_yt talk) refused every run for a fortnight this way.

**A caption failure no longer discards the episode.** Phase 1 has already
established the title, duration and url; losing all of it because the caption
fetch was refused left directories empty, so every later run met the video as
though it had never been seen — and met it in the same early position, where
its refusal ended the run before anything else was fetched. The metadata is
written before the error is re-raised.

**`pull` and `qs transcribe` are rationed too, and were not.** A pull downloads
a whole episode (~50 MB, ~2.5 GB for video) and `transcribe` downloads audio in
batches; neither spent anything from the budget and neither checked the
cooldown. So a 429 could stop every ingest for six hours while a whisper
backfill carried on pulling gigabytes from the host that had just said stop.
Both now check the cooldown and charge the budget. A pull does not wait for
hourly spacing — it is interactive, and two minutes of silence in front of a
person is a hang, not politeness — but it still refuses once the day is spent.
Both are gated on the actual host, so an RSS enclosure is still free.

Running out mid-run stops with `stopped: "budget"` and keeps what it managed;
running out before any work refuses outright, since there is no partial run to
report. Neither is a rate limit and neither starts a cooldown. `qs status`
shows what is left.

**Things that will waste your time if you don't know them**
- A `503` from search means the corpus server is stopped, not broken.
- `qs` run on the desktop exits 1 and tells you the corpus is elsewhere.
  That is the guard working, not a misconfiguration.
- `/api/qs/status` reports `palette.version` and `palette.capabilities` for
  both ends (`remote_palette` when bridged). If something documented here
  is missing, check the two sides are on the same build — `.\deploy.ps1
  -Check` answers that in one command, and `./server-app.sh update` on the
  server brings that side forward without the desktop being involved. It
  fast-forwards and restarts **only if the app was already running**, since
  the app is on demand and updating code is not a request to serve it.
  A dirty tree or a diverged checkout is refused rather than merged.
- **Three sessions write to one `library.json`, so prefer the additive
  endpoints.** `POST /api/items/batch-tag` (and `batch-palette`) add or
  remove *one* tag and are safe under concurrency — eight at once on the same
  item all land. `PATCH /api/items/{id}` replaces the whole `tags` list, so if
  you read the list, add to it and send it back, another session's tag written
  in between is overwritten. That is last-writer-wins by design, not a bug,
  and no server-side lock can fix it: the stale list was computed on your
  side. The library itself is safe either way — writes are atomic and every
  read-modify-write inside the app holds a lock — but *what you send* is your
  problem.
- `tail_clean: false` in a cut's diagnostics means no natural pause was
  within reach and the tail was faded. Usable, but moving the end to just
  before a real pause is better — that is what step 2 is for.

## Transcript from one place, audio from another

A source no longer has to supply both. YouTube gives captions for a few KB and
no throttling; a podcast feed gives the same conversation's audio from a CDN
that wants you to have it, with range requests and no 403. Dwarkesh is set up
this way: `dwarkesh_yt` (YouTube, captions, searchable) and `dwarkesh` (RSS,
136 episodes of audio on disk).

The join is a file. `cut._source_media` checks `stored_audio(ep_dir)` before
anything else, so an `audio.*` hardlinked into the captioned episode's own
directory is used with no network and no code change. `metadata.json` records
where it came from:

```jsonc
"audio_provenance": {
  "linked_from": "dwarkesh/rss-da97217b6203",
  "offset_s": 0.0,
  "alignment": "duration_exact",     // or "probed_constant"
  "matched_on": "title"              // or "date_duration"
}
```

**How the pair was made is recorded, because the evidence is not equal.**
`link-audio` tries titles first. Dwarkesh words the same conversation
differently on each platform - "This might be the clearest warning shot..."
upload, "Inside the OpenAI agent swarm..." feed entry - and 10 of 19 unlinked
episodes never title-matched, so every cut from them fetched ~50 MB from
YouTube while the audio sat on disk. Where no title matches, a pair is made on
**same `upload_date`, duration within tolerance, and exactly one candidate in
each direction**: this episode has one such feed episode, that feed episode is
claimed by no other upload and lends its audio to no one yet, and trailing
series numbers do not disagree. Anything unsure is reported as `ambiguous`,
not folded into `unmatched`, and the dry run lists every `date_duration` pair
with both titles side by side so it can be read before `--apply`.

A title can also find the **wrong conversation**: a guest's 2026 upload
title-matched their 2023 feed episode, 139 s away, and left in `differs` the
offset probe would have measured a shift between two different recordings. A
title match on a different day therefore gives way to a unique same-day,
same-length episode. A same-day title match that differs in length stays in
`differs` - that is a real pre-roll, and it is the probe's to measure.

`matched_on` lets a later reader weigh a date-and-length pair below a
title-confirmed one. It is absent on links made before 2026-09-15, all of which
came through titles or the offset probe. The alignment guard in `cut` still
refuses a wrong pair, but only once a cut is attempted, which is why the rule
refuses first.

**The two versions do not always share a timeline.** Measured across all 125
Dwarkesh episodes: 53 match to the second, 55 sit at a constant shift (mostly
-30 to -60s, the YouTube upload carrying an intro the feed does not), 3 shift
mid-episode, and 1 could not be fitted. `offset_s` holds the measured shift and
`cut` applies it to the whisper window and the ffmpeg seek — and to nothing
else, since `attribution.range` and `source_url_ts` cite the episode as
published.

Offsets are measured, never inferred from the duration difference: the extra
time could sit at the head, the tail, or both, and a wrong guess puts the cut
a minute from the quote while still sounding clean. Two probes per episode, at
25% and 75%; **if they disagree, ads were inserted mid-episode and no single
number is right**, so the episode is left on YouTube audio rather than given a
figure that is correct in one half. Probe agreement within 8s counts as
constant — tighter than that is below what a 1s search step and whisper's word
boundaries can resolve (observed spreads were 4-6s, then a gap, then 27-37s).

That is why `qs cut` checks. It compares the stored transcript's text for the
span against what whisper actually heard and refuses below
`QS_CUT_ALIGN_MIN` (0.45), recording `caption_alignment` in `cut_diagnostics`
either way. **This is the guard that makes the whole arrangement safe**: a
misaligned cut is not obviously broken, it is a fluent clip of a different
sentence in the right voice, and nothing downstream would catch it. If you see
that refusal, the audio and the transcript disagree — do not reach for
`QS_CUT_ALIGN_MIN=0` without listening first.
