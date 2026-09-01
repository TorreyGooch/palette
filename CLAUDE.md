# palette + quotesource

Two things live in this repo:

1. **palette** — web app (`launch.bat`, http://127.0.0.1:7861) for a visual
   reference library: import images/video, carve clips (keyframe workflow),
   tag items, group them into *palettes* (named collections, many-to-many),
   build *storyboards* (chosen panels, annotated, rendered to one PNG),
   export contact sheets (single, or a paged series covering a whole video)
   and trimmed videos for diffusion workflows. Data lives
   in a user-chosen library folder (`config.json` → `library_path`);
   `library.json` inside it is the media database, and `storyboards/`
   beside it holds one JSON file per board.
2. **quotesource (`qs`)** — spoken-word sourcing layer. Maintains a corpus of
   YouTube channels / RSS podcasts with timestamped transcripts, exposes
   search primitives, and stages verified segments onto palettes with
   attribution.

## Three roles share this folder

Work here is done by three Claude Code sessions with different jobs. They share
one working directory, so this file and `.claude/settings.json` load identically
for all of them — which means **a session has to declare which role it is**;
nothing can assign it automatically.

| skill | job | works on |
|---|---|---|
| `/architect` | the app, the CLI, tests, docs, and this harness | either |
| `/researcher` | onboarding sources into the corpus | the server |
| `/storyboarder` | driving the app: search, cut, curate, assemble | the desktop |

**If you are resuming a compacted session, re-invoke your role skill.** The
brief was loaded into the conversation, so a summary keeps the gist and loses
the rules — the escalation cases and the definition of done are exactly the
parts that get compressed away.

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

# keyword search is the SAME endpoint with mode=grep - there is no /api/qs/grep
curl -s 'http://127.0.0.1:7861/api/qs/search?q=%22exact+phrase%22&mode=grep'
```

**`mode=grep` is the fallback when the GPU is unavailable.** Semantic search
needs CUDA and the embedding model; grep is FTS5 over SQLite, on the CPU. A
cuBLAS failure therefore takes out one and not the other, and a session that
read a CUDA error as "the corpus is unreachable" stopped with a working search
path one parameter away. A semantic failure now names this in its message.

Everything under `/api/qs/*` on **:7861** works whether the corpus is local
or remote; that is the whole point of the bridge. Prefer it to the CLI.

**What is in the corpus right now:** ~3,291 episodes, 458,830 chunks, fully
embedded with `bge-large-en-v1.5`. Searchable (captions or whisper):

| source | episodes |
|---|---|
| `jordanpeterson` | 1,079 |
| `lexfridman` | 560 |
| `levin_yt` | 177 |
| `dwarkesh_yt` | 125 |
| `vervaeke_amc` | 50 — *Awakening from the Meaning Crisis* |
| `guest_daniel_dennett`, `guest_randolph_m_nesse`, `guest_james_a_shapiro` | 10 each |

Ask `/api/qs/status` rather than trusting this table; it drifts with every
ingest, and the endpoint is derived from the corpus itself.

**Transcript quality does not follow the source, and `transcript_source:
manual` does not mean a human wrote it** — creators routinely upload an
unedited auto-caption dump as a manual track. Within *one* source you can
find both "Plato is deeply influenced by the natural philosophers" and "my my
contention and what i'm going to argue is it's no coincidence". Lex's are
generally human-made and punctuated and Peterson's are mostly auto-captions,
but treat that as a prior, not a guarantee: **verify wording with `context`
before quoting anything.**

Audio-only, **not yet searchable** — ingested from podcast feeds, which carry
no captions, so each needs whisper before `qs search` can find it: the Jordan
B. Peterson Podcast (590), Theories of Everything (358, the show with the most
Michael Levin appearances), Thoughtforms/Michael Levin (186), Dwarkesh (136,
whose audio is instead linked to `dwarkesh_yt` — see below). Feeds are found
through Apple's public directory, which is a directory over RSS: its search
API returns the publisher's own feed URL, and the audio is a plain enclosure
on their CDN. Spotify is not usable — metadata-only API, DRM'd audio.

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
upload_date, url, url_ts, caption_quality, audio_stored}`. **`audio_stored`
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
limit** (`--cookies-from-browser` and friends), and never fake a browser user
agent: both convert a temporary IP annoyance into a real identity attached to
bulk downloading. When a limit blocks work the answer is to wait.

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
