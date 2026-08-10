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
    new_item, new_palette, media_type,
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
    """Probe (if video), thumbnail, and add a media file to the library."""
    path = root / "media" / filename
    duration = fps = None
    if media_type(filename) == "video":
        info = await probe(path)
        duration, fps = info["duration"], info["fps"]
    item = new_item(filename, title, url, duration, fps)

    thumb = root / "thumbnails" / f"{item['id']}.jpg"
    if item["type"] == "video":
        await video_thumbnail(path, thumb, (duration or 1) / 2)
    elif item["type"] == "image":
        image_thumbnail(path, thumb)

    lib = load_library(root)
    lib["items"].append(item)
    save_library(root, lib)
    return item


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
