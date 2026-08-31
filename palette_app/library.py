import json
import os
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

try:                                    # POSIX
    import fcntl
except ImportError:                     # pragma: no cover - Windows
    fcntl = None
try:                                    # Windows
    import msvcrt
except ImportError:                     # pragma: no cover - POSIX
    msvcrt = None

SUBFOLDERS = ["media", "thumbnails", "exports"]

LOCK_NAME = ".library.lock"
LOCK_TIMEOUT_S = 20.0
REPLACE_TIMEOUT_S = 5.0

# One lock per library root, made once and shared. Keyed by a normalised path
# so "C:/x" and "c:\X\\" are one library on Windows and two names for it are
# not two locks.
_locks: dict = {}
_locks_guard = threading.Lock()
# Nesting depth per thread, so an outer library_lock() that calls a helper
# taking one again does not try to lock the file twice from the same process.
_held = threading.local()


def _lock_key(root) -> str:
    return os.path.normcase(os.path.abspath(str(root)))


def _lock_for(key: str) -> threading.RLock:
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = _locks[key] = threading.RLock()
        return lock


def _take_file_lock(root: Path):
    """An advisory lock on a file beside the database, or None if impossible.

    Covers the second app instance and the CLI. Returns None rather than
    failing when the lock file cannot be made - a read-only or exotic mount
    should not stop the library being read, and the in-process lock still
    holds where it matters most.
    """
    if fcntl is None and msvcrt is None:      # pragma: no cover
        return None
    try:
        fd = os.open(str(Path(root) / LOCK_NAME), os.O_RDWR | os.O_CREAT, 0o644)
    except OSError:
        return None
    deadline = time.monotonic() + LOCK_TIMEOUT_S
    while True:
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_EX)
            else:                             # pragma: no cover - Windows
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return fd
        except OSError:
            if time.monotonic() >= deadline:
                os.close(fd)
                raise RuntimeError(
                    "another process is writing this library; gave up after "
                    f"{LOCK_TIMEOUT_S:.0f}s")
            time.sleep(0.05)


def _drop_file_lock(fd):
    if fd is None:
        return
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_UN)
        else:                                 # pragma: no cover - Windows
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    except OSError:
        pass
    finally:
        os.close(fd)


@contextmanager
def _read_guard(root):
    """Serialise a read against writers *in this process*, and nothing more.

    Deliberately does not take the file lock, because taking it means
    creating the lock file, and a read path must not write into someone's
    library. The in-process lock is what matters anyway: one app process
    serves every session, so its own writer is the one a reader will actually
    collide with. A writer in another process is covered by the retries on
    both sides instead.
    """
    key = _lock_key(root)
    lock = _lock_for(key)
    lock.acquire()
    try:
        yield
    finally:
        lock.release()


@contextmanager
def library_lock(root):
    """Hold this around every read-modify-write of the library.

    `save_library` writes atomically, so a reader never sees half a file and
    a crash cannot truncate one. That is not the same as being safe: the
    dangerous window is between *loading* and *saving*, where two writers
    each read the same database, each change their own copy, and the second
    save silently discards the first change. Three sessions now drive this
    app, so that window is real, and a lost tag looks exactly like a tag that
    was never applied.

    Reentrant within a thread, so a helper that locks can be called from a
    caller that already has.
    """
    key = _lock_key(root)
    depths = getattr(_held, "depths", None)
    if depths is None:
        depths = _held.depths = {}
    lock = _lock_for(key)
    lock.acquire()
    depths[key] = depths.get(key, 0) + 1
    fd = None
    try:
        if depths[key] == 1:
            fd = _take_file_lock(Path(root))
        yield
    finally:
        _drop_file_lock(fd)
        depths[key] -= 1
        if not depths[key]:
            depths.pop(key, None)
        lock.release()


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
    # Checked and created under the lock: two processes starting at once
    # would otherwise both find no database and both create one, and the
    # second would erase whatever the first had already written into it.
    with library_lock(root):
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
    with library_lock(root):
        save_library(root, lib)
    return lib


def load_library(root: Path) -> dict:
    """Read the database, waiting out a writer rather than failing on one.

    Atomic replacement is not enough on Windows, which is where this app
    runs. A rename there fails while another handle has the destination open,
    and an open fails while a rename is in flight - so the collision cuts
    both ways, and either side surfaces as an intermittent "Permission
    denied" rather than anything a reader could interpret. In-process that is
    settled by the lock; the retry is for a writer in another process.
    """
    path = Path(root) / "library.json"
    with _read_guard(root):
        deadline = time.monotonic() + REPLACE_TIMEOUT_S
        while True:
            try:
                with open(path, encoding="utf-8") as f:
                    return json.load(f)
            except PermissionError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.01)


def save_library(root: Path, lib: dict):
    """Replace the database, atomically.

    Written to a temporary file in the same directory and moved into place,
    because a plain overwrite truncates first: a crash, a full disk or a
    second writer landing mid-dump leaves a half-written library.json, and
    that file *is* the media database - every item, tag and palette. os.replace
    is atomic on both POSIX and Windows, so a reader sees either the old
    database or the new one and never a fragment of either.

    This does not make a read-modify-write safe on its own. Hold
    `library_lock` around load-then-save for that.
    """
    root = Path(root)
    fd, tmp = tempfile.mkstemp(dir=str(root), prefix=".library-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(lib, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        # os.replace is atomic on both platforms, but on Windows it also
        # *fails* while another handle has the destination open: Python opens
        # files for reading without FILE_SHARE_DELETE, so a single concurrent
        # reader is enough to deny the rename. Readers are deliberately not
        # made to take the lock - they are the common case and a complete file
        # is all they need - so the collision is handled here instead. The
        # replace is the only step that can collide and it is over in
        # microseconds, which turns an intermittent 500 into a brief wait.
        deadline = time.monotonic() + REPLACE_TIMEOUT_S
        while True:
            try:
                os.replace(tmp, root / "library.json")
                break
            except PermissionError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.01)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


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

    with library_lock(root):
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
