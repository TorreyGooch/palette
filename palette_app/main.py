import json
import os
import re
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
    create_library, load_library, save_library, is_library, library_lock,
    new_item, new_palette, media_type, register_media_file,
)
from .narration import bind as narration_bind, lay_out as narration_layout
from .storyboard import (
    DEFAULT_ASPECT, delete_board, derive_frame, list_boards, load_board,
    new_board, new_panel, render_storyboard, save_board, slugify,
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
    "recut",       # /api/qs/recut corrects a clip's bounds in place
    "words",       # /api/qs/words exposes word timings for picking boundaries
    "word_index",  # words/pauses carry positions, not just spellings
    "hit_audio",   # search hits carry audio_stored, so cost is known up front
    "clip_words",  # /api/items/{id}/words serves a clip's indexed manifest
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


_ASSET_REF = re.compile(r'(?P<attr>(?:src|href)=")(?P<path>/static/[^"?]+)"')


def _stamp_assets(html: str) -> str:
    """Give every static reference a version taken from the file's mtime.

    Without this the browser keeps whatever it cached, so a shipped frontend
    change stays invisible to the person it was built for until they happen to
    hard-reload - and it discredits work that did ship. It cost exactly that:
    a board rendering "image unavailable" on beats whose narration UI had been
    live for hours, and a reasonable conclusion that the feature was missing.

    mtime rather than the build version, because it changes when the file
    changes rather than when a commit happens, so an uncommitted edit busts
    the cache too.
    """
    static_root = TOOL_DIR / "frontend" / "static"

    def stamp(match):
        rel = match.group("path")[len("/static/"):]
        try:
            version = int((static_root / rel).stat().st_mtime)
        except OSError:
            return match.group(0)        # not ours to stamp; leave it alone
        return f'{match.group("attr")}{match.group("path")}?v={version}"'

    return _ASSET_REF.sub(stamp, html)


@app.get("/")
def serve_frontend():
    if API_ONLY:
        return HTMLResponse(API_ONLY_PAGE)
    index = TOOL_DIR / "frontend" / "index.html"
    return HTMLResponse(_stamp_assets(index.read_text(encoding="utf-8")))


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
    with library_lock(root):
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
    with library_lock(root):
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
    with library_lock(root):
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
    with library_lock(root):
        lib = load_library(root)
        item = _find(lib["items"], iid)
        if not item:
            raise HTTPException(404)

        remaining = [i for i in lib["items"] if i["id"] != iid]

        # Several items can point at one file — re-staging the same quote
        # makes another item, and a filename has never identified one.
        # Unlinking on the first delete would leave every other item pointing
        # at nothing, so the media only goes when its last reference does.
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
    with library_lock(root):
        lib = load_library(root)
        p = new_palette(name)
        lib["palettes"].append(p)
        save_library(root, lib)
    return p


@app.patch("/api/palettes/{pid}")
async def rename_palette(pid: str, body: dict = Body(...)):
    root = _root()
    with library_lock(root):
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
    with library_lock(root):
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


@app.get("/api/items/{iid}/words")
def item_words(iid: str, min_gap: float = 0.15):
    """The clip's own words, indexed, with the pauses between them.

    This closes the seam between choosing and storing. Selection happens in
    seconds — you hear a pause, you pick a boundary — but everything durable
    stores *word indices*, because an index means something to a person
    ("from 'lobster' to 'antidepressants'") and survives a re-cut. Nothing
    joined the two, so the numbers were converted by hand on every beat: the
    same subtraction written four times in one session, with a copy-paste in
    the middle of it.

    `narration.with_gaps` already computed exactly this, but only inside a
    board GET — you had to commit the clip to a beat before you could see the
    indices you needed in order to choose the range. The manifest was
    therefore read off disk instead, going around the API to reach the
    authoritative artifact. This is that artifact, served.

    Cheap on purpose: it reads `<clip>.words.json` and nothing else. No audio,
    no whisper, no GPU, no network — which is why it still works on an episode
    whose media YouTube refuses. `pauses` is usually the only field you need;
    `words` is there when you want to read the span.
    """
    root = _root()
    item = _find(load_library(root)["items"], iid)
    if not item:
        raise HTTPException(404, f"no item '{iid}'")

    from . import narration

    media = root / "media" / (item.get("filename") or "")
    manifest = narration.load_manifest(media)
    words = narration.word_list(manifest)
    if not words:
        # A pull stages audio without a manifest and an imported file never
        # had one. Saying so beats an empty list that reads as "no speech".
        return {"item_id": iid, "filename": item.get("filename"),
                "precision": "clip", "word_count": 0,
                "duration": item.get("duration"), "words": [], "pauses": [],
                "detail": "this clip has no word manifest — only `qs cut` "
                          "writes one, so its words cannot be indexed"}

    detailed = narration.with_gaps(words, 0, len(words) - 1)
    pauses = [
        {"after_index": w["index"] - 1, "after_word": detailed[w["index"] - 1]["word"],
         "next_index": w["index"], "next_word": w["word"],
         "at": detailed[w["index"] - 1]["end"], "gap": w["gap_before"]}
        for w in detailed
        if w["index"] > 0 and w["gap_before"] and w["gap_before"] >= min_gap
    ]
    return {
        "item_id": iid,
        "filename": item.get("filename"),
        "precision": (manifest.get("attribution") or {}).get("precision")
                     or "word_accurate",
        "word_count": len(words),
        "duration": manifest.get("duration") or item.get("duration"),
        "words": detailed,
        "pauses": pauses,
    }


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
        with library_lock(root):
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

    every_n = int(body.get("every_n", 30))
    cols = int(body.get("cols", 4))
    rows = int(body["rows"]) if body.get("rows") not in (None, "", 0, "0") else None
    start = float(body["start"]) if body.get("start") not in (None, "") else None
    end = float(body["end"]) if body.get("end") not in (None, "") else None

    # A series is forty files that belong together, so it gets a folder you can
    # hand over whole. A single sheet is one file and a folder would just be a
    # box around it, so it stays loose in exports/ as it always has.
    if rows:
        # The stamp only resolves to the second. Two renders inside one second
        # would otherwise share a folder, and since a shorter run writes fewer
        # pages the loser keeps the winner's leftover sheets — a folder holding
        # two different renders, with an index describing only one of them.
        series_dir = root / "exports" / f"{base}_sheet_{stamp}"
        n = 2
        while series_dir.exists():
            series_dir = root / "exports" / f"{base}_sheet_{stamp}_{n}"
            n += 1
        series_dir.mkdir(parents=True)
        out_path = series_dir / "sheet.jpg"
    else:
        series_dir = None
        out_path = root / "exports" / f"{base}_sheet_{stamp}.jpg"

    result = await contact_sheet(
        root / "media" / item["filename"],
        out_path,
        every_n=every_n,
        cols=cols,
        tile_width=int(body.get("tile_width", 320)),
        padding=int(body.get("padding", 8)),
        order=body.get("order", "rows"),
        labels=bool(body.get("labels", False)),
        max_width=int(body["max_width"]) if body.get("max_width") else None,
        start=start,
        end=end,
        rows=rows,
        fps=item.get("fps"),
    )
    if not result.get("ok"):
        raise HTTPException(500, f"Contact sheet failed: {result.get('error', 'unknown')}")

    sheets = result.get("sheets", [])

    # A series without an index is a pile of JPEGs. Write one so the session
    # reading the sheets knows what it is looking at and where in the video.
    # Its sheet names stay bare, relative to the folder that holds them, so the
    # folder survives being renamed or moved somewhere else entirely.
    index_name = None
    if series_dir is not None:
        (series_dir / "index.json").write_text(json.dumps({
            "source": item["filename"],
            "title": item.get("title"),
            "duration": item.get("duration"),
            "fps": item.get("fps"),
            "sampled": {"every_n": every_n, "start": start, "end": end},
            "layout": {"cols": cols, "rows": rows,
                       "order": body.get("order", "rows"),
                       "labels": bool(body.get("labels", False))},
            "frames": result.get("frames"),
            "sheets": sheets,
        }, indent=2), encoding="utf-8")
        index_name = f"{series_dir.name}/index.json"

    # URLs are relative to exports/; the series folder is part of the path.
    prefix = f"{series_dir.name}/" if series_dir is not None else ""
    names = [f"{prefix}{s['filename']}" for s in sheets]

    return {**result,
            "sheets": [{**s, "url": f"{prefix}{s['filename']}"} for s in sheets],
            "filename": names[0] if names else out_path.name,
            "filenames": names,
            "index": index_name,
            "dir": series_dir.name if series_dir is not None else None,
            # The whole point of the folder: a path to hand to another session.
            "dir_path": str(series_dir) if series_dir is not None else None}


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


# ── Storyboards ───────────────────────────────────────────────────────────────
# A board is an ordered list of beats. A beat is one moment of the piece: it can
# be seen (a library image), heard (a clip and a range of its words), or both.
# Boards persist as their own documents (see storyboard.py) and reference
# library items by id, so the board itself holds no media.
#
# The words are the spine. Where a beat has narration, its duration comes from
# the audio rather than from anyone's estimate, which is what lets a visual beat
# land on a specific word.

def _opt_int(value):
    """int, or None for anything a form leaves empty."""
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _panel_view(root: Path, lib: dict, panel: dict) -> dict:
    """A stored beat plus what the browser needs to draw and play it.

    The narration's times and text are read back from the word manifest on
    every view rather than stored, so they cannot drift from the audio.
    """
    item = _find(lib["items"], panel["item_id"]) if panel.get("item_id") else None
    src_id = panel.get("source_item_id")
    source = _find(lib["items"], src_id) if src_id else None

    narration = None
    stored = panel.get("narration") or None
    if stored and stored.get("item_id"):
        clip = _find(lib["items"], stored["item_id"])
        if clip:
            narration = narration_bind(root / "media", clip,
                                       stored.get("word_start"),
                                       stored.get("word_end"))
            narration["audio_url"] = f"/api/media/{clip['filename']}"
            narration["attribution"] = clip.get("attribution")
            narration["missing"] = False
        else:
            narration = {**stored, "missing": True}

    return {**panel,
            "image_url": f"/api/media/{item['filename']}" if item else None,
            "title": item.get("title") if item else None,
            # A beat can be heard and not seen. Only call it missing when it
            # names an image that has gone, never when it never had one.
            "missing": panel.get("item_id") is not None and item is None,
            "narration": narration,
            "source_title": source.get("title") if source else None,
            "source_fps": source.get("fps") if source else None}


def _clean_panels(lib: dict, panels) -> list:
    """Normalise incoming beats, deriving what a client must not assert.

    A beat needs a visual, a narration, or a prompt — any one of the three.
    Requiring the image is what made a quote with no picture impossible to
    write down; requiring an *asset* would do the same to a beat that has yet
    to be made, which is exactly what a prompt is for.

    Two things are derived rather than trusted. The frame, because it is only
    meaningful against the source's rate. And the narration's times, of which
    only the *inputs* are stored — which clip, which words — so that the times
    are re-read from the word manifest instead of copied and left to rot.
    """
    out = []
    for p in panels or []:
        item_id = p.get("item_id") or None
        stored = p.get("narration") or {}
        narration_id = stored.get("item_id") or None
        prompt = (p.get("video_prompt") or "").strip()
        if not item_id and not narration_id and not prompt:
            continue        # not seen, not heard, not asked for: not a beat
        tc = p.get("timecode")
        tc = float(tc) if tc not in (None, "") else None
        src = p.get("source_item_id") or None
        frame = p.get("frame")
        frame = int(frame) if frame not in (None, "") else None
        if tc is not None and src:
            source = _find(lib["items"], src)
            derived = derive_frame(tc, (source or {}).get("fps"))
            if derived is not None:
                frame = derived
        narration = None
        if narration_id:
            narration = {"item_id": narration_id,
                         "word_start": _opt_int(stored.get("word_start")),
                         "word_end": _opt_int(stored.get("word_end"))}
        out.append({"id": p.get("id") or str(uuid.uuid4()),
                    "item_id": item_id,
                    "note": p.get("note") or "",
                    "source_item_id": src,
                    "timecode": tc,
                    "frame": frame,
                    "narration": narration,
                    "video_prompt": prompt})
    return out


def _board_view(root: Path, lib: dict, board: dict) -> dict:
    """A board with every beat enriched, plus where each one falls in time."""
    beats = [_panel_view(root, lib, p) for p in board.get("panels", [])]
    return {**board, "panels": beats, "timeline": narration_layout(beats)}


def _require_board(root: Path, bid: str) -> dict:
    try:
        board = load_board(root, bid)
    except ValueError:      # not an id at all — same answer as not found
        board = None
    if not board:
        raise HTTPException(404, "Board not found")
    return board


@app.get("/api/storyboards")
def storyboards_index():
    return list_boards(_root())


@app.post("/api/storyboards")
def storyboard_create(body: dict = Body(...)):
    root = _root()
    return save_board(root, new_board((body.get("name") or "").strip()))


@app.get("/api/storyboards/{bid}")
def storyboard_get(bid: str):
    root = _root()
    board = _require_board(root, bid)
    lib = load_library(root)
    return _board_view(root, lib, board)


@app.patch("/api/storyboards/{bid}")
def storyboard_update(bid: str, body: dict = Body(...)):
    """Rename, and/or replace the panel list wholesale.

    Reorder, edit and delete all arrive as one new list. Panels carry their own
    ids, so a full replace is the same amount of work as a diff and cannot get
    out of step with what the user is looking at.
    """
    root = _root()
    board = _require_board(root, bid)
    lib = load_library(root)
    if "name" in body:
        board["name"] = (body["name"] or "").strip() or board["name"]
    if "panels" in body:
        board["panels"] = _clean_panels(lib, body["panels"])
    save_board(root, board)
    return _board_view(root, lib, board)


@app.delete("/api/storyboards/{bid}")
def storyboard_delete(bid: str):
    try:
        ok = delete_board(_root(), bid)
    except ValueError:
        ok = False
    if not ok:
        raise HTTPException(404, "Board not found")
    return {"deleted": bid}


@app.post("/api/storyboards/{bid}/panels")
def storyboard_add_panels(bid: str, body: dict = Body(...)):
    """Append library items to the end of the board, in the order given."""
    root = _root()
    board = _require_board(root, bid)
    lib = load_library(root)
    added = 0
    for iid in body.get("item_ids") or []:
        item = _find(lib["items"], iid)
        if not item:
            continue
        # What the item *is* decides which half of the beat it fills. Adding a
        # cut quote should give you a beat that speaks, not a blank picture.
        if item.get("type") == "audio":
            board["panels"].append(new_panel(narration={"item_id": iid}))
        else:
            board["panels"].append(new_panel(iid))
        added += 1
    save_board(root, board)
    return {**_board_view(root, lib, board), "added": added}


@app.post("/api/storyboards/{bid}/render")
def storyboard_render(bid: str, body: dict = Body(...)):
    root = _root()
    board = _require_board(root, bid)
    lib = load_library(root)

    panels = []
    for p in board.get("panels", []):
        item = _find(lib["items"], p["item_id"]) if p.get("item_id") else None
        src_id = p.get("source_item_id")
        source = _find(lib["items"], src_id) if src_id else None

        # A beat that speaks renders its words, so the board reads as a script
        # rather than as a grid with holes in it.
        stored = p.get("narration") or {}
        quote = speaker = None
        if stored.get("item_id"):
            clip = _find(lib["items"], stored["item_id"])
            if clip:
                bound = narration_bind(root / "media", clip,
                                       stored.get("word_start"),
                                       stored.get("word_end"))
                quote = bound.get("text")
                speaker = (clip.get("attribution") or {}).get("person")

        panels.append({
            "image": (root / "media" / item["filename"]) if item else None,
            "quote": quote,
            "prompt": (p.get("video_prompt") or "").strip() or None,
            "note": p.get("note") or "",
            "timecode": p.get("timecode"),
            "frame": p.get("frame"),
            "source_title": (source.get("title") if source else None) or speaker,
        })
    if not panels:
        raise HTTPException(400, "Board has no panels")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_name = f"{slugify(board['name'])}_storyboard_{stamp}.png"
    out_path = root / "exports" / out_name

    # An explicit empty title drops the header; anything else names the board.
    title = body.get("title", board.get("name")) or None
    aspect = body.get("aspect")
    result = render_storyboard(
        panels, out_path,
        title=title,
        cols=int(body.get("cols", 3)),
        tile_width=int(body.get("tile_width", 360)),
        aspect=float(aspect) if aspect else DEFAULT_ASPECT,
        padding=int(body.get("padding", 16)),
        max_width=int(body["max_width"]) if body.get("max_width") else None,
    )
    if not result.get("ok"):
        raise HTTPException(
            500, f"Storyboard render failed: {result.get('error', 'unknown')}")
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
        # Semantic search needs the GPU and the embedding model. Keyword
        # search needs neither - it is FTS5, on the CPU - so when the GPU
        # cannot initialise, one of these two still answers. A bare cuBLAS
        # error reads as "the corpus is unreachable" and has already cost a
        # session that had a working search path one parameter away.
        if mode != "grep":
            raise HTTPException(
                500, f"semantic search failed: {e}. Keyword search does not "
                     f"use the GPU and may still work - retry the same query "
                     f"with mode=grep") from None
        raise HTTPException(500, str(e))


# A refused download and an unfetched one look alike and need opposite
# answers, so the markers are named rather than guessed at each call site.
_SOURCE_REFUSED = ("403", "forbidden", "video unavailable", "private video",
                   "sign in to confirm", "removed by the uploader",
                   "not available in your country")


_GPU_BUSY = ("cuda", "out of memory", "cublas", "cudnn", "no kernel image",
             "device-side assert", "cufft")
# What _source_media raises when the fetch it fell back to did not produce a
# file. Authoritative on its own: the disk check can be inconclusive, and
# discarding a signal the error already gave would be worse than not looking.
_NO_AUDIO = ("could not obtain audio",)


def _audio_is_stored(episode_id: str):
    """Whether this episode's audio is on disk, or None if that is unknowable.

    Asked rather than assumed. The message used to *assert* the audio was not
    stored whatever had gone wrong, which is how a CUDA out-of-memory came to
    recommend a 50 MB download that would have fixed nothing.
    """
    try:
        from quotesource.cut import _find_episode_dir
        from quotesource.pull import stored_audio

        ep_dir = _find_episode_dir(episode_id)
        if not ep_dir:
            return None
        return stored_audio(ep_dir) is not None
    except Exception:
        return None


def _words_failure_detail(error: Exception, episode_id: str = "") -> str:
    """Why word timings failed, and what the reader should do about it.

    Several failures produce one symptom and want different responses, so the
    branch that guesses wrong is worse than no advice at all:

      audio not stored     pull it; ~50 MB, and permanent
      source refused       wait; never authenticate
      GPU busy / CUDA OOM  wait and retry; costs nothing

    The last one arrived wearing the first one's explanation - the GPU ran out
    of memory and the reader was told to spend 50 MB on a download that would
    change nothing, while the actual answer went unmentioned. Same shape as
    the 403 before it: a real error landing in the stored-audio branch and
    inheriting its template.

    So the stored-audio claim is now *checked* rather than assumed, and an
    unrecognised failure says what happened instead of inventing a cause.
    """
    text = str(error) or error.__class__.__name__
    lowered = text.lower()

    if any(marker in lowered for marker in _GPU_BUSY):
        # Whisper and the embedding model share one GPU, and a semantic search
        # holds ~3.3 GB until QS_MODEL_IDLE_S (600s) after the last one. So a
        # burst of searching is the usual reason this window will not run, and
        # waiting is both the fix and free.
        return (f"{text}. Word timings need the GPU, and it has no memory free "
                f"right now - nothing is wrong with the audio or the corpus. "
                f"Wait and retry; it costs nothing. A recent semantic search "
                f"may still be holding the embedding model (~3.3 GB), which is "
                f"released about ten minutes after the last search. `context` "
                f"works meanwhile: it reads the transcript from disk.")

    if any(marker in lowered for marker in _SOURCE_REFUSED):
        headline = (f"{text}. This endpoint needs the episode's audio, and "
                    f"the source refused to serve it - a `qs pull` will fail "
                    f"the same way, so this is a wait, not a retry. Never add "
                    f"cookies or a browser user agent to get past it. "
                    f"`context` still works here: it reads the transcript "
                    f"already on disk.")
    elif (any(m in lowered for m in _NO_AUDIO)
          or _audio_is_stored(episode_id) is False):
        headline = (f"{text}. This endpoint needs the episode's audio and it "
                    f"is not stored yet; captions alone are not enough. A "
                    f"`qs pull` on this episode fetches and keeps it (~50 MB), "
                    f"after which words and cuts on it cost no network.")
    else:
        # The audio is present, or it could not be determined. Either way this
        # is not the stored-audio case, and saying it is would send the reader
        # to spend bandwidth on something already there.
        headline = (f"{text}. Word timings failed for a reason this endpoint "
                    f"does not recognise, and the episode's audio is not "
                    f"known to be the problem.")
    # What this does *not* block, which the bare sentence above implies it
    # does. A clip already cut from this episode carries its own per-word
    # timings in the .words.json beside it, so narrowing a beat to a shorter
    # span is a local file read - no audio, no whisper, no network. Every
    # `qs cut` clip has one, which makes that the common case, not the
    # exception: episode audio is for cutting something *new*.
    return headline + (" This blocks new cuts from the episode, not narrowing "
                       "a clip you already have: a cut's .words.json carries "
                       "per-word timings, so re-splitting one by word index "
                       "needs no audio at all.")


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
    except HTTPException:
        raise
    except Exception as e:
        # Word timings are whisper over a window of the *audio*, so an episode
        # holding only captions has nothing to run on and falls through to an
        # on-demand fetch. Catching RuntimeError alone was not enough: the
        # fetch is yt-dlp, and a refused download raises DownloadError, which
        # sailed straight past and reached the browser as a bare 500 reading
        # "the endpoint is broken" - while `context` on the same episode
        # answered perfectly well from the transcript on disk.
        raise HTTPException(
            502, _words_failure_detail(e, episode_id)) from None


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


def _beats_bound_to(root: Path, item_id: str) -> dict:
    """Every storyboard beat that speaks this clip, and what it says now.

    Word indices are *positions* in the clip's manifest. A re-cut regenerates
    that manifest, so a beat's range stays valid while pointing at different
    words - the range survives, the meaning does not. Nothing downstream can
    see it: the beat still renders, the timeline still recomputes, and the
    note above it still describes the quote it used to be. That is the same
    shape as a misaligned cut, fluent and confident and wrong, arriving by a
    door the alignment guard does not watch.

    Called either side of a replace, which is the one moment both the old and
    new manifests exist and the drift is computable at all.
    """
    from .narration import bind as narration_bind
    from .storyboard import list_boards, load_board

    lib = load_library(root)
    clip = _find(lib["items"], item_id)
    out = {}
    for summary in list_boards(root):
        board = load_board(root, summary["id"])
        if not board:
            continue
        for panel in board.get("panels", []):
            stored = panel.get("narration") or {}
            if stored.get("item_id") != item_id:
                continue
            bound = narration_bind(root / "media", clip,
                                   stored.get("word_start"),
                                   stored.get("word_end")) if clip else {}
            out[panel.get("id")] = {
                "board": board.get("name"),
                "board_id": board.get("id"),
                "panel_id": panel.get("id"),
                "word_start": stored.get("word_start"),
                "word_end": stored.get("word_end"),
                "text": bound.get("text"),
                "word_total": bound.get("word_total"),
            }
    return out


def _narration_drift(before: dict, after: dict) -> list:
    """Beats whose words changed under them, reported so a person can look.

    Not corrected automatically: re-anchoring rewrites someone's board, and a
    board is a record of decisions rather than an index to be fixed up. What
    it must not be is silent.
    """
    drifted = []
    for panel_id, was in before.items():
        now = after.get(panel_id)
        if not now or was.get("text") == now.get("text"):
            continue
        drifted.append({**now, "was": was.get("text"),
                        "now": now.get("text"),
                        "word_total_was": was.get("word_total")})
    return drifted


# Keys that mean something only on this side of the bridge.
#
# `replace_item` is a *local* library item id. Cut clips are registered here
# and the server is told to discard its copy, so the remote library is the one
# place guaranteed never to contain it. Forwarding it made the server attempt
# the swap against its own library and fail with "no item <id>" — after the
# whisper pass, so a recut in the bridged configuration could never succeed
# and cost GPU time to find that out.
LOCAL_ONLY_JOB_KEYS = ("replace_item",)


def _remote_job_body(body: dict) -> dict:
    """What of a job request the remote should actually be told."""
    return {k: v for k, v in body.items()
            if k not in LOCAL_ONLY_JOB_KEYS} | {"stage": False}


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
            started = qs_remote.post(f"/api/qs/{kind}",
                                     _remote_job_body(body))
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
            replacing = body.get("replace_item")
            if replacing:
                # A correction, not a new clip: the item keeps its id so
                # every board beat pointing at it survives the edit.
                before_beats = _beats_bound_to(_root(), replacing)
                job["replaced"] = _asyncio.run(qs_remote.replace_remote_item(
                    _root(), replacing, finished["item"]))
                job["replaced"]["beats_drifted"] = _narration_drift(
                    before_beats, _beats_bound_to(_root(), replacing))
                job["item"] = _find(load_library(_root())["items"], replacing)
            else:
                # Palette and person are reapplied here rather than copied
                # from the remote: its palette ids belong to its own library.
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
                # Audio unless asked: ~50 MB against ~2.5 GB for a 2h episode,
                # and only the picture is missing.
                mode=body.get("mode", "audio"),
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


def _ssh_binary() -> str:
    """An ssh that can actually authenticate, rather than whichever is first.

    Windows has two, and they do not share credentials. The OpenSSH client in
    System32 talks to the Windows ssh-agent service; Git for Windows ships its
    own under usr/bin which does not, and answers "Permission denied
    (publickey)" for a key the agent is holding. Which one wins is decided by
    the PATH the app happened to be launched with - a shell difference
    silently changing whether the server controls work.

    Same reasoning as the `./qs` wrapper picking an interpreter that actually
    has faster-whisper instead of trusting `python3`. QS_SSH overrides.
    """
    explicit = os.environ.get("QS_SSH")
    if explicit:
        return explicit
    if os.name == "nt":
        # os.path rather than pathlib: Path() raises on a non-Windows host,
        # which makes this branch impossible to exercise anywhere else and
        # buys nothing here.
        candidate = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                                 "System32", "OpenSSH", "ssh.exe")
        if os.path.exists(candidate):
            return candidate
    return "ssh"


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
            [_ssh_binary(), "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
             target, f"bash {script} {action}"],
            capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise HTTPException(
            500, f"ssh is not available on this machine "
                 f"(tried {_ssh_binary()})") from None
    except subprocess.TimeoutExpired:
        raise HTTPException(504, f"'{action}' timed out talking to {target}") from None

    # server-app.sh prints JSON on stdout and human notes on stderr, so a
    # chatty start still parses.
    note = (proc.stderr or "").strip()
    try:
        state = json.loads(proc.stdout)
    except (json.JSONDecodeError, ValueError):
        detail = note or (proc.stdout or "").strip()
        if not detail:
            # A bare "no output" reads as though the corpus server answered
            # and answered emptily. Usually it means ssh never got there -
            # no key, no route, wrong host - which is a fault on this side.
            # The exit status is the one thing always available to say so.
            detail = (f"ssh exited {proc.returncode} with no output; the "
                      f"transport failed rather than the server")
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


