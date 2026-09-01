"""sources.yaml registry. Human-editable; these helpers keep the shape valid."""
import re
from .paths import sources_path, ensure_root

VALID_TYPES = ("youtube_channel", "youtube_playlist", "rss", "episodes")

TEMPLATE = """\
# quotesource registry. Edit freely; `qs sources` commands keep this shape.
# Each source:
#   - id: short-slug            # unique, filesystem-safe
#     name: Human Name
#     type: youtube_channel | youtube_playlist | rss | episodes
#     url: https://...          # not needed for an `episodes` source
#     people: [Host Name]       # default speaker metadata for the source
#     min_duration: 1800        # optional; skip anything shorter (seconds)
#     notes: optional free text
#
# An `episodes` source is a bag of individually added videos rather than a
# feed - see `qs guest add`. It has nothing to enumerate, so `qs ingest` on one
# only retries episodes already on disk whose caption fetch failed.
sources: []
"""

_DUR_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([hms]?)", re.I)


def parse_duration(value) -> int:
    """Seconds from '1800', '30m', '1h', '1h30m'. Bare numbers are seconds."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().lower()
    if not text:
        return None
    matches = _DUR_RE.findall(text)
    if not matches or "".join(n + u for n, u in matches) != text.replace(" ", ""):
        raise ValueError(f"bad duration: {value!r} (try 1800, 30m, 1h30m)")
    total = 0.0
    for number, unit in matches:
        total += float(number) * {"h": 3600, "m": 60, "s": 1, "": 1}[unit]
    return int(total)


def _load_yaml():
    import yaml

    p = sources_path()
    if not p.exists():
        ensure_root()
        p.write_text(TEMPLATE, encoding="utf-8")
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return data.get("sources") or []


def _save_yaml(sources: list):
    import yaml

    p = sources_path()
    p.write_text(
        yaml.safe_dump({"sources": sources}, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


def list_sources() -> list:
    return _load_yaml()


def get_source(source_id: str) -> dict | None:
    return next((s for s in _load_yaml() if s.get("id") == source_id), None)


def add_source(source_id: str, name: str, type_: str, url: str,
               people: list[str] | None = None, notes: str = "",
               min_duration=None) -> dict:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", source_id):
        raise ValueError(f"id must be a lowercase slug, got: {source_id!r}")
    if type_ not in VALID_TYPES:
        raise ValueError(f"type must be one of {VALID_TYPES}, got: {type_!r}")
    sources = _load_yaml()
    if any(s.get("id") == source_id for s in sources):
        raise ValueError(f"source '{source_id}' already exists")
    entry = {
        "id": source_id,
        "name": name,
        "type": type_,
        "url": url,
        "people": people or [],
    }
    seconds = parse_duration(min_duration)
    if seconds:
        entry["min_duration"] = seconds
    if notes:
        entry["notes"] = notes
    sources.append(entry)
    _save_yaml(sources)
    return entry


def record_min_duration_evidence(source_id: str, evidence: dict) -> bool:
    """Store what the channel looked like when a min_duration was applied.

    `min_duration: 1800` is a number with no argument attached, and the next
    person cannot tell a deliberate threshold from an oversight. Prose would
    not fix that - "dropped the trailer" still cannot be checked. What cannot
    be reconstructed later is the *evidence*: 51 items enumerated, one below
    the line, and it was a 171-second trailer.

    This is not a derive-don't-store violation, and the distinction matters.
    Re-enumerating gives today's channel, not the one the decision was made
    against; the observation is historical, so recomputing it answers a
    different question. If someone re-enumerates in a year and forty items now
    fall under the threshold, the number has gone wrong for what the channel
    became - and only the snapshot makes that visible.

    Written at ingest rather than at `sources add`, because add writes YAML
    and never touches the network: gathering this there would mean spending
    the request budget to describe a source nobody has fetched yet. Ingest
    already enumerates and already applies the filter.
    """
    sources = _load_yaml()
    for entry in sources:
        if entry.get("id") == source_id:
            entry["min_duration_evidence"] = evidence
            _save_yaml(sources)
            return True
    return False


def remove_source(source_id: str) -> bool:
    sources = _load_yaml()
    kept = [s for s in sources if s.get("id") != source_id]
    if len(kept) == len(sources):
        return False
    _save_yaml(kept)
    return True
