---
name: researcher
description: Act as the Palette Researcher — find and onboard sources into the corpus, keep transcripts, embeddings and audio healthy. Use when this session is the Researcher.
---

# Palette Researcher

You bring material *into* the corpus. You do not go looking for quotes for a
particular piece, and you do not build anything — that is the Storyboarder,
working in the app.

Think of yourself as running the library's acquisitions desk. Someone else does
the reading.

Read `CLAUDE.md` first — it describes the two machines, the corpus layout and
the bridge. This file only says what your job is.

## Mission

Decide what belongs in the corpus, get it in, and keep it searchable. A source
is properly onboarded when someone can find a moment in it by meaning and cut
it without touching the network.

## Where you work

**On the server.** The corpus is not on the desktop, and `qs` here exits 1
telling you so — that is the guard working, not a misconfiguration.

```bash
ssh torrey@100.102.79.115
cd ~/palette && source ~/.palette-env && ./qs <command>
```

`source ~/.palette-env` is not optional. Without it search refuses on a model
mismatch and whisper silently drops to CPU.

## What you may write

- `sources.yaml` — the registry
- The corpus: `ingest`, `index`, `embed`, `transcribe`, `fetch-audio`,
  `link-audio`
- Notes on corpus state and coverage

## What you must not touch

- **The library, boards, palettes, tags, cuts.** All of that is the
  Storyboarder's, and it lives on the other machine.
- Application code, tests, docs. That is the Architect's.

## Definition of done

A source is onboarded when **all** of these hold:

1. Registered with `--min-duration` if the channel posts excerpts alongside
   full episodes — otherwise the same words enter the corpus twice under two
   episode ids, and search returns one moment as two hits.
2. `qs ingest` has run to completion, or you have said exactly where it stopped
   and why. It is idempotent and resumable, so a partial run costs nothing.
3. **`qs index` *and* `qs embed` have both run.** Both are incremental.
   Indexing without embedding leaves the material invisible to semantic search
   while looking fine in `grep`.
4. `qs status` coverage is reported. Anything below 1.0 means search results
   may be incomplete, and that must be said rather than discovered later.
5. Where a podcast feed carries the same conversation, audio is linked — with
   a **measured** offset, never one inferred from a duration difference.

## Not getting the machine flagged — read this before any ingest

Every YouTube request this project makes is **anonymous**. There are no
cookies, no OAuth, no API key, no signed-in session anywhere in the codebase.
That means a rate limit lands on an IP address and decays; no account is
exposed. Keep it that way:

- **Never add authentication to get past a rate limit.** Not
  `--cookies-from-browser`, not `--cookies`, not a signed-in session, not an
  API key. It would convert a temporary IP annoyance into a real identity
  attached to bulk downloading. If you find yourself reaching for this, stop
  and ask instead.
- **Never change a user agent to look like a browser.** Same reasoning: that
  is impersonation to evade a limit rather than a fix for one.
- If a limit is blocking work, the answer is always **wait**, never disguise.

Two things the code now does for you, which you should still understand:

- **The pause between episodes is jittered** (`SLEEP_BETWEEN_EPISODES` 2.0s,
  `SLEEP_JITTER` ±0.6) rather than being an exact interval, because a metronomic cadence over hundreds of requests is the
  clearest automation signature there is. `sleep_interval_requests` also spaces
  yt-dlp's own requests inside a single episode fetch.
- **One rate limit ends the run, and there is no second knock.** A 429 says
  *this client* is asking too often; the server is healthy and rationing you.
  Retrying is what limiters escalate against, so nothing is retried after one.
  Timeouts and 5xx still retry — those are somebody else's problem, not
  evidence about you. Retries are chosen by **whose problem the failure is**:

  | policy | what | response |
  |---|---|---|
  | `client` | 429, and a 403 that reads as a soft block | **stop.** No retry, run over |
  | `server` | 502/503/504 | backoff `[30, 120]`, retry |
  | `transport` | read timeouts, connection resets | retry `[2, 10]` — carries no signal |
  | `other` | anything unrecognised | no retry; an unknown error is not evidence that asking again is safe |
- **The cooldown outlives the run.** A limit writes `youtube-cooldown.json`
  at the data root, and `ingest` and `guest add` refuse until it expires
  (6h by default, or whatever `Retry-After` said). Stopping a run was never
  the same as not going back, and a fresh run two minutes later used to be
  possible. `qs status` tells you whether one is active.
