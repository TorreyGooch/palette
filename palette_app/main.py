import json
import os
import shutil
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, UploadFile, File, Body
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from .config import get_library_path, set_library_path
from .library import (
    create_library, load_library, save_library, is_library,
    new_item, new_palette, media_type, register_media_file,
)
from .api.download import download_url
from .api.media import (
    probe, video_thumbnail, image_thumbnail, extract_clip,
    contact_sheet, export_video, ffmpeg_available,
)

TOOL_DIR = Path(__file__).parent.parent

# What this build can do, advertised on /api/qs/status so a client can tell
# whether a feature is safe to rely on. A bare commit says the two sides
# differ; it does not say whether the difference matters. An older server
# simply advertises fewer of these, so support degrades instead of failing
# silently — which is exactly how the staging flag went unnoticed.
API_VERSION = 2
CAPABILITIES = [
    "stage",       # pull/cut honour stage=False instead of always staging
    "discard",     # /api/qs/discard removes a handed-over clip
    "api_only",    # PALETTE_API_ONLY serves an explanation instead of the UI
    "job_boot_id",  # job ids identify the process, so a restart is legible
    "words",       # /api/qs/words exposes word timings for picking boundaries
]


def _build_version() -> str:
    """Short commit of the running tree, for humans reading a status page."""
    import subprocess

    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             cwd=str(TOOL_DIR), capture_output=True,
                             text=True, timeout=5)
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


BUILD_VERSION = _build_version()

app = FastAPI(title="PALETTE")
app.mount(
    "/static",
    StaticFiles(directory=str(TOOL_DIR / "frontend" / "static")),
    name="static",
)


# An instance that exists only to serve a corpus should not also hand out a
# UI: two palettes that look identical but hold different libraries is how
# you end up tagging clips into the wrong one, or thinking a delete failed.
API_ONLY = os.environ.get("PALETTE_API_ONLY", "").strip() not in ("", "0")

API_ONLY_PAGE = """<!doctype html><meta charset="utf-8">
<title>quotesource API</title>
<style>body{font:16px/1.6 system-ui;margin:12vh auto;max-width:34rem;padding:0 1.5rem;
background:#14161a;color:#e6e6e6}code{background:#232730;padding:.15em .4em;
border-radius:4px}a{color:#79b8ff}</style>
<h1>quotesource API</h1>
<p>This is the corpus API, not your library. It has the transcripts and the
GPU; it does <strong>not</strong> have your media.</p>
<p>Anything you stage here lands in <em>this</em> machine's library, not yours.
Open palette on your own machine instead — it queries this one over
<code>QS_REMOTE</code> and keeps the clips locally.</p>
<p><a href="/api/qs/status">/api/qs/status</a></p>"""


@app.get("/")
def serve_frontend():
    if API_ONLY:
        return HTMLResponse(API_ONLY_PAGE)
    return FileResponse(str(TOOL_DIR / "frontend" / "index.html"))


def _root() -> Path:
    root = get_library_path()
    if not root:
        raise HTTPException(409, "No library configured")
    return root


def _find(items: list, id_val: str) -> Optional[dict]:
    return next((x for x in items if x.get("id") == id_val), None)


async def _register_file(root: Path, filename: str, title: str, url: Optional[str] = None) -> dict:
    return await register_media_file(root, filename, title, url)


# ── Library setup ─────────────────────────────────────────────────────────────

@app.get("/api/library")
def library_status():
    root = get_library_path()
    if not root:
        return {"configured": False}
    lib = load_library(root)
    return {
        "configured": True,
        "path": str(root),
        "item_count": len(lib["items"]),
        "ffmpeg": ffmpeg_available(),
    }


@app.post("/api/library")
async def setup_library(body: dict = Body(...)):
    path = (body.get("path") or "").strip()
    if not path:
        raise HTTPException(400, "path required")
    root = Path(path)
    if not is_library(root):
        create_library(root)
    set_library_path(str(root))
    return {"configured": True, "path": str(root)}


# ── Items ─────────────────────────────────────────────────────────────────────

@app.get("/api/items")
def list_items(tag: Optional[str] = None, palette: Optional[str] = None,
               type: Optional[str] = None):
    root = _root()
    lib = load_library(root)
    items = lib["items"]
    if tag:
        items = [i for i in items if tag in i["tags"]]
    if palette:
        items = [i for i in items if palette in i["palettes"]]
    if type:
        items = [i for i in items if i["type"] == type]
    return items


