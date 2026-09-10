"""Submitting a beat's image prompt to ComfyUI and getting references back.

**The workflow is not written here, and that is the whole design.** This box
holds krea2, two sizes of flux-2-klein, z-image, anima, a reference-to-video
model, five LoRAs and eleven VAEs, and each combination needs a different
graph — a different text encoder, a different VAE, different sampler
settings. Encoding any of that in Python would mean a code change every time
you change your mind about a model, and it would go stale silently.

So the graph comes from ComfyUI itself. Export a workflow in **API format**,
drop it in the workflows directory, and put `{{PROMPT}}` where the prompt text
belongs. Everything else — model, sampler, resolution, LoRA stack — stays
where you already edit it, and swapping models needs no code at all.

Two placeholders, both optional:

    {{PROMPT}}   the beat's image_prompt, JSON-escaped for you
    {{SEED}}     a fresh integer per image, so a batch varies

Substitution happens on the raw text before parsing, which is why `{{SEED}}`
can sit unquoted where a number belongs. A template with no `{{SEED}}` is
legal and will simply render the same image every time — occasionally what
you want, usually a mistake, so it is reported rather than corrected.

There is deliberately no default workflow. A guessed graph against an unknown
model set produces plausible garbage, and refusing with instructions is worth
more than an image nobody asked for.
"""
import json
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

COMFY_URL = os.environ.get("COMFY_URL", "http://127.0.0.1:8188")
POLL_INTERVAL_S = 1.5
DEFAULT_TIMEOUT_S = float(os.environ.get("PALETTE_GENERATE_TIMEOUT_S", "900"))

PROMPT_TOKEN = "{{PROMPT}}"
SEED_TOKEN = "{{SEED}}"


class GenerateError(RuntimeError):
    """Anything that stops a generation, phrased for whoever asked for it."""


def workflows_dir() -> Path:
    """Where API-format workflow exports live.

    Beside the library by default, because a workflow is closer to a creative
    setting than to code: it names your models and your taste in samplers, and
    it should travel with the library rather than the repo.
    """
    override = os.environ.get("PALETTE_WORKFLOW_DIR")
    if override:
        return Path(override).expanduser()
    from .config import get_library_path

    root = get_library_path()
    if not root:
        raise GenerateError(
            "no library is configured, so there is nowhere to keep workflows. "
            "Set PALETTE_WORKFLOW_DIR, or choose a library first.")
    return Path(root) / "workflows"


def list_workflows() -> list[dict]:
    """Every usable template, with what each one will and will not vary."""
    directory = workflows_dir()
    if not directory.exists():
        return []
    out = []
    for path in sorted(directory.glob("*.json")):
        raw = path.read_text(encoding="utf-8", errors="replace")
        out.append({
            "name": path.stem,
            "path": str(path),
            "has_prompt": PROMPT_TOKEN in raw,
            # Without a seed placeholder every image in a batch is identical.
            "varies_by_seed": SEED_TOKEN in raw,
        })
    return out


def load_workflow(name: Optional[str] = None) -> tuple[str, str]:
    """The raw template text and the name it was found under.

    Refuses rather than guessing. The message says exactly what to do, because
    the fix is three clicks in ComfyUI and impossible to work out from a
    stack trace.
    """
    directory = workflows_dir()
    available = list_workflows()
    if not available:
        raise GenerateError(
            f"no workflow templates in {directory}. Export one from ComfyUI "
            f"with Workflow > Export (API), save it there as <name>.json, and "
            f"put {PROMPT_TOKEN} where the prompt text goes.")
    if name:
        match = next((w for w in available if w["name"] == name), None)
        if not match:
            names = ", ".join(w["name"] for w in available)
            raise GenerateError(f"no workflow named '{name}'. Available: {names}")
    elif len(available) == 1:
        match = available[0]
    else:
        names = ", ".join(w["name"] for w in available)
        raise GenerateError(
            f"several workflows exist and none was named: {names}")

    raw = Path(match["path"]).read_text(encoding="utf-8")
    if PROMPT_TOKEN not in raw:
        raise GenerateError(
            f"workflow '{match['name']}' has no {PROMPT_TOKEN} placeholder, so "
            f"the prompt would be ignored and it would render whatever text is "
            f"baked into the graph.")
    return raw, match["name"]


def build_graph(raw: str, prompt: str, seed: int) -> dict:
    """Substitute, then parse. In that order, so {{SEED}} can be a number.

    The prompt is JSON-escaped before it goes in: a quote or a backslash in a
    perfectly ordinary prompt would otherwise produce a template that no
    longer parses, and the error would point at the graph rather than at the
    apostrophe that caused it.
    """
    escaped = json.dumps(prompt)[1:-1]
    text = raw.replace(PROMPT_TOKEN, escaped).replace(SEED_TOKEN, str(seed))
    try:
        graph = json.loads(text)
    except ValueError as e:
        raise GenerateError(
            f"workflow is not valid JSON after substitution: {e}. It must be "
            f"an API-format export (Workflow > Export (API)), not the editor "
            f"format.") from None
    if not isinstance(graph, dict) or not graph:
        raise GenerateError("workflow is empty or not an object")
    return graph


def _post(path: str, body: dict, timeout: float = 30.0) -> dict:
    data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        COMFY_URL + path, data=data,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:800]
        raise GenerateError(f"ComfyUI refused the graph ({e.code}): {detail}") \
            from None
    except (urllib.error.URLError, OSError) as e:
        raise GenerateError(
            f"ComfyUI is not answering at {COMFY_URL}: {e}") from None


