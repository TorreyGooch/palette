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

## House invariants — break these and something rots quietly

- **Derive, do not store.** Frame numbers, narration times, pipeline stage: all
  functions of something else. A copied number goes stale the moment its source
  moves. Store the input, compute the rest on read.
- **Read paths never write.** A GET that creates a directory is a side effect
  nobody asked for. This has already been fixed once.
- **Refuse rather than emit something plausible-but-wrong.** The alignment
  guard is the model: a misaligned cut is not obviously broken, so the code
  stops instead of shipping it.
- **Report what actually happened.** `missing[]`, `tail_clean`, `coverage`.
  Partial success that reads as success is the failure mode to design against.
- **One writer per artifact.** `save_library` writes atomically and every
  read-modify-write holds `library_lock`. Anything new that loads the library,
  changes it and saves it must take that lock too, or it reintroduces the lost
  update.

## Things that will waste your time if you don't know them
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

## Where the rest of this lives

This file is the shared model: the two machines, the bridge, what is in the
corpus, and the rules every role is held to. **Operational detail lives in the
brief for the role that acts on it**, and that brief is already loaded for that
session — so nothing here is lost, it is addressed.

| you want | read |
|---|---|
| the `qs` command table, guests, audio linking, ingest cost and rate limits | `.claude/skills/researcher/SKILL.md` |
| search patterns, cutting for narration, the word manifest, contact sheets, storyboards | `.claude/skills/storyboarder/SKILL.md` |
| the harness itself, the debt list, what may be written where | `.claude/skills/architect/SKILL.md` |

One copy of each fact, in front of the person who acts on it. A fact two roles
both need belongs *here*; a fact one role acts on belongs in their brief. When
those drift the brief wins for that role, and it is the Architect's job to
notice — a stale brief is not a documentation lapse, it is a standing
instruction to work the old way, re-issued at the top of every session.