@app.get("/api/tags")
def list_tags():
    root = _root()
    lib = load_library(root)
    tags = {}
    for i in lib["items"]:
        for t in i["tags"]:
            tags[t] = tags.get(t, 0) + 1
    return [{"tag": t, "count": c} for t, c in sorted(tags.items())]


@app.post("/api/items/download")
async def download_item(body: dict = Body(...)):
    root = _root()
    url = (body.get("url") or "").strip()
    if not url:
        raise HTTPException(400, "url required")
    start_time = body.get("start_time") or None
    end_time = body.get("end_time") or None

    results = await download_url(url, root / "media", start_time, end_time)
    added = []
    for r in results:
        item = await _register_file(root, Path(r["filename"]).name, r["title"], url)
        added.append(item)
    return added


@app.post("/api/items/import")
async def import_item(file: UploadFile = File(...)):
    root = _root()
    dest = root / "media" / file.filename
    with open(dest, "wb") as f:
        shutil.copyfileobj(file.file, f)
    title = Path(file.filename).stem.replace("_", " ").replace("-", " ")
    return await _register_file(root, file.filename, title)


@app.patch("/api/items/{iid}")
async def update_item(iid: str, body: dict = Body(...)):
    root = _root()
    lib = load_library(root)
    item = _find(lib["items"], iid)
    if not item:
        raise HTTPException(404, "Item not found")
    for field in ("title", "tags", "palettes"):
        if field in body:
            item[field] = body[field]
    save_library(root, lib)
    return item


@app.post("/api/items/batch-tag")
async def batch_tag(body: dict = Body(...)):
    root = _root()
    lib = load_library(root)
    ids = set(body.get("item_ids", []))
    tag = (body.get("tag") or "").strip()
    action = body.get("action", "add")
    updated = []
    for item in lib["items"]:
        if item["id"] not in ids:
            continue
        if action == "add" and tag and tag not in item["tags"]:
            item["tags"].append(tag)
            updated.append(item["id"])
        elif action == "remove" and tag in item["tags"]:
            item["tags"].remove(tag)
            updated.append(item["id"])
    save_library(root, lib)
    return {"updated": updated}


@app.post("/api/items/batch-palette")
async def batch_palette(body: dict = Body(...)):
    root = _root()
    lib = load_library(root)
    ids = set(body.get("item_ids", []))
    pid = body.get("palette_id")
    action = body.get("action", "add")
    if not _find(lib["palettes"], pid):
        raise HTTPException(404, "Palette not found")
    for item in lib["items"]:
        if item["id"] not in ids:
            continue
        if action == "add" and pid not in item["palettes"]:
            item["palettes"].append(pid)
        elif action == "remove" and pid in item["palettes"]:
            item["palettes"].remove(pid)
    save_library(root, lib)
    return {"ok": True}


@app.delete("/api/items/{iid}")
async def delete_item(iid: str):
    root = _root()
    lib = load_library(root)
    item = _find(lib["items"], iid)
    if not item:
        raise HTTPException(404)

    remaining = [i for i in lib["items"] if i["id"] != iid]

    # Several items can point at one file — clip filenames truncate their
    # bounds to whole seconds, so two cuts a fraction apart collide, and
    # re-staging the same quote makes another item. Unlinking on the first
    # delete would leave every other item pointing at nothing, so the media
    # only goes when its last reference does.
    if not any(i["filename"] == item["filename"] for i in remaining):
        media = root / "media" / item["filename"]
        if media.exists():
            media.unlink()
        # The word manifest is part of the clip, not a separate asset; left
        # behind it just accumulates beside media that no longer exists.
        sidecar = media.with_suffix(".words.json")
        if sidecar.exists():
            sidecar.unlink()

    thumb = root / "thumbnails" / f"{iid}.jpg"  # keyed by item id, never shared
    if thumb.exists():
        thumb.unlink()

    lib["items"] = remaining
    save_library(root, lib)
    return {"ok": True}


# ── Palettes ──────────────────────────────────────────────────────────────────

