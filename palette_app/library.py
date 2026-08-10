import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

SUBFOLDERS = ["media", "thumbnails", "exports"]

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff"}
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}


def create_library(root: Path) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    for folder in SUBFOLDERS:
        (root / folder).mkdir(parents=True, exist_ok=True)
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
    return "other"


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
