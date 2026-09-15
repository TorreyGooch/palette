@AGENTS.md

# Claude Code sessions

`AGENTS.md`, imported above, is shared with every agent on this repository —
Claude Code and Codex alike. This file adds only what is specific to Claude
Code. If the import did not load, read `AGENTS.md` now: the rules in it are
not optional.

## Three roles share this folder

Work here is done by three Claude Code sessions with different jobs. They share
one working directory, so this file and `.claude/settings.json` load identically
for all of them — which means **a session has to declare which role it is**;
nothing can assign it automatically.

Codex also works on this code, on branches. It reads `AGENTS.md` and
none of `.claude/`, which is why the rules every agent must follow
live there and not here.

| skill | job | works on |
|---|---|---|
| `/architect` | the app, the CLI, tests, docs, and this harness | either |
| `/researcher` | onboarding sources into the corpus | the server |
| `/storyboarder` | driving the app: search, cut, curate, assemble | the desktop |

**If you are resuming a compacted session, re-invoke your role skill.** The
brief was loaded into the conversation, so a summary keeps the gist and loses
the rules — the escalation cases and the definition of done are exactly the
parts that get compressed away.

## What binds only Claude Code

- `.claude/settings.json` denies `Write` and `Edit` under the media library.
  Codex never reads it; `AGENTS.md` states the same rule for every agent.
- Role briefs live in `.claude/skills/<role>/SKILL.md`, and each names the
  `docs/` files its role reads.

## Why this file is short

Until 2026-09-15 this file held the detail for every subsystem — 1,233 lines,
loaded into every session whether its role needed it or not. That detail now
lives in `docs/`, word for word, where Codex can reach it as well and a session
reads only the parts its work touches. The index is "Where the detail lives"
at the end of `AGENTS.md`.
