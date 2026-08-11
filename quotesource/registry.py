"""sources.yaml registry. Human-editable; these helpers keep the shape valid."""
import re
from .paths import sources_path, ensure_root

VALID_TYPES = ("youtube_channel", "youtube_playlist", "rss")

TEMPLATE = """\
# quotesource registry. Edit freely; `qs sources` commands keep this shape.
# Each source:
#   - id: short-slug            # unique, filesystem-safe
#     name: Human Name
#     type: youtube_channel | youtube_playlist | rss
#     url: https://...
#     people: [Host Name]       # default speaker metadata for the source
#     notes: optional free text
sources: []
"""


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
               people: list[str] | None = None, notes: str = "") -> dict:
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
    if notes:
        entry["notes"] = notes
    sources.append(entry)
    _save_yaml(sources)
    return entry


def remove_source(source_id: str) -> bool:
    sources = _load_yaml()
    kept = [s for s in sources if s.get("id") != source_id]
    if len(kept) == len(sources):
        return False
    _save_yaml(kept)
    return True
