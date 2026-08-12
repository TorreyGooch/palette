# Running palette + quotesource on the GPU server

Goal: the server owns the data and does all heavy work (ingest, whisper,
embeddings). Your desktop is just a browser tab. Nothing syncs, because
there is only one copy.

## 1. Install on the server

```bash
git clone https://github.com/TorreyGooch/palette
cd palette
python -m venv .venv && .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
pip install faster-whisper fastembed-gpu
```

Use **Python 3.12** for this venv. faster-whisper needs ctranslate2, and
ctranslate2/onnxruntime-gpu wheels lag new Python releases — 3.12 is the
safe choice today. (The desktop's 3.14 is fine for the app itself; it is
the ML packages that are picky.)

ffmpeg must be on PATH: `winget install ffmpeg` or your distro's package.

## 2. Point it at storage

Run `python run.py` once and set the library folder to a roomy disk, e.g.
`D:\palette-library`. The corpus then lives at
`D:\palette-library\quotesource\` automatically. To put the corpus
somewhere else, set `QUOTESOURCE_DATA`.

Budget: transcripts+metadata ~2.5 GB per 1,000 YouTube episodes; whisper
audio ~25 MB/hour of content (≈15 GB for 600 hours); pull cache capped by
`QS_PULL_CACHE_GB` (default 6).

## 3. Serve the UI over the LAN

```bash
PALETTE_HOST=0.0.0.0 python run.py          # Windows: set PALETTE_HOST=0.0.0.0
```

Open `http://<server-ip>:7861` from your desktop. Allow the port through
the server firewall. This is a trusting, unauthenticated app — keep it on
your LAN, never port-forward it to the internet.

## 4. Fill the queue

```bash
qs sources add --id <slug> --name "<Name>" --type youtube_channel \
  --url "<channel url>" --people "<Host>"
qs ingest --all                 # metadata + captions, polite + resumable
qs transcribe --batch           # whisper; overnight, resumable
qs index && qs embed            # both incremental
```

Re-run any of these after adding sources; they only do new work. A nightly
cron/Task Scheduler entry running `qs ingest --all && qs index && qs embed`
keeps everything current.

## Expected speeds on an RTX 3060 (12 GB)

| job | CPU (desktop) | GPU (server) |
|---|---|---|
| whisper large-v3 | ~1x realtime | ~8–12x realtime |
| embeddings (179k chunks) | ~2 h | ~5–15 min |

600 hours of audio ≈ 50–75 h of GPU transcription: a few unattended
nights. Run it with `--limit` per night if you want the box free by day.

Env knobs: `QS_WHISPER_MODEL` (default large-v3 on GPU), `QS_WHISPER_DEVICE`,
`QS_WHISPER_COMPUTE`, `QS_DISK_FLOOR_GB` (default 20), `QS_EMBED_MODEL`,
`QS_PULL_MAX_HEIGHT` (default 720), `QS_PULL_CACHE_GB` (default 6).

## Migrating the corpus you already have

The corpus is a plain folder — copy
`C:\Users\torre\Documents\palette\Library` to the server disk and point the
server's `config.json` `library_path` at it. Then `qs status` there should
report the same episode counts, and `qs index --rebuild` regenerates the
SQLite index if you'd rather not copy the large `index/` folder.
