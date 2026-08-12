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

## 3. Serve the UI over the tailnet

`launch-tailnet.bat` (or `PALETTE_HOST=tailscale`) binds **only** this
machine's Tailscale address, so the app is reachable from your other
tailnet devices and from nothing else:

```
launch-tailnet.bat
```

Windows PowerShell has no inline env-var prefix, so the bash form
`PALETTE_HOST=tailscale python run.py` is a syntax error there. Use:

```powershell
$env:PALETTE_HOST="tailscale"; python run.py
```

cmd.exe: `set PALETTE_HOST=tailscale && python run.py`. On Linux/macOS the
bash form works as written.

`tailscale` resolves via `tailscale ip -4`, falling back to scanning
interfaces for the 100.64.0.0/10 range Tailscale allocates from. You can
also pass a literal address (`PALETTE_HOST=100.99.248.49`) or a hostname.

Then browse to `http://<that-100.x-address>:7861`, or use the MagicDNS
name: `http://<machine-name>:7861`.

### Firewall

Windows Firewall blocks the inbound connection by default. The first run
usually raises an "allow this app" prompt — accepting it is enough. If you
get no prompt, add a rule from an **Administrator** terminal, scoped to the
tailnet range so it stays closed to every other network:

```powershell
netsh advfirewall firewall add rule name="PALETTE tailnet" dir=in action=allow protocol=TCP localport=7861 remoteip=100.64.0.0/10
```

To allow just one peer instead of the whole tailnet, use that machine's
address: `remoteip=100.102.79.115`.

### Which IP goes where

`PALETTE_HOST` is the address the app **listens on**, so it must belong to
the machine *running* palette — never the machine you browse from. The
client's address never appears in `PALETTE_HOST`; it only shows up if you
scope a firewall rule to it. `PALETTE_HOST=tailscale` sidesteps the whole
question by resolving to whichever machine it runs on.

### Why not 0.0.0.0

`PALETTE_HOST=0.0.0.0` also works, but it binds *every* interface — the
tailnet, your home LAN, and whatever public wi-fi you connect to later.
This app has **no authentication**: anyone who can reach the port has full
control of the library, including delete. Prefer the Tailscale binding, and
never expose it with `tailscale funnel` or a port-forward.

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

## Moving an existing setup to the server

`git pull` brings the **code only**. Three other things have to arrive
separately, because none of them are in the repo:

| what | where | in git? |
|---|---|---|
| code | the repo | yes |
| corpus (transcripts, audio, index) | `<library>/quotesource/` | no — gitignored |
| library (media, thumbnails, `library.json`) | `<library>/` | no — gitignored |
| `config.json` (points at the library) | repo root | no — gitignored |

### 1. Code

```bash
git clone https://github.com/TorreyGooch/palette
cd palette && chmod +x qs
```

### 2. Data

The corpus is a plain folder, so copy it over the tailnet. From the
Windows machine:

```powershell
scp -r "C:\Users\torre\Documents\palette\Library" torrey@100.102.79.115:/path/to/palette-library
```

Only `quotesource/episodes/` and `quotesource/sources.yaml` are
irreplaceable — they cost hours of throttled ingest. `quotesource/index/`
regenerates in minutes on the GPU (`qs index && qs embed`) and
`quotesource/cache/` is disposable, so skip both if you would rather not
move the bulk.

### 3. Config

Run `python run.py` once and enter the library path, or write
`config.json` directly:

```json
{ "library_path": "/path/to/palette-library" }
```

Verify with `./qs status --pretty` — episode counts should match the
Windows machine.

## Putting the GPU to work

Installing the two GPU packages is all it takes; the code detects CUDA on
its own and needs no flags.

```bash
pip install faster-whisper fastembed-gpu
pip install nvidia-cublas-cu12 nvidia-cudnn-cu12   # ctranslate2 CUDA deps
```

That last line is the usual stumbling block: ctranslate2 needs cuBLAS and
cuDNN, and a missing `libcudnn` shows up as an import or runtime error
rather than a clean "no GPU" message. Installing them from pip avoids
depending on a system CUDA install.

What then runs on the GPU:

- **whisper** — `qs transcribe`, `qs cut`, `qs words`. `_whisper_config()`
  checks `ctranslate2.get_cuda_device_count()` and switches to
  `large-v3` + `float16` when a device is present (CPU default is the far
  weaker `base`). ~8–12x realtime instead of ~1x.
- **embeddings** — `qs embed`, `qs search`. Requests
  `CUDAExecutionProvider` and silently falls back to CPU if unavailable.
  Minutes instead of hours for a full corpus.

Confirm both after install:

```bash
python -c "import ctranslate2; print('cuda devices:', ctranslate2.get_cuda_device_count())"
./qs status --pretty
```

A non-zero device count means whisper is on the GPU. If embeddings were
built with a different model, `qs embed --reset` re-embeds.

ComfyUI and any video model are separate processes — they use the GPU
already and share it with these jobs, so avoid running a whisper batch
during a long generation.

### One less hop

If the GPU box is also the machine running the video model, put the
library there and the hand-off disappears: `qs cut` writes straight into
a folder the video pipeline can read, with no copying between machines.
