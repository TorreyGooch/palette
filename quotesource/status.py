"""Corpus-wide status: episode counts by transcript state, disk usage."""
from pathlib import Path

from .paths import ensure_root
from .ingest import episode_status
from .registry import list_sources


def _dir_size(path: Path) -> int:
    total = 0
    if path.exists():
        for f in path.rglob("*"):
            if f.is_file():
                total += f.stat().st_size
    return total


def _cooldown_block() -> dict:
    """Whether we are currently standing off YouTube, and until when.

    Discovering this used to mean grepping a log in /tmp. It is the one piece
    of state that decides whether any ingest can run at all.
    """
    from .ingest import cooldown_state

    state = cooldown_state()
    if not state:
        return {"active": False}
    return {"active": True, "until": state.get("until"),
            "since": state.get("at"), "remaining_s": state.get("remaining_s"),
            "source": state.get("source"), "reason": state.get("reason")}


def _budget_block() -> dict:
    """How much of today's request allowance is left."""
    from .ingest import budget_state

    return budget_state()


def corpus_status() -> dict:
    root = ensure_root()
    episodes_root = root / "episodes"
    registered = {s["id"]: s for s in list_sources()}

    sources = []
    totals = {"episodes": 0, "captions": 0, "whisper": 0,
              "needs_transcription": 0, "captions_pending": 0, "unknown": 0}

    seen_dirs = set()
    for src_dir in sorted(episodes_root.iterdir()) if episodes_root.exists() else []:
        if not src_dir.is_dir():
            continue
        seen_dirs.add(src_dir.name)
        counts = {"captions": 0, "whisper": 0, "needs_transcription": 0,
                  "captions_pending": 0, "unknown": 0}
        for ep_dir in src_dir.iterdir():
            if not ep_dir.is_dir():
                continue
            st = episode_status(ep_dir)
            counts[st] = counts.get(st, 0) + 1
        n = sum(counts.values())
        totals["episodes"] += n
        for k in counts:
            totals[k] = totals.get(k, 0) + counts[k]
        sources.append({
            "source": src_dir.name,
            "registered": src_dir.name in registered,
            "episodes": n,
            **counts,
        })

    # registered sources with no episodes yet
    for sid in registered:
        if sid not in seen_dirs:
            sources.append({
                "source": sid, "registered": True, "episodes": 0,
                "captions": 0, "whisper": 0, "needs_transcription": 0,
                "captions_pending": 0, "unknown": 0,
            })

    from .indexer import index_stats
    try:
        from .embedder import embed_stats
        embed = embed_stats()
    except Exception:
        embed = None

    return {
        "data_root": str(root),
        "sources": sources,
        "totals": totals,
        "index": index_stats(),
        "embeddings": embed,
        "cooldown": _cooldown_block(),
        "budget": _budget_block(),
        "disk": {
            "episodes_mb": round(_dir_size(root / "episodes") / 1048576, 1),
            "total_mb": round(_dir_size(root) / 1048576, 1),
        },
    }