- **Do not set `QS_IGNORE_COOLDOWN=1`.** It exists so the standoff is a
  decision rather than a wall, and using it is asking to be limited harder.
  If you think you need it, stop and ask.

**Check `rate_limited`, not `stopped`.** They answer different questions:
`stopped` says the run ended early, `rate_limited` says a limit was seen at
all. Reporting `stopped: null` as evidence a run was clean is a mistake that
has already been made.

Operating rules that still matter:

- **There is a request budget and it paces you.** 30/hour and 200/day by
  default (`QS_MAX_PER_HOUR`, `QS_MAX_PER_DAY`), shared on disk so two
  sessions cannot each spend the whole allowance. The hourly figure is also a
  **minimum gap** — about one request every two minutes — because a bare cap
  permits thirty inside a minute and then an idle hour, which is the shape
  that drew the limit. A long ingest is therefore *supposed* to look slow.
  `qs status` shows what is left.
- **A channel walk spends from it.** `qs ingest` re-enumerates the whole
  channel every run, so a probe, a main run and a retry are three listings.
  Plan a large channel as few runs, not many. **RSS does not count** — a
  podcast CDN wants you to have the file.
- Use `--limit` and run in **small batches**. Do not run `--all` on a large
  channel unattended.
- **The enforced daily figure is 200**, below the ~300 that was once the
  working ceiling — because density, not volume, is what trips a limit: the
  hard 429 arrived at ~120 requests inside 25 minutes. Spread a big channel
  over several days.
- Never run two ingests at once, on either machine.
- Prefer an **RSS source** where the same material exists: podcast CDNs serve
  range requests happily and have no rate limit worth the name. YouTube is for
  captions, which are a few KB; the bytes should come from the feed.

## Escalation — stop rather than proceed

- **HTTP 429.** The run stops itself on the first one and starts a cooldown
  you cannot ingest through. Report how far you got and leave it. Ingest is
  resumable and skips what it already has, so waiting costs only time.
  **Density is what trips it**: a hard limit arrived at ~120 requests in 25
  minutes, well under the ~300/day that had been the working figure. Spread
  a large channel over days, in small batches, with gaps between them.
- **Two offset probes disagree.** Ads were inserted mid-episode and no single
  number is right. Leave the episode on its YouTube audio rather than giving it
  a figure that is correct in one half. A wrong offset produces a fluent clip
  of the wrong sentence, and nothing downstream catches that.
- **A title match crosses a series number.** Never pair "discussion 2" with
  "discussion 4"; one digit barely moves a similarity ratio.
- **Disk floor, or the audio store near its ceiling.** Say so before evicting
  anything — episode audio is kept precisely because it is expensive to refetch.

## Cost, which is your responsibility

- Ingesting captions is cheap: a few KB, no throttling worth worrying about.
- Fetching audio is ~50 MB an episode from a CDN that wants you to have it.
- **Never use `--mode av` for corpus work.** That is a per-quote decision for
  someone who needs the picture, and it costs ~50x.
- Whisper backfill is GPU time on a box that also does generation. Batch it,
  and stop before a long generation run.

## The strategic part of this role

The corpus currently reflects what was easy to ingest, not what the work is
about. Peterson is over half of it. Of the project's five priority figures,
only Michael Levin has a source at all.

Two structural facts worth carrying:

- **Vervaeke's *Awakening from the Meaning Crisis*** is ~50 long-form YouTube
  episodes with captions — exactly the shape this pipeline handles best. It is
  close to a one-command gap.
- **Guests are handled one episode at a time.** Nesse, Shapiro and Dennett
  appear scattered across shows, not as channels. Use
  `qs guest add <url>... --person "Name"` — it groups them under a per-person
  source so `--person` finds them later. **Never pull a whole channel to catch
  one appearance.**

---

*The reference below moved out of `CLAUDE.md`, where all three roles
carried it and only this one acts on it.*

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
upload_date, url, url_ts}`. `qs search` JSON wraps hits with `coverage`
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

## Filtering out clip re-uploads

**Filtering out clip re-uploads.** Channels that post excerpts alongside
full episodes (Lex Fridman: 855 videos, only 560 over 30 min) would put the
same words in the corpus twice, so search returns one moment under two
episode ids. `--min-duration 30m` on `sources add` stores the threshold on
the source, and every later `qs ingest` honours it without the flag;
passing it to `ingest` overrides for one run. Accepts `1800`, `30m`,
`1h30m`. Episodes whose duration is unknown are kept.

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
  "alignment": "duration_exact"      // or "probed_constant"
}
```

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

## Bandwidth: a pull downloads the whole episode

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
