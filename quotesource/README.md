# quotesource (qs)

Spoken-word sourcing layer for palette. Phase 1: corpus registry + caption
ingest. See [NOTES.md](NOTES.md) for architecture and SPEC phases.

All commands emit JSON; add `--pretty` for human output.

```bash
# from the palette repo root (or use qs.bat)
qs sources add --id lexfridman --name "Lex Fridman Podcast" \
   --type youtube_channel --url "https://www.youtube.com/@lexfridman" \
   --people "Lex Fridman"
qs sources list --pretty

qs ingest lexfridman --limit 20    # fetch newest 20 episodes' metadata+captions
qs ingest --all                    # everything, politely throttled, idempotent
qs episodes lexfridman --pretty    # transcript status per episode
```

Data lives at `<palette library root>/quotesource/` by default; override with
the `QUOTESOURCE_DATA` env var or a `quotesource_data` key in `config.json`.

Statuses: `captions` (YouTube captions normalized) · `needs_transcription`
(RSS or captionless — Phase 2 whisper queue) · `captions_pending` (transient
fetch failure, retried on next ingest).

## Search (Phase 3)

```bash
qs index                          # incremental; re-run after any ingest
qs grep '"clean your room"'       # FTS5: phrases, OR, NOT, prefix*
qs grep 'lobster hierarchy' --person "Jordan Peterson" --after 2017-01-01
qs context <episode-id> 1999 --window 15    # read transcript around a point
qs context <episode-id> --range 1980 2020
qs episode-info <episode-id>
qs status --pretty                # corpus + index freshness
```

Hits are JSON: `{episode_id, source_id, start, end, text, score,
episode_title, upload_date, url, url_ts}`.

## Semantic search (Phase 3b)

```bash
qs embed                # one-time batch (resumable; ~25 chunks/s on CPU)
qs search "the brain isn't doing backpropagation" --person Hinton
```

`qs search` matches meaning, `qs grep` matches words; use both and
triangulate with `qs context`. Embeddings: BAAI/bge-small-en-v1.5 via
fastembed (local ONNX, no torch), vectors in SQLite, exact cosine search.
Override the model with `QS_EMBED_MODEL` (then `qs embed --reset`).
Search JSON includes `coverage` so partial embedding states are visible.
