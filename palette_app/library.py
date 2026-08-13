import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

SUBFOLDERS = ["media", "thumbnails", "exports"]

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff"}
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}
AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".opus", ".ogg", ".wav", ".flac"}


def ensure_library(root: Path) -> Path:
    """Make a library root usable: subfolders and a database. Idempotent.

    A root can come into existence without create_library() ever running —
    restored from a backup, or assembled by hand when moving to a new
    machine. Writers then aim at folders that are not there (ffmpeg reports
    that as a bare "exit status 254", saying nothing about a missing
    directory) or read a library.json that does not exist. Preserves an
    existing database; only the missing pieces are created.
    """
    root.mkdir(parents=True, exist_ok=True)
    for folder in SUBFOLDERS:
        (root / folder).mkdir(parents=True, exist_ok=True)
    if not (root / "library.json").exists():
        save_library(root, {"created": datetime.now().isoformat(),
                            "items": [], "palettes": []})
    return root


def create_library(root: Path) -> dict:
    ensure_library(root)
    lib = {
        "created": datetime.now().isoformat(),
        "items": [],
        "palettes": [],
    }
    save_library(root, lib)
    return lib


def load_library(root: Path) -> dict:
    with open(root / "library.json", encoding="utf-8") as f:
        return json.load(f)


def save_library(root: Path, lib: dict):
    with open(root / "library.json", "w", encoding="utf-8") as f:
        json.dump(lib, f, indent=2)


def is_library(root: Path) -> bool:
    return (root / "library.json").exists()


def media_type(filename: str) -> str:
    ext = Path(filename).suffix.lower()
    if ext in IMAGE_EXTS:
        return "image"
    if ext in VIDEO_EXTS:
        return "video"
    if ext in AUDIO_EXTS:
        return "audio"
    return "other"


async def register_media_file(root: Path, filename: str, title: str,
                              url: Optional[str] = None) -> dict:
    """Probe, thumbnail, and add a media file (already in root/media) to the
    library. Shared by the web app and the quotesource CLI."""
    from .api.media import probe, video_thumbnail, image_thumbnail, audio_thumbnail

    path = root / "media" / filename
    duration = fps = None
    mtype = media_type(filename)
    if mtype in ("video", "audio"):
        info = await probe(path)
        duration, fps = info["duration"], info["fps"]
    item = new_item(filename, title, url, duration, fps)

    thumb = root / "thumbnails" / f"{item['id']}.jpg"
    if item["type"] == "video":
        await video_thumbnail(path, thumb, (duration or 1) / 2)
    elif item["type"] == "image":
        image_thumbnail(path, thumb)
    elif item["type"] == "audio":
        await audio_thumbnail(path, thumb)

    lib = load_library(root)
    lib["items"].append(item)
    save_library(root, lib)
    return item


def new_item(filename: str, title: str, url: Optional[str] = None,
             duration: Optional[float] = None, fps: Optional[float] = None) -> dict:
    return {
        "id": str(uuid.uuid4()),
        "filename": filename,
        "type": media_type(filename),
        "title": title,
        "url": url,
        "tags": [],
        "palettes": [],
        "duration": duration,
        "fps": fps,
        "added": datetime.now().isoformat(),
    }


def new_palette(name: str) -> dict:
    return {
        "id": str(uuid.uuid4()),
        "name": name,
        "created": datetime.now().isoformat(),
    }