@app.get("/api/palettes")
def list_palettes():
    root = _root()
    lib = load_library(root)
    counts = {}
    for i in lib["items"]:
        for pid in i["palettes"]:
            counts[pid] = counts.get(pid, 0) + 1
    return [{**p, "count": counts.get(p["id"], 0)} for p in lib["palettes"]]


@app.post("/api/palettes")
async def create_palette_ep(body: dict = Body(...)):
    root = _root()
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name required")
    lib = load_library(root)
    p = new_palette(name)
    lib["palettes"].append(p)
    save_library(root, lib)
    return p


@app.patch("/api/palettes/{pid}")
async def rename_palette(pid: str, body: dict = Body(...)):
    root = _root()
    lib = load_library(root)
    p = _find(lib["palettes"], pid)
    if not p:
        raise HTTPException(404)
    if "name" in body:
        p["name"] = body["name"]
    save_library(root, lib)
    return p


@app.delete("/api/palettes/{pid}")
async def delete_palette(pid: str):
    root = _root()
    lib = load_library(root)
    lib["palettes"] = [p for p in lib["palettes"] if p["id"] != pid]
    for item in lib["items"]:
        if pid in item["palettes"]:
            item["palettes"].remove(pid)
    save_library(root, lib)
    return {"ok": True}


# ── Media serving ─────────────────────────────────────────────────────────────

@app.get("/api/media/{filename:path}")
def serve_media(filename: str):
    root = _root()
    path = root / "media" / filename
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(str(path))


@app.get("/api/items/{iid}/thumbnail")
def serve_thumbnail(iid: str):
    root = _root()
    thumb = root / "thumbnails" / f"{iid}.jpg"
    if not thumb.exists():
        raise HTTPException(404)
    return FileResponse(str(thumb), media_type="image/jpeg")


@app.get("/api/items/{iid}/fps")
def item_fps(iid: str):
    root = _root()
    lib = load_library(root)
    item = _find(lib["items"], iid)
    if not item:
        raise HTTPException(404)
    return {"fps": item.get("fps") or 30, "duration": item.get("duration")}


# ── Clip extraction (keyframe workflow) ───────────────────────────────────────

@app.post("/api/items/{iid}/extract")
async def extract_clips(iid: str, body: dict = Body(...)):
    root = _root()
    lib = load_library(root)
    src = _find(lib["items"], iid)
    if not src or src["type"] != "video":
        raise HTTPException(404, "Video item not found")

    segments = body.get("segments", [])
    src_path = root / "media" / src["filename"]
    base = Path(src["filename"]).stem
    added = []

    for seg in segments:
        start, end = float(seg["start"]), float(seg["end"])
        n = 1
        while (root / "media" / f"{base}_clip_{n:03d}.mp4").exists():
            n += 1
        clip_name = f"{base}_clip_{n:03d}.mp4"
        dest = root / "media" / clip_name
        ok = await extract_clip(src_path, dest, start, end)
        if not ok:
            continue
        item = await _register_file(root, clip_name, f"{src['title']} clip {n}")
        # inherit tags and palettes from source
        lib2 = load_library(root)
        it = _find(lib2["items"], item["id"])
        it["tags"] = list(src["tags"])
        it["palettes"] = list(src["palettes"])
        save_library(root, lib2)
        added.append(it)

    return added


# ── Exports ───────────────────────────────────────────────────────────────────

@app.post("/api/export/contact-sheet")
async def export_contact_sheet(body: dict = Body(...)):
    root = _root()
    lib = load_library(root)
    item = _find(lib["items"], body.get("item_id"))
    if not item or item["type"] != "video":
        raise HTTPException(404, "Video item not found")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = Path(item["filename"]).stem
    out_name = f"{base}_sheet_{stamp}.jpg"
    out_path = root / "exports" / out_name

    result = await contact_sheet(
        root / "media" / item["filename"],
        out_path,
        every_n=int(body.get("every_n", 30)),
        cols=int(body.get("cols", 4)),
        tile_width=int(body.get("tile_width", 320)),
        padding=int(body.get("padding", 8)),
        order=body.get("order", "rows"),
        labels=bool(body.get("labels", False)),
        max_width=int(body["max_width"]) if body.get("max_width") else None,
        start=float(body["start"]) if body.get("start") not in (None, "") else None,
        end=float(body["end"]) if body.get("end") not in (None, "") else None,
    )
    if not result.get("ok"):
        raise HTTPException(500, f"Contact sheet failed: {result.get('error', 'unknown')}")
    return {**result, "filename": out_name}


