# palette + quotesource

This file is for every agent that works on this repository — Claude Code
sessions and Codex alike — and for the people directing them. It holds what is
true for all of them: what the system is, how the code is laid out, how work
gets from a branch onto the two machines, and the rules nobody gets to skip.
Detail for each part of the system lives in `docs/`; the index is at the end.

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

## Who works here

The work is split into roles. **Operating** roles use the system:

| role | job | works on |
|---|---|---|
| Researcher | onboarding sources into the corpus | the server |
| Storyboarder | driving the app: search, cut, curate, assemble | the desktop |

**Engineering** is done on branches, by any agent — a Claude Code `/architect`
session or Codex — and one **integrator** merges into `main` and deploys.
Claude Code sessions also get role briefs under `.claude/skills/` (see
`CLAUDE.md`). Other tools never read those, so anything that must hold for
every agent is written here, enforced by a test, or enforced by a git hook.

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
  Use the app's endpoints (`docs/operations.md`), or run `qs` over ssh on the server.
- **The server is started on demand.** It is not a service — that box shares
  memory and GPU with generation. If search returns 503, the server is simply
  off; start it (`docs/operations.md`) rather than debugging.
- **Do not browse `:7862`.** It serves an explanation page, not the app.
  Anything staged there lands in the *server's* library, not yours.

## How the code is laid out

Measured 2026-09-15.

| part | lines | what it is |
|---|---|---|
| `palette_app/` | ~4,300 | FastAPI app. `main.py` (~2,100) holds all 47 routes in nine commented sections; `library.py` the media database and its lock; `storyboard.py` boards and rendering; `narration.py` binding beats to word ranges; `generate.py` the ComfyUI client; `qs_remote.py` the bridge to the server |
| `quotesource/` | ~5,100 | the corpus layer and the `qs` CLI: ingest, index, embed, search, cut, pull, transcribe, feed audio |
| `frontend/` | ~4,100 | static HTML, CSS and JS with no build step. Talks to the backend only through JSON under `/api/`, through the `api()` helper in `static/js/app.js` |
| `tests/` | ~10,100 | hermetic: no network, corpus, GPU, ffmpeg or real library. `tests/conftest.py` is where that is promised |

Entry points: `launch.bat` (the desktop app on :7861), `run.py`, `./qs` on the
server, `server-app.sh` (the server app on :7862), `deploy.ps1`.

## Working on the code

### On a branch, never on `main`

`main` is what both machines pull and what `deploy.ps1` ships, so only the
integrator moves it. Work on a branch named for who and what —
`architect/<subject>`, `codex/<subject>` — push the branch, and hand it over:
a pull request, or a message to the integrator naming the branch.

The pre-push hook refuses any push that updates `main` unless
`PALETTE_INTEGRATOR=1` is set. Hooks are enabled once per clone:

    git config core.hooksPath hooks

A clone without that setting — a fresh Codex checkout, for one — runs no hook
at all, which is why CI matters: `.github/workflows/tests.yml` runs the full
suite on every branch push and every pull request, on both deployed platforms
(Ubuntu with Python 3.12 for the server, Windows with 3.14 for the desktop).
A branch with a red build is not merged.

### Worktrees, when several agents work at once

Two agents editing one checkout will overwrite each other. Give each its own:

    git worktree add ../palette-<subject> -b <role>/<subject>

A worktree is for editing and running tests. It is **not** where the app runs:
`config.json`, which names the media library, is gitignored and exists only in
the main checkout, and `launch.bat` there is what the person using the app is
looking at. Do not start the app from a worktree, and do not switch branches
in the main checkout while the app is running — it serves `frontend/` straight
from disk.

### Tests

    py -m pytest            # the Windows desktop
    python -m pytest        # anywhere else

Always the full suite, not the file you touched. It takes about half a minute.

### Definition of done

1. The full suite passes.
2. New behaviour has tests that fail without the change. A test that passes
   before and after proves nothing, so check: set the change aside and run
   the new tests against the old code.
3. Documentation matches behaviour — the `docs/` file for the part you changed,
   and this file if it changes something every agent needs to know.
4. Committed on a branch, with a message that says *why*, not just what.

### The integrator merges and deploys

One integrator at a time, normally the Claude Code `/architect` session or the
person directing the work:

    git switch main
    git pull --ff-only
    git merge --ff-only <branch>        # rebase the branch onto main if refused
    .\deploy.ps1                         # tests, push main, server pull, proof

