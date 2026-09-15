# Operating the app across both machines

Starting the corpus server and searching, reading and cutting from the desktop. Shared context — the two machines, how work reaches them, and the rules every agent follows — is in `AGENTS.md`.

## Starting a creative session

```bash
# 1. is the corpus server up?  (start|stop|restart|status)
curl -s -X POST http://127.0.0.1:7861/api/qs/server \
     -H 'Content-Type: application/json' -d '{"action":"start"}'
```

Or press **Start** on the Quotes page — same endpoint. The card shows what
it costs: app RAM, free machine memory, VRAM, GPU load.

**What it actually costs**, because the two figures are far apart: **~63 MB
idle**, but **~5.8 GB after a search** (measured 2026-09-10 at 458 k chunks;
it read ~3.3 GB when this was first written and the corpus was smaller) — the
embedding model plus one pass over the vectors. **The second number tracks the
corpus and will keep drifting**, so read it off `/api/qs/server` rather than
off this page. It is released about 10 minutes after the last search
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
whose audio is instead linked to `dwarkesh_yt` — see "Transcript from one place, audio from another" in `docs/corpus.md`). Feeds are found
through Apple's public directory, which is a directory over RSS: its search
API returns the publisher's own feed URL, and the audio is a plain enclosure
on their CDN. Spotify is not usable — metadata-only API, DRM'd audio.