@app.post("/api/export/video")
async def export_video_ep(body: dict = Body(...)):
    root = _root()
    lib = load_library(root)
    item = _find(lib["items"], body.get("item_id"))
    if not item or item["type"] != "video":
        raise HTTPException(404, "Video item not found")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = Path(item["filename"]).stem
    out_name = f"{base}_export_{stamp}.mp4"
    out_path = root / "exports" / out_name

    result = await export_video(
        root / "media" / item["filename"],
        out_path,
        start=float(body["start"]) if body.get("start") not in (None, "") else None,
        end=float(body["end"]) if body.get("end") not in (None, "") else None,
        scale_width=int(body["scale_width"]) if body.get("scale_width") else None,
        fps=float(body["fps"]) if body.get("fps") else None,
    )
    if not result.get("ok"):
        raise HTTPException(500, f"Video export failed: {result.get('error', 'unknown')}")
    return {**result, "filename": out_name}


# ── Quotesource bridge ────────────────────────────────────────────────────────
# Sync endpoints on purpose: FastAPI runs them in a threadpool, and the
# quotesource library is synchronous (sqlite + subprocess via asyncio.run).
#
# When QS_REMOTE is set the corpus lives on another machine (see qs_remote):
# these forward there instead of importing quotesource locally, so the media
# library can stay on the machine you review video on.

def _remote():
    from .qs_remote import remote_base

    return remote_base()


def _remote_call(fn):
    """Run a remote call, mapping transport failures onto HTTP responses."""
    from .qs_remote import RemoteError

    try:
        return fn()
    except RemoteError as e:
        raise HTTPException(e.status, str(e)) from None


def _palette_block() -> dict:
    return {"version": BUILD_VERSION, "api": API_VERSION,
            "capabilities": CAPABILITIES, "boot_id": BOOT_ID}


@app.get("/api/qs/status")
def qs_status():
    if _remote():
        from . import qs_remote

        status = _remote_call(lambda: qs_remote.get("/api/qs/status"))
        status["remote"] = _remote()
        # The forwarded body carries the remote's own palette block; keep it
        # under a separate key so both sides are visible at once.
        status["remote_palette"] = status.get("palette")
        status["palette"] = _palette_block()
        return status

    from quotesource.status import corpus_status

    try:
        status = corpus_status()
        status["palette"] = _palette_block()
        return status
    except Exception as e:
        raise HTTPException(409, str(e))


@app.get("/api/qs/search")
def qs_search(q: str, mode: str = "semantic", source: Optional[str] = None,
              person: Optional[str] = None, after: Optional[str] = None,
              before: Optional[str] = None, limit: int = 20):
    if _remote():
        from . import qs_remote

        return _remote_call(lambda: qs_remote.get("/api/qs/search", {
            "q": q, "mode": mode, "source": source, "person": person,
            "after": after, "before": before, "limit": limit}))

    try:
        if mode == "grep":
            from quotesource.search import grep

            return {"mode": "grep", "hits": grep(
                q, source=source, person=person, after=after,
                before=before, limit=limit)}
        from quotesource.embedder import embed_stats, semantic_search

        stats = embed_stats()
        if stats["embedded"] == 0:
            raise HTTPException(409, "no embeddings yet — run: qs embed")
        return {"mode": "semantic", "coverage": stats["coverage"],
                "hits": semantic_search(q, source=source, person=person,
                                        after=after, before=before, limit=limit)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/qs/words")
def qs_words(episode_id: str, start: float, end: float, pad: float = 0.0,
             model: Optional[str] = None):
    """Word timings and pauses, for choosing cut boundaries.

    The step most worth not skipping: caption timestamps are far too coarse
    to see a pause, and a cut that ends anywhere but just before one gets a
    faded run-on. Whisper runs on the window only, so this is seconds on the
    GPU rather than a transcription job.
    """
    if _remote():
        from . import qs_remote

        return _remote_call(lambda: qs_remote.get("/api/qs/words", {
            "episode_id": episode_id, "start": start, "end": end,
            "pad": pad, "model": model}, timeout=300))

    from quotesource.cut import word_map

    try:
        return word_map(episode_id, start, end, pad=pad, model_size=model)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))


