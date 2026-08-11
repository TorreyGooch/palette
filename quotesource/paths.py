"""Storage root resolution for quotesource.

Precedence: QUOTESOURCE_DATA env var > `quotesource_data` in palette's
config.json > `<library_root>/quotesource` derived from palette's library.
"""
import json
import os
from pathlib import Path

REPO_DIR = Path(__file__).parent.parent


class DataRootError(RuntimeError):
    pass


def data_root() -> Path:
    env = os.environ.get("QUOTESOURCE_DATA")
    if env:
        return Path(env)
    cfg_path = REPO_DIR / "config.json"
    if cfg_path.exists():
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        if cfg.get("quotesource_data"):
            return Path(cfg["quotesource_data"])
        if cfg.get("library_path"):
            return Path(cfg["library_path"]) / "quotesource"
    raise DataRootError(
        "No data root configured. Set QUOTESOURCE_DATA, add 'quotesource_data' "
        "to config.json, or configure the palette library first (run the app once)."
    )


def ensure_root() -> Path:
    root = data_root()
    (root / "episodes").mkdir(parents=True, exist_ok=True)
    return root


def sources_path() -> Path:
    return data_root() / "sources.yaml"


def episode_dir(source_id: str, episode_id: str) -> Path:
    return data_root() / "episodes" / source_id / episode_id
