"""Candidates that were looked at and deliberately not added.

Finding guests means searching, reading results and throwing most of them
away. That judgement is the expensive part and nothing recorded it, so the
next person re-evaluates the same URLs — or, worse, adds one that was already
rejected for a reason they never see.

Two real cases from one session, both searching for James A. Shapiro: several
results were Denis Noble, and several were intelligent-design repackagings of
Shapiro's work rather than his own talks. Neither is detectable from a title.

**Keyed by video id at the data root, not per source.** A rejection is a fact
about a video, not about the source it was being considered for: the same
Denis Noble result could plausibly be offered again later for a Noble source,
and the note about who it actually is remains true.

**It warns; it never refuses.** A rejection is a judgement and judgements can
be wrong — the same video might legitimately be wanted for a different person.
That matches the duplicate guard, which also reports and lets you proceed.
A file nobody reads is dead weight, so the value is entirely in `guest add`
checking it: the fact arrives where the consumer already looks.
"""
import json
from datetime import datetime

from .paths import data_root

REJECTIONS_FILE = "rejected.json"


def rejections_path():
    return data_root() / REJECTIONS_FILE


def load_rejections() -> dict:
    """Every rejection, or {} if there are none or the file is unreadable.

    Fails open on purpose: this is an advisory note, not the corpus. A corrupt
    file should cost a lost warning, never a refused ingest.
    """
    path = rejections_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def rejection_for(video_id: str):
    """The recorded judgement about this video, or None."""
    return load_rejections().get(video_id) or None


def reject(video_id: str, reason: str, person: str = "") -> dict:
    """Record that this video was considered and declined.

    Re-rejecting overwrites, because the newest reading of a candidate is the
    one worth keeping and a history of one person's changing mind about a
    YouTube video is not worth a file format.
    """
    if not video_id:
        raise ValueError("a video id is required")
    if not (reason or "").strip():
        # A rejection with no reason is the thing this file exists to prevent:
        # it tells the next person only that somebody said no.
        raise ValueError("a reason is required — the reason is the whole point")

    entry = {"at": datetime.now().strftime("%Y-%m-%d"),
             "reason": reason.strip()}
    if person:
        entry["person"] = person

    rejections = load_rejections()
    rejections[video_id] = entry
    root = data_root()
    root.mkdir(parents=True, exist_ok=True)
    rejections_path().write_text(
        json.dumps(rejections, indent=2, sort_keys=True), encoding="utf-8")
    return {"video_id": video_id, **entry}


def unreject(video_id: str) -> bool:
    """Take a video back off the list. True if it was on it."""
    rejections = load_rejections()
    if video_id not in rejections:
        return False
    rejections.pop(video_id)
    rejections_path().write_text(
        json.dumps(rejections, indent=2, sort_keys=True), encoding="utf-8")
    return True


def list_rejections() -> list[dict]:
    """Newest first, so the recent judgements are the visible ones."""
    return sorted(({"video_id": vid, **entry}
                   for vid, entry in load_rejections().items()),
                  key=lambda r: (r.get("at") or "", r["video_id"]), reverse=True)