@app.get("/api/qs/context")
def qs_context(episode_id: str, start: float, end: float, window: float = 20.0):
    if _remote():
        from . import qs_remote

        return _remote_call(lambda: qs_remote.get("/api/qs/context", {
            "episode_id": episode_id, "start": start,
            "end": end, "window": window}))

    from quotesource.search import context

    try:
        return context(episode_id, range_=(start - window, end + window))
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))


# Pull runs as a job: POST starts it, GET polls stage/elapsed/result.
_pull_jobs: dict = {}
_jobs_lock = threading.Lock()

# Jobs live in memory, so a restart loses them. Ids carry the id of the
# process that made them, which lets a client polling across a restart be
# told what actually happened instead of getting a bare "unknown job".
BOOT_ID = uuid.uuid4().hex[:8]

JOB_TTL_S = float(os.environ.get("PALETTE_JOB_TTL_S", "3600"))
JOB_MAX = int(os.environ.get("PALETTE_JOB_MAX", "200"))


def _prune_jobs():
    """Forget finished jobs. Running ones are never dropped.

    A CLI process exited and took its jobs with it; a server running for
    weeks accumulates one dict entry per pull forever.
    """
    now = time.time()
    finished = [(jid, j) for jid, j in _pull_jobs.items() if j.get("done")]
    for jid, job in finished:
        if now - job.get("finished_at", now) > JOB_TTL_S:
            _pull_jobs.pop(jid, None)

    over = len(_pull_jobs) - JOB_MAX
    if over > 0:
        oldest = sorted((kv for kv in _pull_jobs.items() if kv[1].get("done")),
                        key=lambda kv: kv[1].get("finished_at", 0.0))
        for jid, _ in oldest[:over]:
            _pull_jobs.pop(jid, None)


def _new_job(**extra) -> tuple:
    """Register a job and return (job_id, job)."""
    job_id = f"{BOOT_ID}-{uuid.uuid4().hex[:8]}"
    job = {"stage": "queued", "started": datetime.now().isoformat(),
           "done": False, "error": None, "item": None}
    job.update(extra)
    with _jobs_lock:
        _prune_jobs()
        _pull_jobs[job_id] = job
    return job_id, job


def _finish_job(job: dict):
    job["done"] = True
    job["finished_at"] = time.time()


def _start_remote_job(kind: str, body: dict):
    """Mirror a remote pull/cut as a local job.

    The remote does the work and stages the clip into its own library; we
    follow its progress, then copy the result into ours. The browser polls
    the same endpoints either way and cannot tell the difference.
    """
    from . import qs_remote

    job_id, job = _new_job(remote=True)

    def _run_job():
        import asyncio as _asyncio

        try:
            # The remote produces the clip; this library owns it. Staging it
            # there too would leave a second copy on a machine that never
            # opens it, on a disk shared with model checkpoints.
            #
            # An older server does not know the flag, ignores it, and stages
            # anyway — silently, which is the failure mode worth naming.
            if not qs_remote.remote_supports("stage"):
                job["warning"] = (
                    "remote is too old to skip staging; it will keep its own "
                    "copy of this clip")
            started = qs_remote.post(f"/api/qs/{kind}", {**body, "stage": False})
            remote_id = started["job_id"]
            job["stage"] = "remote:queued"

            def _mirror(j):
                job["stage"] = f"remote:{j.get('stage', '?')}"

            deadline = time.time() + 1800
            finished = None
            while time.time() < deadline:
                j = qs_remote.get(f"/api/qs/pull/{remote_id}", timeout=30)
                _mirror(j)
                if j.get("done"):
                    finished = j
                    break
                time.sleep(1.0)
            if finished is None:
                raise qs_remote.RemoteError(f"remote {kind} did not finish", 504)
            if finished.get("error"):
                raise RuntimeError(finished["error"])

            job["stage"] = "downloading"
            # Palette and person are reapplied here rather than copied from
            # the remote: its palette ids belong to its own library.
            job["item"] = _asyncio.run(qs_remote.adopt_remote_item(
                _root(), finished["item"],
                palette_name=body.get("palette") or None,
                person=body.get("person") or None,
                kind=kind))

            # Only once the clip is safely here. Failing to clean up leaves
            # a stray file; cleaning up too early loses it outright.
            try:
                qs_remote.post("/api/qs/discard",
                               {"filename": finished["item"]["filename"]},
                               timeout=30)
            except qs_remote.RemoteError:
                pass  # a leftover file is not worth failing an adopted clip

            job["stage"] = "done"
        except Exception as e:
            job["error"] = str(e)
            job["stage"] = "failed"
        finally:
            _finish_job(job)

    threading.Thread(target=_run_job, daemon=True).start()
    return {"job_id": job_id}


