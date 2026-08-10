import json
from pathlib import Path

TOOL_DIR = Path(__file__).parent.parent
CONFIG_PATH = TOOL_DIR / "config.json"


def get_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def save_config(cfg: dict):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def get_library_path() -> Path | None:
    cfg = get_config()
    p = cfg.get("library_path")
    if p and Path(p).exists():
        return Path(p)
    return None


def set_library_path(path: str):
    cfg = get_config()
    cfg["library_path"] = path
    save_config(cfg)