@app.post("/api/qs/recut")
def qs_recut(body: dict = Body(...)):
    """Re-cut an existing clip to new bounds, keeping its identity.

    The correction that used to be impossible without losing something.
    Cutting the same quote again mints a new item, so every storyboard beat
    pointing at the old one is left behind along with the note written under
    it — and until clip filenames carried milliseconds, the new audio also
    overwrote the old file, so the abandoned item silently started playing a
    different sentence.

    Episode and person come from the item's own attribution rather than the
    caller: this is the same quote, moved, and asking for them again is an
    invitation to cite it as something it is not.
    """
    root = _root()
    item_id = (body.get("item_id") or "").strip()
    item = _find(load_library(root)["items"], item_id) if item_id else None
    if not item:
        raise HTTPException(404, "Item not found")

    attribution = item.get("attribution") or {}
    episode_id = attribution.get("episode_id")
    if not episode_id:
        raise HTTPException(
            400, "this item has no attribution, so there is no episode to "
                 "re-cut from. Only clips made by `qs cut` can be re-cut.")

    try:
        start, end = float(body["start"]), float(body["end"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(400, "start and end (seconds) are required") from None
    if end <= start:
        raise HTTPException(400, "end must be after start")

    return qs_cut({"episode_id": episode_id, "start": start, "end": end,
                   "person": attribution.get("person"),
                   "model": body.get("model") or None,
                   "replace_item": item_id})


@app.post("/api/qs/cut")
def qs_cut(body: dict = Body(...)):
    """Word-accurate cut. Same job/polling contract as /api/qs/pull."""
    if _remote():
        return _start_remote_job("cut", body)

    from quotesource.cut import cut_quote

    job_id, job = _new_job()

    def _run_job():
        try:
            replacing = body.get("replace_item")
            res = cut_quote(
                body["episode_id"],
                float(body["start"]), float(body["end"]),
                palette_name=body.get("palette") or None,
                person=body.get("person") or None,
                model_size=body.get("model") or None,
                progress_cb=lambda stage: job.__setitem__("stage", stage),
                # A correction produces the clip but must not register a
                # second item for it - the whole point is that the existing
                # one survives.
                stage=False if replacing else bool(body.get("stage", True)),
                outbox=body.get("outbox") or None,
            )
            if replacing:
                import asyncio as _asyncio

                from .library import replace_item_media

                job["stage"] = "replacing"
                before_beats = _beats_bound_to(_root(), replacing)
                job["replaced"] = _asyncio.run(replace_item_media(
                    _root(), replacing, res["filename"],
                    manifest=Path(res["filename"]).with_suffix(
                        ".words.json").name,
                    attribution=res.get("attribution"),
                    url=(res.get("attribution") or {}).get("source_url_ts"),
                    title=res.get("title")))
                job["replaced"]["beats_drifted"] = _narration_drift(
                    before_beats, _beats_bound_to(_root(), replacing))
                job["item"] = _find(load_library(_root())["items"], replacing)
            else:
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
    mode = body.get("mode", "audio")
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
    """One entry per export. A sheet series is a folder and counts as one —
    forty rows for a single render would bury the rest of the history."""
    root = _root()
    entries = []
    for p in (root / "exports").iterdir():
        if p.is_file():
            entries.append({"kind": "file", "filename": p.name,
                            "size_bytes": p.stat().st_size,
                            "mtime": p.stat().st_mtime})
        elif p.is_dir():
            sheets = sorted(p.glob("*.jpg"))
            if not sheets:
                continue
            entries.append({
                "kind": "series",
                "filename": p.name,
                "sheets": len(sheets),
                "size_bytes": sum(f.stat().st_size for f in p.iterdir() if f.is_file()),
                "mtime": p.stat().st_mtime,
                "index": f"{p.name}/index.json" if (p / "index.json").exists() else None,
                "first": f"{p.name}/{sheets[0].name}",
                "path": str(p),
            })
    entries.sort(key=lambda e: e["mtime"], reverse=True)
    return entries


@app.get("/api/exports/{filename:path}")
def serve_export(filename: str):
    root = _root()
    exports = (root / "exports").resolve()
    # filename is now a path with a folder in it, so ../ has somewhere to go.
    path = (exports / filename).resolve()
    if not path.is_relative_to(exports) or not path.is_file():
        raise HTTPException(404)
    return FileResponse(str(path))