@app.post("/api/qs/pull")
def qs_pull(body: dict = Body(...)):
    if _remote():
        return _start_remote_job("pull", body)

    from quotesource.pull import pull

    job_id, job = _new_job()

    def _run_job():
        try:
            job["item"] = pull(
                body["episode_id"],
                float(body["start"]), float(body["end"]),
                mode=body.get("mode", "av"),
                palette_name=body.get("palette") or None,
                person=body.get("person") or None,
                pad=float(body.get("pad") or 0),
                rough=bool(body.get("rough", False)),
                progress_cb=lambda stage: job.__setitem__("stage", stage),
                stage=bool(body.get("stage", True)),
                outbox=body.get("outbox") or None,
            )
            job["stage"] = "done"
        except Exception as e:
            job["error"] = str(e)
            job["stage"] = "failed"
        finally:
            _finish_job(job)

    threading.Thread(target=_run_job, daemon=True).start()
    return {"job_id": job_id}


# ── Controlling the corpus API from here ──────────────────────────────────────
# The server API is started on demand: that box shares memory and GPU with
# generation, so it should not idle there holding an embedding model. To
# start something that is not running, ssh is the only channel available -
# an always-on supervisor would be the very thing we are avoiding.

SERVER_ACTIONS = ("start", "stop", "restart", "status")


def _server_ssh() -> Optional[str]:
    """user@host for the corpus machine, inferred from QS_REMOTE if unset."""
    explicit = (os.environ.get("QS_SERVER_SSH") or "").strip()
    if explicit:
        return explicit
    from .qs_remote import remote_base

    base = remote_base()
    if not base:
        return None
    host = base.split("://", 1)[-1].split("/")[0].split(":")[0]
    user = os.environ.get("QS_SERVER_USER", "torrey")
    return f"{user}@{host}"


def _run_server_action(action: str) -> dict:
    import subprocess

    # Whitelisted above; nothing from the request is interpolated into the
    # command, so there is no shell for a caller to reach.
    if action not in SERVER_ACTIONS:
        raise HTTPException(400, f"action must be one of {SERVER_ACTIONS}")

    target = _server_ssh()
    if not target:
        raise HTTPException(409, "no corpus server configured (set QS_REMOTE)")

    script = os.environ.get("QS_SERVER_SCRIPT", "~/palette/server-app.sh")
    timeout = 90 if action in ("start", "restart") else 45
    try:
        proc = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
             target, f"bash {script} {action}"],
            capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise HTTPException(500, "ssh is not available on this machine") from None
    except subprocess.TimeoutExpired:
        raise HTTPException(504, f"'{action}' timed out talking to {target}") from None

    # server-app.sh prints JSON on stdout and human notes on stderr, so a
    # chatty start still parses.
    note = (proc.stderr or "").strip()
    try:
        state = json.loads(proc.stdout)
    except (json.JSONDecodeError, ValueError):
        detail = note or (proc.stdout or "").strip() or "no output"
        raise HTTPException(502, f"{target}: {detail[:300]}") from None

    state["action"] = action
    state["host"] = target
    if note:
        state["note"] = note.splitlines()[-1]
    return state


@app.get("/api/qs/server")
def qs_server_status():
    return _run_server_action("status")


@app.post("/api/qs/server")
def qs_server_control(body: dict = Body(...)):
    return _run_server_action((body.get("action") or "").strip().lower())


