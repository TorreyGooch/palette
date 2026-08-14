"""Drop a finished clip somewhere the next tool can pick it up.

A cut is born on the machine with the corpus and the GPU, which is also the
machine running the video model — so a clip destined for generation would
otherwise travel to the desktop library and back to get four directories
away. The outbox is that shortcut.

Deliberately not the generator's own input folder: that fills with whatever
a pipeline is fed and becomes impossible to curate. This is a staging area
you copy *from*.

Off unless asked. `--outbox <path>` per run, or QS_OUTBOX to set it once.
"""
import os
import shutil
from pathlib import Path


def outbox_dir(explicit: str | None = None) -> Path | None:
    """The configured outbox, or None when the feature is off."""
    raw = explicit if explicit is not None else os.environ.get("QS_OUTBOX")
    raw = (raw or "").strip()
    if not raw:
        return None
    return Path(os.path.expanduser(raw))


def deliver(paths, explicit: str | None = None) -> list[str]:
    """Copy the given files into the outbox. Returns what landed there.

    Copies rather than moves: the caller still has to hand the clip to
    whoever asked for it, and a move would pull the file out from under
    them. Missing inputs are skipped — a plain pull has no manifest, and
    that is not an error.
    """
    target = outbox_dir(explicit)
    if target is None:
        return []

    target.mkdir(parents=True, exist_ok=True)
    delivered = []
    for path in paths:
        src = Path(path)
        if not src.exists():
            continue
        dest = target / src.name
        # Write beside and rename, so a reader watching the folder never
        # sees a half-written clip.
        tmp = dest.with_suffix(dest.suffix + ".part")
        shutil.copyfile(src, tmp)
        tmp.replace(dest)
        delivered.append(str(dest))
    return delivered
