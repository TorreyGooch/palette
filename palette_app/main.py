import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, UploadFile, File, Body
from fastapi.responses import FileResponse
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

app = FastAPI(title="PALETTE")
app.mount(
    "/static",
    StaticFiles(directory=str(TOOL_DIR / "frontend" / "static")),
    name="static",
)


@app.get("/")
def serve_frontend():
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
    for p in [root / "media" / item["filename"], root / "thumbnails" / f"{iid}.jpg"]:
        if p.exists():
            p.unlink()
    lib["items"] = [i for i in lib["items"] if i["id"] != iid]
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

@app.get("/api/qs/status")
def qs_status():
    from quotesource.status import corpus_status

    try:
        return corpus_status()
    except Exception as e:
        raise HTTPException(409, str(e))


@app.get("/api/qs/search")
def qs_search(q: str, mode: str = "semantic", source: Optional[str] = None,
              person: Optional[str] = None, after: Optional[str] = None,
              before: Optional[str] = None, limit: int = 20):
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


@app.get("/api/qs/context")
def qs_context(episode_id: str, start: float, end: float, window: float = 20.0):
    from quotesource.search import context

    try:
        return context(episode_id, range_=(start - window, end + window))
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))


# Pull runs as a job: POST starts it, GET polls stage/elapsed/result.
_pull_jobs: dict = {}


@app.post("/api/qs/pull")
def qs_pull(body: dict = Body(...)):
    import threading
    import uuid as _uuid
    from datetime import datetime as _dt

    from quotesource.pull import pull

    job_id = str(_uuid.uuid4())[:8]
    job = {"stage": "queued", "started": _dt.now().isoformat(),
           "done": False, "error": None, "item": None}
    _pull_jobs[job_id] = job

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
            )
            job["stage"] = "done"
        except Exception as e:
            job["error"] = str(e)
            job["stage"] = "failed"
        finally:
            job["done"] = True

    threading.Thread(target=_run_job, daemon=True).start()
    return {"job_id": job_id}


@app.get("/api/qs/pull/{job_id}")
def qs_pull_status(job_id: str):
    job = _pull_jobs.get(job_id)
    if not job:
        raise HTTPException(404, "unknown job")
    return job


@app.post("/api/qs/cut")
def qs_cut(body: dict = Body(...)):
    """Word-accurate cut. Same job/polling contract as /api/qs/pull."""
    import threading
    import uuid as _uuid
    from datetime import datetime as _dt

    from quotesource.cut import cut_quote

    job_id = str(_uuid.uuid4())[:8]
    job = {"stage": "queued", "started": _dt.now().isoformat(),
           "done": False, "error": None, "item": None}
    _pull_jobs[job_id] = job

    def _run_job():
        try:
            res = cut_quote(
                body["episode_id"],
                float(body["start"]), float(body["end"]),
                palette_name=body.get("palette") or None,
                person=body.get("person") or None,
                model_size=body.get("model") or None,
                progress_cb=lambda stage: job.__setitem__("stage", stage),
            )
            job["item"] = {"title": res["filename"], **res}
            job["stage"] = "done"
        except Exception as e:
            job["error"] = str(e)
            job["stage"] = "failed"
        finally:
            job["done"] = True

    threading.Thread(target=_run_job, daemon=True).start()
    return {"job_id": job_id}


_warming: set = set()


@app.post("/api/qs/warm")
def qs_warm(body: dict = Body(...)):
    """Start caching an episode's media in the background so a subsequent
    pull is near-instant. Fire-and-forget; safe to call repeatedly."""
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