@app.post("/api/qs/discard")
def qs_discard(body: dict = Body(...)):
    """Delete a clip whose caller has taken ownership of it.

    A remote pull/cut runs with stage=False, so the file sits in this
    library's media folder without a library entry. Once the caller has it,
    keeping the bytes here just accumulates media this machine never opens.
    """
    if _remote():
        from . import qs_remote

        return _remote_call(lambda: qs_remote.post("/api/qs/discard", body, timeout=30))

    root = _root()
    filename = (body.get("filename") or "").strip()
    if not filename:
        raise HTTPException(400, "filename required")

    media = (root / "media").resolve()
    target = (media / filename).resolve()
    # The name arrives over the network, so refuse anything that resolves
    # outside the media folder rather than trusting it.
    if target.parent != media:
        raise HTTPException(400, "filename must name a file in media/")

    lib = load_library(root)
    if any(i["filename"] == filename for i in lib["items"]):
        return {"discarded": False, "reason": "this library references it"}

    removed = []
    for path in (target, target.with_suffix(".words.json")):
        if path.exists():
            path.unlink()
            removed.append(path.name)
    return {"discarded": bool(removed), "removed": removed}


@app.get("/api/qs/pull/{job_id}")
def qs_pull_status(job_id: str):
    job = _pull_jobs.get(job_id)
    if job:
        return job
    # An id stamped by an earlier process means the server restarted, which
    # is a different story from a bad id and worth telling apart: the work
    # is gone and will not come back, so the caller should stop polling.
    if "-" in job_id and not job_id.startswith(f"{BOOT_ID}-"):
        raise HTTPException(
            410, "the server restarted after this job started; it is gone")
    raise HTTPException(404, "unknown job")


@app.post("/api/qs/cut")
def qs_cut(body: dict = Body(...)):
    """Word-accurate cut. Same job/polling contract as /api/qs/pull."""
    if _remote():
        return _start_remote_job("cut", body)

    from quotesource.cut import cut_quote

    job_id, job = _new_job()

    def _run_job():
        try:
            res = cut_quote(
                body["episode_id"],
                float(body["start"]), float(body["end"]),
                palette_name=body.get("palette") or None,
                person=body.get("person") or None,
                model_size=body.get("model") or None,
                progress_cb=lambda stage: job.__setitem__("stage", stage),
                stage=bool(body.get("stage", True)),
                outbox=body.get("outbox") or None,
            )
            job["item"] = {"title": res["filename"], **res}
            job["stage"] = "done"
        except Exception as e:
            job["error"] = str(e)
            job["stage"] = "failed"
        finally:
            _finish_job(job)

    threading.Thread(target=_run_job, daemon=True).start()
    return {"job_id": job_id}


_warming: set = set()


@app.post("/api/qs/warm")
def qs_warm(body: dict = Body(...)):
    """Start caching an episode's media in the background so a subsequent
    pull is near-instant. Fire-and-forget; safe to call repeatedly."""
    if _remote():
        # The cache that matters is the remote's - that is where the media is
        # fetched and cut. Best effort by design: warming is an optimisation.
        from . import qs_remote

        try:
            return qs_remote.post("/api/qs/warm", body, timeout=30)
        except qs_remote.RemoteError as e:
            return {"status": "unavailable", "detail": str(e)}

    import asyncio as _asyncio
    import threading

    from quotesource.pull import _get_full_media
    from quotesource.search import _find_episode_dir

    episode_id = body.get("episode_id")
    mode = body.get("mode", "av")
    if not episode_id:
        raise HTTPException(400, "episode_id required")
    key = f"{episode_id}:{mode}"
    if key in _warming:
        return {"status": "already_warming"}

    ep_dir = _find_episode_dir(episode_id)
    if not ep_dir:
        raise HTTPException(404, "episode not found")
    meta = json.loads((ep_dir / "metadata.json").read_text(encoding="utf-8"))

    def _run():
        try:
            _asyncio.run(_get_full_media(episode_id, meta.get("url", ""), mode, ep_dir))
        except Exception:
            pass
        finally:
            _warming.discard(key)

    _warming.add(key)
    threading.Thread(target=_run, daemon=True).start()
    return {"status": "warming"}


@app.get("/api/exports")
def list_exports():
    root = _root()
    files = sorted(
        (root / "exports").iterdir(),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return [
        {"filename": f.name, "size_bytes": f.stat().st_size}
        for f in files if f.is_file()
    ]


@app.get("/api/exports/{filename:path}")
def serve_export(filename: str):
    root = _root()
    path = root / "exports" / filename
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(str(path))