`deploy.ps1` refuses to run from any branch but `main`, refuses a dirty tree on
either machine, sets `PALETTE_INTEGRATOR` for its own push, and fails unless
both machines report the same commit. `.\deploy.ps1 -Check` changes nothing and
says whether the machines agree.

Deployed code is not running code. The desktop app and the server app keep
serving whatever they loaded, and `deploy.ps1` says when either is stale.
`./server-app.sh update` on the server fast-forwards and restarts the server
app if it was running. **Restarting the desktop app interrupts whoever is using
it — ask first.**

## House invariants — break these and something rots quietly

- **Derive, do not store.** Frame numbers, narration times, pipeline stage:
  all functions of something else. A copied number goes stale the moment its
  source moves. Store the input and compute the rest on read.
- **Read paths never write.** A GET that creates a directory is a side effect
  nobody asked for.
- **Refuse rather than emit something plausible but wrong.** The alignment
  guard in `qs cut` is the model: a misaligned cut is not obviously broken, so
  the code stops instead of shipping it.
- **Report what actually happened.** `missing[]`, `tail_clean`, `coverage`.
  Partial success that reads as success is the failure mode to design against.
- **One writer per artifact.** `save_library` and `save_board` write atomically,
  and every read-modify-write holds `library_lock`. Anything new that loads,
  changes and saves either must take that lock too, or it reintroduces the
  lost update.

## Rules every agent follows, in any tool

For Claude Code sessions some of these are also enforced by
`.claude/settings.json` and the role briefs. Other tools read neither. The rules
hold regardless.

- **Never edit the media library's files.** The desktop library is
  `C:\Users\torre\PaletteLibrary` and the server's is
  `/home/torrey/palette-library`. The app's API is the only writer. Tests use
  throwaway libraries under a temporary directory.
- **YouTube is used anonymously, and a limit means wait.** No cookies, no
  OAuth, no API keys, no signed-in session, no faked browser user agent, and
  never yt-dlp's `--impersonate`. Do not set `QS_IGNORE_COOLDOWN` or raise the
  request budget to get past a refusal. The reasoning is in `docs/corpus.md`.
- **Do not lower `QS_CUT_ALIGN_MIN`** to get a cut through. A refusal means the
  transcript and the audio disagree.
- **The app has no authentication.** It binds to the tailnet only. Never widen
  that: no `0.0.0.0`, no `tailscale funnel`, no port forwarding.
- **Stop and ask a person before:**
  - changing a stored shape — `library.json`, board JSON, `.words.json`,
    `metadata.json` — and say what existing data would need migrating;
  - deleting or overwriting media: look at the target first, since several
    library items can share one file;
  - anything that runs on a schedule, downloads in bulk, or spends the GPU;
  - widening what another agent may do unattended.
- **Corpus maintenance is operations, not engineering.** Running `qs ingest`,
  `embed`, `transcribe`, `fetch-audio` or `link-audio --apply` against the real
  corpus is the Researcher's job. Engineering builds and tests those tools.

## Known structural debt — do not deepen it

- **`quotesource` imports `palette_app`** in `cut.py`, `pull.py` and
  `ingest.py`: cutting and pulling register clips straight into the media
  library, `pull` borrows the app's ffmpeg helpers, and the request ledger
  borrows the library's lock. This is the one real dependency loop between the
  two packages. Do not add to it. The intended direction is that `quotesource`
  returns a clip with its attribution and the app adopts it, which the remote
  path already does.
- **`main.py` holds every route.** Expect conflicts there; keep changes small
  and rebase often.
- **Request and response bodies are untyped** — `body: dict` on 22 endpoints,
  no response models — so the generated OpenAPI describes nothing, and the
  contract lives in `docs/` and the tests.
- **34 tests patch attributes on `palette_app.main`**, so moving a function out
  of `main.py` means updating those patch targets too.

## Where the detail lives

| file | read it when |
|---|---|
| `docs/operations.md` | starting the corpus server; searching, reading and cutting from the desktop |
| `docs/corpus.md` | building or running `qs`: commands, guests, investigation, rate limits, feed audio |
| `docs/narration.md` | cutting quotes, word timings and pauses, the `.words.json` manifest |
| `docs/storyboards.md` | contact sheets, boards, beats, narration binding, timing, rendering |
| `docs/generation.md` | reference generation, ComfyUI workflows, the shared GPU |
| `SERVER.md` | installing and running the GPU server |
| `quotesource/README.md` | the `qs` CLI in brief |
| `CLAUDE.md` | Claude Code roles and briefs |
