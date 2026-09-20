# Generating references

ComfyUI workflows, candidates and choosing, and the one GPU three tenants share. Shared context — the two machines, how work reaches them, and the rules every agent follows — is in `AGENTS.md`.

## Generating references for a beat

The GPU renders; a person chooses. That split is the whole design, and it is
what makes an unattended run safe to ask for.

```bash
API=http://127.0.0.1:7861/api
curl -s "$API/generate/workflows"          # what templates exist
curl -s -X POST "$API/generate" -H 'Content-Type: application/json' -d '{
  "prompt": "a defeated lobster, low angle, cold rim light",
  "count": 3, "board_id": "<board>", "beat_id": "<beat>"}'
curl -s "$API/qs/pull/<job_id>"            # same polling as pull and cut
```

**The workflow is yours, not ours.** There is no graph in this codebase and
there will not be one. The server holds krea2, two sizes of flux-2-klein,
z-image, anima, a reference-to-video model, five LoRAs and eleven VAEs, and
each combination wants a different text encoder, VAE and sampler — encoding
any of that in Python means a code change every time you change your mind
about a model. So export a workflow from ComfyUI with **Workflow > Export
(API)**, drop it in **`/home/torrey/palette-library/workflows/<name>.json` on
the server** — the machine with the GPU, not the machine with the media, and
`/api/generate/workflows` reports the path it is actually reading — and put
`{{PROMPT}}`
where the prompt text goes and `{{SEED}}` where the seed goes. Everything else
stays where you already edit it.

**A batched run shares one seed across its images, and that is left alone.**
Three images from `count: 3` come back carrying the same seed, so the seed
does not identify which one you liked. ComfyUI varies the noise per batch
index internally; the run is still reproducible by rerunning the same workflow
at the same seed and picking the image again. A `batch_index` field and the
machinery around it were considered and deliberately not built — this is a
quirk to know about, not a defect to work around.

`{{SEED}}` is optional and its absence is reported by
`/api/generate/workflows` as `varies_by_seed: false`, because a template
without it renders the same image three times — legal, occasionally wanted,
usually a mistake.

There is **no default workflow**. A guessed graph against an unknown model set
renders plausible garbage, so with no template the call refuses and the
message says which three clicks fix it.

**Generating never selects.** A finished job appends to the beat's
`candidates` and leaves `item_id` alone. Choosing between references is a
judgement and it stays with a person unless someone explicitly asks
otherwise — which is what lets "fill the board and I will pick later" be one
sentence rather than a mode.

Attaching is **additive**, under the same reasoning as `batch-tag`: a
whole-list `PATCH` would discard anything written to the board while the GPU
was busy, and a batch of three takes minutes.

**The page has the same call.** Each beat has a **Generate 3** button under its
image prompt. It saves first, sends the prompt exactly as written on the beat,
shows the job's stage on the button, and when the references land re-renders
the beat with the cursor put back where it was. It never selects either.

**Nothing copies the generation parameters into the library.** ComfyUI writes
the prompt and the entire graph into the PNG's text chunks, so the file
already answers "how was this made?" — and import is a byte-for-byte copy, so
it survives. A second copy in `library.json` would only be the one that goes
stale.

**References are kept but hidden.** Every generated image is tagged
`reference` and `generated`, and `GET /api/items` leaves them out unless you
pass `references=true` or ask for the tag by name. Three per beat per round
fills a picker faster than anything else here and most are rejected on sight,
but "actually the second one was better" is real, so they are kept out of the
way rather than deleted.

**One card, three tenants, and one of them squats.** ComfyUI, the embedding
model and whisper share one 12 GB GPU. Two of the three let go by themselves:
the embedding model is released about ten minutes after the last search, and
whisper after `QS_WHISPER_IDLE_S` (600 s) without a cut or a `words` call.

**Whisper only started doing that on 2026-09-20**, and this page said it did
long before. Its model cache had no eviction at all: one `qs words` call left
the corpus app holding 3,930 MB of the card for 4 days and 20 hours, idle, on
a machine whose whole point is that generation and the corpus take turns. If a
process is holding the card and nobody is working, check
`nvidia-smi --query-compute-apps=pid,used_memory --format=csv` before assuming
which tenant it is. **ComfyUI is not one of them** — it keeps its
checkpoint resident once loaded and does not give it back. Measured 2026-09-10,
21 hours after the last generation with nothing queued: 10,292 MB of 12,288
held by one process, 16% of the card free.

That corrects the claim that used to be here, which said nothing squatted
permanently. It does not, however, mean searches start failing: measured on
the same nearly-full card, a semantic search loaded its model into the
remaining ~1.6 GB and returned in 6.2 s. **The squat is real and the collision
was not** — the second was assumed here for a while on the strength of the
first, and one measurement removed it. Check `gpu_used_mb` on
`/api/qs/server` before blaming the card for anything.

A generate reports `vram` and, below 25% free, a `warning` naming what is
holding it. It is **reported, never enforced**, in both directions: stopping
the corpus server to free memory would kill a search mid-thought in a session
that never learns why, and unloading ComfyUI's models under a generation run
is the same rudeness pointed the other way. Freeing the card is a decision
with a person behind it.

**The embedding model is cached somewhere that survives a reboot**, which it
was not. `fastembed` defaults to `tempfile.gettempdir()/fastembed_cache` —
`/tmp` on Linux — so the first search after every reboot re-downloaded
~1.3 GB from the HuggingFace Hub. Measured before the fix: 2 m 07 s, during
which the caller got no progress and finally a bare `Internal Server Error`.

Nothing was broken, which is what made it easy to miss for so long. It
re-downloaded, it worked, and the only symptom was one slow search a person
would read as the corpus being unwell. A cache whose entire job is to not do
the work twice was doing it again on every boot.

It now lives in a durable per-user directory — `$XDG_CACHE_HOME/quotesource/
models`, else `~/.cache/quotesource/models`, and **`QS_MODEL_CACHE`**
overrides. Deliberately not the corpus data root: the model is refetchable
and is not corpus data, and a gigabyte of it beside the transcripts would end
up in every backup of them.

**A slow corpus no longer reports as a broken one.** The bridge waits
`QS_REMOTE_TIMEOUT` (120 s) and a read timeout used to escape urllib
unwrapped, past both of its handlers, arriving as a bare 500. It is now a 504
that says the server is probably still working and to retry — which is the
honest reading, since the request it gave up on goes on to succeed. If a
search still times out, `tail ~/palette-app.log` on the server shows what it
is doing.