def _get(path: str, timeout: float = 30.0) -> dict:
    try:
        with urllib.request.urlopen(COMFY_URL + path, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        raise GenerateError(f"ComfyUI returned {e.code} for {path}") from None
    except (urllib.error.URLError, OSError) as e:
        raise GenerateError(
            f"ComfyUI is not answering at {COMFY_URL}: {e}") from None


def vram() -> Optional[dict]:
    """What the card has spare, as ComfyUI sees it. None if it cannot say.

    One 12 GB card runs three things here: this, the corpus embedding model
    (~3.3 GB, released about ten minutes after the last search) and whisper.
    None of them squats permanently, so most of the time nothing collides —
    but the natural creative rhythm is exactly the one that does, because you
    find a quote and then want to picture it.

    Reported rather than enforced. Stopping the corpus server to free memory
    would kill someone's search mid-thought, and that someone may be another
    session that never learns why. A number the caller can act on is worth
    more than a decision taken on their behalf.
    """
    try:
        stats = _get("/system_stats", timeout=10)
    except GenerateError:
        return None
    devices = stats.get("devices") or []
    if not devices:
        return None
    device = devices[0]
    total = device.get("vram_total")
    free = device.get("vram_free")
    if not total:
        return None
    return {"name": device.get("name"), "total_mb": round(total / 1048576),
            "free_mb": round((free or 0) / 1048576),
            "free_fraction": round((free or 0) / total, 3)}


def submit(graph: dict) -> str:
    """Queue one graph. ComfyUI serialises, so this never waits on the GPU."""
    result = _post("/prompt", {"prompt": graph})
    prompt_id = result.get("prompt_id")
    if not prompt_id:
        errors = result.get("node_errors") or result.get("error") or result
        raise GenerateError(f"ComfyUI accepted nothing: {json.dumps(errors)[:600]}")
    return prompt_id


def outputs_for(prompt_id: str) -> Optional[list[dict]]:
    """The images a finished job produced, or None while it is still queued.

    Distinguishes "not done" from "done and produced nothing", because a graph
    whose SaveImage node never fires looks exactly like a slow one otherwise.
    """
    history = _get(f"/history/{prompt_id}")
    entry = history.get(prompt_id)
    if not entry:
        return None
    status = entry.get("status") or {}
    if status.get("status_str") == "error":
        messages = status.get("messages") or []
        raise GenerateError(f"ComfyUI reported an error: "
                            f"{json.dumps(messages)[:600]}")
    if not status.get("completed", True):
        return None
    images = []
    for node in (entry.get("outputs") or {}).values():
        for image in node.get("images") or []:
            if image.get("type") == "temp":
                continue        # previews, not the saved result
            images.append(image)
    return images


def fetch_image(image: dict) -> bytes:
    """The bytes of one produced image, metadata intact.

    ComfyUI writes the prompt and the whole graph into the PNG's text chunks,
    which is why nothing here copies generation parameters into a database:
    the file already carries them, and a second copy would be the one that
    goes stale.
    """
    query = urllib.parse.urlencode({
        "filename": image.get("filename", ""),
        "subfolder": image.get("subfolder", ""),
        "type": image.get("type", "output")})
    try:
        with urllib.request.urlopen(f"{COMFY_URL}/view?{query}", timeout=120) as r:
            return r.read()
    except (urllib.error.URLError, OSError) as e:
        raise GenerateError(f"could not fetch {image.get('filename')}: {e}") \
            from None


def generate(prompt: str, count: int = 3, workflow: Optional[str] = None,
             seed: Optional[int] = None, progress=None,
             timeout_s: float = DEFAULT_TIMEOUT_S) -> dict:
    """Queue `count` renders of one prompt and wait for them.

    Every image gets its own seed so a batch is a set of alternatives rather
    than the same picture N times — which is the entire point of asking for
    three. Passing `seed` fixes the first one, so a result can be reproduced
    exactly while the rest still vary.

    All `count` graphs are submitted before any is waited on, because ComfyUI
    has its own queue and serialising here would only make it slower.
    """
    if not (prompt or "").strip():
        raise GenerateError("a prompt is required")
    count = max(1, min(int(count), 12))

    raw, used = load_workflow(workflow)
    headroom = vram()
    warning = None
    if headroom and headroom["free_fraction"] < 0.25:
        # Not a refusal. ComfyUI queues and will wait for its own memory; the
        # thing worth saying is *why* it is about to be slow, so nobody
        # debugs a stall that is really a search still holding the card.
        warning = (f"only {headroom['free_mb']} MB of "
                   f"{headroom['total_mb']} MB VRAM free — something else is "
                   f"holding the card. The corpus embedding model releases "
                   f"about ten minutes after the last search; stopping the "
                   f"corpus server frees it now.")
    rng = random.Random(seed)
    seeds = [seed if seed is not None and i == 0
             else rng.randrange(1, 2 ** 31) for i in range(count)]

    queued = []
    for index, one in enumerate(seeds):
        if progress:
            progress(f"queueing {index + 1}/{count}")
        queued.append((submit(build_graph(raw, prompt, one)), one))

    images, deadline = [], time.time() + timeout_s
    for index, (prompt_id, one) in enumerate(queued):
        while True:
            done = outputs_for(prompt_id)
            if done is not None:
                for image in done:
                    images.append({**image, "seed": one})
                break
            if time.time() > deadline:
                raise GenerateError(
                    f"timed out after {timeout_s:.0f}s with {len(images)} of "
                    f"{count} rendered. ComfyUI may still be working — the "
                    f"queue is not cancelled.")
            if progress:
                progress(f"rendering {index + 1}/{count}")
            time.sleep(POLL_INTERVAL_S)

    return {"workflow": used, "prompt": prompt, "count": len(images),
            "seeds": seeds, "images": images, "vram": headroom,
            "warning": warning}
