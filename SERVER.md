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

## 3. Run the corpus API

On the GPU box the app runs **headless, on demand, on :7862** — it serves
the corpus to your desktop, not a UI to you. `server-app.sh` owns its
lifecycle:

```bash
./server-app.sh start     # also stop | restart | status
```

`status` prints JSON, which is what the Start/Stop control on the desktop's
Quotes page reads over ssh. Start it from there and you never touch this
script by hand.

On demand rather than a service is deliberate: this box shares memory and
GPU with generation. Idle it is ~63 MB, but after a search it holds the
embedding model and a pass over the vectors — about 3.3 GB — until
`QS_MODEL_IDLE_S` (600s) releases it. Stop it outright before a long
generation batch.

`PALETTE_API_ONLY=1` makes `/` serve a short explanation instead of the
app. Two palettes that look identical but hold different libraries is how
you end up tagging clips into the wrong one.

### Serving the UI from a machine instead

Only relevant if a machine both holds a media library and should serve it
to other tailnet devices — not the GPU box, whose library is empty by
design. `launch-tailnet.bat` (or `PALETTE_HOST=tailscale`) binds **only**
that machine's Tailscale address:

```powershell
$env:PALETTE_HOST="tailscale"; python run.py
```

Windows PowerShell has no inline env-var prefix, so the bash form
`PALETTE_HOST=tailscale python run.py` is a syntax error there; cmd.exe
wants `set PALETTE_HOST=tailscale && python run.py`. On Linux/macOS the
bash form works as written.

`tailscale` resolves via `tailscale ip -4`, falling back to scanning
interfaces for the 100.64.0.0/10 range. A literal address
(`PALETTE_HOST=100.99.248.49`) or hostname also works.

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
# onnxruntime-gpu from plain PyPI is built against CUDA 13; ctranslate2 wants
# CUDA 12. Take the CUDA 12 build so both stacks share one generation:
pip uninstall -y onnxruntime onnxruntime-gpu
pip install onnxruntime-gpu --extra-index-url \
  https://aiinfra.pkgs.visualstudio.com/PublicPackages/_packaging/onnxruntime-cuda-12/pypi/simple/
pip install nvidia-cublas-cu12 nvidia-cudnn-cu12 nvidia-cuda-runtime-cu12
```

Then put the pip-installed NVIDIA libs on the loader path, or onnxruntime
will not find `libcudnn` even though it is installed:

```bash
SITE=$(python -c 'import site; print(site.getsitepackages()[0])')
export LD_LIBRARY_PATH="$(find "$SITE/nvidia" -maxdepth 2 -name lib -type d | tr '\n' ':')$LD_LIBRARY_PATH"
```

Keep that (and `QS_EMBED_MODEL`) in a `~/.palette-env` you source before
running `qs`, so every invocation agrees.

**This is the stumbling block, and it fails quietly.** `embedder.py` asks
for `CUDAExecutionProvider` and falls back to CPU on failure, so a broken
CUDA setup does not error — it just runs ~38x slower. On an RTX 3060,
embedding runs at **~150 chunks/s**; CPU manages ~4/s. If `qs embed`
reports an ETA in hours rather than minutes, you are on CPU. Confirm
before starting a long job:

```bash
python -c "import onnxruntime as ort; print(ort.get_available_providers())"
nvidia-smi --query-gpu=utilization.gpu,memory.used --format=csv,noheader
```

A real GPU embed run sits at ~100% utilisation and several GB of VRAM.
Utilisation pinned at 0% means the fallback happened.

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

### "Failed to initialize NVML: Driver/library version mismatch"

The driver package was upgraded while the old kernel module was still
loaded. Nothing needs installing — compare the two and reboot:

```bash
sed -n 's/^NVRM version:.*x86_64 *\([0-9.]*\).*/\1/p' /proc/driver/nvidia/version  # loaded
dpkg -l | grep -E 'nvidia-driver-[0-9]'                                            # installed
```

### Choosing an embedding model

`QS_EMBED_MODEL` (default `BAAI/bge-small-en-v1.5`). On a GPU the larger
models cost minutes rather than hours, so `BAAI/bge-large-en-v1.5`
(1024-dim) is worth it for retrieval quality. **Never mix models in one
vector store** — similarity scores across different models are
meaningless. Changing it means `qs embed --reset`.

ComfyUI and any video model are separate processes — they use the GPU
already and share it with these jobs, so avoid running a whisper batch
during a long generation.

## Split setup: corpus on the server, media on your desktop

Putting everything on the server is the simplest story, but it breaks down
if you review video from another machine: scrubbing needs sustained MB/s,
and a Tailscale connection that falls back to a DERP relay gives you about
1 MB/s. Check which you have — a relayed link makes remote video painful:

```bash
tailscale ping <peer>        # "via DERP(...)" = relayed, "direct" = fast
```

The split that works: **the corpus stays with the GPU, the media stays with
your eyes.** A search query is ~1 KB and a cut clip is a few hundred KB, so
only tiny things cross the network — never the multi-GB corpus, never the
media library.

Run palette in both places. On the server, as the corpus API:

```bash
source ~/.palette-env
PALETTE_HOST=tailscale python run.py     # library folder can stay empty
```

On your desktop, point at it:

```powershell
$env:QS_REMOTE="http://100.102.79.115:7861"; python run.py
```

With `QS_REMOTE` set, `/api/qs/*` forwards to the server instead of loading
quotesource locally. Search, context and status become proxied JSON. `pull`
and `cut` run on the server — where the corpus, the audio cache and the GPU
whisper all are — and the finished clip (plus its `.words.json` manifest) is
downloaded into your local library with its attribution, tags and palettes
intact. The browser polls the same endpoints and cannot tell the difference.

The desktop then needs no corpus at all. Delete any stale local
`<library>/quotesource/` so there is no question which copy is real.

### One less hop, and why the library still does not live here

An earlier version of this file said: if the GPU box also runs the video
model, put the library there and the hand-off disappears. That advice
predates the split and no longer applies as written.

The reason is measured, not architectural taste. The link between these two
machines is **relayed, not direct** — `tailscale ping` reports `via
DERP(...)` at ~1 MB/s. That is fine for a search query or a 200 KB clip, and
hopeless for scrubbing 1080p video. The media library is gigabytes of video
reviewed by eye, so it belongs on the machine with the eyes. Moving it here
would trade a real problem for a worse one.

What *is* true is narrower, and `--outbox` acts on it: **a cut clip has two
consumers on two machines.** You review it in palette on the desktop, and
the video pipeline reads it here — so a clip born four directories from
`ComfyUI/input` would otherwise make a round trip to get back. At 200 KB
that is friction rather than a wall, but the trip is avoidable.

```bash
export QS_OUTBOX=~/narration-outbox     # or --outbox per run
```

The clip and its manifest are copied there as they are written, before the
staging branch — so it works for exactly the case it exists for, where the
desktop adopts the clip and this machine discards its own copy. Discard only
touches `media/`, so the outbox copy survives.

A staging folder rather than `ComfyUI/input` directly: the generator's input
folder accumulates everything a pipeline is ever fed, and a tray you copy
*from* stays curatable. Off unless configured.
