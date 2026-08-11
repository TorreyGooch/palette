# QUOTESOURCE — Phase 0 Discovery Notes

Findings from reading the staging app (palette) before building quotesource.
Vocabulary mapping from the spec: "boards" → **palettes**, "clips" → **library items**.

## How the staging app stores things

- **One library, one root folder**, chosen at first run and remembered in
  `config.json` at the repo root (`palette_app/config.py`). Layout:
  ```
  <library_root>/
    media/          # all item files (flat)
    thumbnails/     # <item_id>.jpg
    exports/        # contact sheets, re-encoded videos
    library.json    # the entire database
  ```
- **`library.json` is the database.** Two arrays:
  - `items`: `{id (uuid4), filename, type: image|video, title, url, tags[],
    palettes[], duration, fps, added}`
  - `palettes`: `{id (uuid4), name, created}`
  Items reference palettes by id (many-to-many). Everything is schemaless
  JSON — adding fields is free and old items simply lack them.
- Read/write helpers live in `palette_app/library.py` (`load_library`,
  `save_library`, `new_item`, `media_type`).

## How the YouTube download path works

- `palette_app/api/download.py` → `download_url(url, dest_dir, start_time,
  end_time)`: yt-dlp, best-quality mp4, optional time-range download
  (`download_range_func` + `force_keyframes_at_cuts`), Windows-safe filenames.
  Returns `[{title, filename}]`. Runs in an executor (async-friendly).
- Registration (probe duration/fps via ffprobe, generate thumbnail, append to
  `library.json`) happens in **`_register_file()` in `palette_app/main.py`**.

## What "export for the diffusion pipeline" produces

- Contact sheets (Nth-frame grid JPG) and re-encoded/trimmed MP4s, written to
  `<library_root>/exports/`. Both operate on video items only. Audio items are
  irrelevant to these exporters — no conflict.

## Integration point for "stage this segment onto a palette"

- The right call path is: write the clip file into `<library_root>/media/`,
  then register it as an item with `tags`/`palettes` set.
- **Gap:** `_register_file()` lives inside the FastAPI module, so the CLI would
  need the server's module (not the server itself — it's just a function — but
  it's the wrong home). **Planned refactor:** move registration into
  `palette_app/library.py` (or a small `palette_app/registry.py`) so both the
  web app and the quotesource CLI call the same function. Small, safe change.

## Schema additions needed (smallest viable)

1. **`attribution` field on items** (optional dict):
   `{person, show, episode_title, episode_date, source_url_ts, quote_text,
   transcript_provenance, range: [start, end]}`. Schemaless JSON → additive,
   no migration.
2. **`audio` item type.** `media_type()` currently knows image/video
   extensions only; add audio (`.mp3 .m4a .opus .ogg .wav .flac`). UI needs a
   placeholder thumbnail and an `<audio>` element in the detail panel.
   Decision per spec Phase 4: audio items appear on palettes like any other
   item, distinguished by type badge — the existing structures suggest exactly
   this, no separate board type.

## Storage root

- Corpus data lives at **`<library_root>/quotesource/`** by default — inside
  the library root, *not* the repo, so it inherits the existing "relocate by
  changing one config value" property (and can live on the GPU server next to
  the audio store). Override precedence: `QUOTESOURCE_DATA` env var →
  `quotesource_data` key in `config.json` → default.
  ```
  <library_root>/quotesource/
    sources.yaml
    episodes/<source_id>/<episode_id>/   # metadata.json, captions.raw.*,
                                         # transcript.json, audio.m4a
    index/                               # SQLite (FTS5 + vectors)
  ```

## Environment notes (affect later phases)

- Operator Python is 3.14 — torch/ctranslate2 wheels may lag. Phase 2/3
  candidates: whisper.cpp (no Python ML deps) or a pinned 3.12 venv on the
  GPU server (RTX 3060 12 GB — faster-whisper large-v3 int8 fits fine).
  Decide in Phase 2, not before.
- yt-dlp caption fetching rate-limits earlier than video downloads; ingest
  must back off on 429 and treat caption failures as retryable, not as
  `needs_transcription`.
