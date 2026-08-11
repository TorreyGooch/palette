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
