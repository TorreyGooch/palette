"""Talk to a quotesource corpus living on another machine.

The corpus (transcripts + index + vectors, several GB and growing) belongs
next to the GPU that builds it. The media library belongs next to the person
scrubbing video. Those pull in opposite directions, so palette runs in two
places: a headless instance on the server owning the corpus, and the one you
actually look at, owning the media.

Set QS_REMOTE to the server's palette URL and the /api/qs/* endpoints stop
importing quotesource locally and forward there instead. What crosses the
network is a text query and, for cut/pull, one small clip — never the corpus
and never the media library.

Deliberately stdlib-only and blocking: the qs endpoints are already sync on
purpose (FastAPI runs them in a threadpool), so there is no async client to
justify and no dependency to add.
"""
import json
import os
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

# Long enough for a cold semantic search (model load + brute-force cosine),
# short enough to fail rather than hang the UI. Job polling uses its own.
TIMEOUT = float(os.environ.get("QS_REMOTE_TIMEOUT", "120"))


def remote_base() -> Optional[str]:
    """The remote palette's base URL, or None when running self-contained."""
    base = (os.environ.get("QS_REMOTE") or "").strip().rstrip("/")
    if not base:
        return None
    if not base.startswith(("http://", "https://")):
        base = "http://" + base
    return base


class RemoteError(RuntimeError):
    """A remote call failed. Carries the upstream status when there was one."""

    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


def _request(method: str, path: str, params: dict = None,
             body: dict = None, timeout: float = None):
    base = remote_base()
    if not base:
        raise RemoteError("QS_REMOTE is not set", 500)

    url = f"{base}{path}"
    if params:
        clean = {k: v for k, v in params.items() if v is not None}
        if clean:
            url += "?" + urllib.parse.urlencode(clean)

    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout or TIMEOUT) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        # Parse before truncating, not after. Cutting the body first breaks
        # the JSON, so the parse fails, and what reaches the caller is the raw
        # envelope with the message chopped mid-word - losing exactly the tail
        # of a long explanation, which is where the advice lives. 400 was also
        # simply too short: these are sentences meant to be read.
        body = e.read().decode("utf-8", "replace")
        detail = body
        try:
            parsed = json.loads(body)
            if isinstance(parsed, dict):
                detail = parsed.get("detail", body)
        except Exception:
            pass
        detail = str(detail)[:2000]
        # Preserve the upstream status: a 404 for a missing episode should not
        # reach the browser as a generic gateway error.
        raise RemoteError(f"remote {e.code}: {detail}", e.code) from None
    except urllib.error.URLError as e:
        raise RemoteError(f"cannot reach quotesource at {base}: {e.reason}", 503) from None

    return json.loads(raw) if raw else None


def get(path: str, params: dict = None, timeout: float = None):
    return _request("GET", path, params=params, timeout=timeout)


def post(path: str, body: dict, timeout: float = None):
    return _request("POST", path, body=body, timeout=timeout)


# Capabilities change only when the far side restarts onto new code, so a
# short cache keeps this off the path of every pull without going stale for
# long. Deliberately fail-soft: an unreachable server reports no
# capabilities, and callers treat that as "assume the old behaviour".
_CAP_TTL_S = float(os.environ.get("QS_REMOTE_CAP_TTL_S", "60"))
_caps: set = set()
_caps_base = None
_caps_at = 0.0


def remote_capabilities(refresh: bool = False) -> set:
    """What the remote says it can do. Empty when unknown or unreachable."""
    global _caps, _caps_base, _caps_at

    base = remote_base()
    if not base:
        return set()

    now = time.monotonic()
    if (not refresh and _caps_base == base
            and now - _caps_at < _CAP_TTL_S):
        return _caps

    try:
        status = get("/api/qs/status", timeout=30) or {}
        palette = status.get("palette") or {}
        _caps = set(palette.get("capabilities") or [])
    except RemoteError:
        _caps = set()
    _caps_base, _caps_at = base, now
    return _caps


def remote_supports(name: str) -> bool:
    return name in remote_capabilities()


def fetch_file(filename: str, dest: Path, timeout: float = 600.0) -> bool:
    """Download one file from the remote library's media folder.

    Returns False when the remote has no such file, which is not always an
    error — `qs cut` writes a .words.json sidecar that a plain pull will not.
    """
    base = remote_base()
    url = f"{base}/api/media/{urllib.parse.quote(filename)}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_suffix(dest.suffix + ".part")
            with open(tmp, "wb") as fh:
                shutil.copyfileobj(resp, fh)
            tmp.replace(dest)  # atomic: never leave a half file in the library
        return True
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False
        raise RemoteError(f"downloading {filename}: remote {e.code}", e.code) from None
    except urllib.error.URLError as e:
        raise RemoteError(f"downloading {filename}: {e.reason}", 503) from None


def poll_job(job_id: str, kind: str = "pull", interval: float = 1.0,
             max_wait: float = 1800.0) -> dict:
    """Block until a remote pull/cut job finishes. Returns the finished job."""
    import time

    deadline = time.time() + max_wait
    while time.time() < deadline:
        job = get(f"/api/qs/{kind}/{job_id}", timeout=30)
        if job.get("done"):
            return job
        time.sleep(interval)
    raise RemoteError(f"remote {kind} job {job_id} did not finish in {max_wait:.0f}s", 504)


def manifest_name(filename: str, remote_item: dict = None) -> str:
    """Sidecar name for a clip: qs cut *replaces* the extension, not appends."""
    stated = (remote_item or {}).get("manifest")
    if stated:
        return Path(stated).name
    return Path(filename).with_suffix(".words.json").name


async def replace_remote_item(root: Path, item_id: str,
                              remote_item: dict) -> dict:
    """Swap a remote re-cut into an item that already exists here.

    Same fetch as adopt; a different destination. Attribution travels
    verbatim for the same reason it does there — it is what makes the clip
    citable, and rebuilding it locally could let it drift from what the
    server recorded. Tags and palettes are not touched at all: they are this
    library's curation, and the remote has no opinion about them.
    """
    from .library import replace_item_media

    filename = remote_item.get("filename")
    if not filename:
        raise RemoteError("remote job returned no filename", 502)

    dest = root / "media" / filename
    if not fetch_file(filename, dest):
        raise RemoteError(f"remote produced {filename} but will not serve it", 502)

    sidecar = manifest_name(filename, remote_item)
    if not fetch_file(sidecar, root / "media" / sidecar):
        raise RemoteError(f"re-cut produced no manifest ({sidecar}) — the clip "
                          f"cannot drive word-level beats without it", 502)

    return await replace_item_media(
        root, item_id, filename,
        manifest=sidecar,
        attribution=remote_item.get("attribution"),
        url=remote_item.get("url"),
        title=remote_item.get("title"))


async def adopt_remote_item(root: Path, remote_item: dict,
                            palette_name: str = None, person: str = None,
                            kind: str = "cut") -> dict:
    """Copy a clip the remote just produced into the local library.

    Attribution travels verbatim — it is what makes the clip citable, and
    rebuilding it here could let it drift from what the server recorded.
    Tags and palette membership do not travel: palettes are stored as ids,
    and the remote's ids refer to *its* palettes, so copying them would point
    at nothing here. They are reapplied by name instead, matching what
    cut_quote/pull do locally.
    """
    from .library import (library_lock, load_library, new_palette,
                          register_media_file, save_library)

    filename = remote_item.get("filename")
    if not filename:
        raise RemoteError("remote job returned no filename", 502)

    # Always fetch, even when the name already exists locally. Clip filenames
    # truncate their bounds to whole seconds, so two cuts a fraction of a
    # second apart collide — and keeping the old audio beside the new manifest
    # would leave word timings describing a clip that is not there.
    dest = root / "media" / filename
    if not fetch_file(filename, dest):
        raise RemoteError(f"remote produced {filename} but will not serve it", 502)

    # Per-word timings are what let visual beats land on specific words, so
    # the manifest is part of the artifact rather than an optional extra.
    # A plain pull has none; a cut that lost one is worth knowing about.
    sidecar = manifest_name(filename, remote_item)
    got_manifest = fetch_file(sidecar, root / "media" / sidecar)
    if kind == "cut" and not got_manifest:
        raise RemoteError(f"cut produced no manifest ({sidecar}) — the clip "
                          f"cannot drive word-level beats without it", 502)

    item = await register_media_file(
        root, filename, remote_item.get("title") or filename,
        remote_item.get("url"))

    with library_lock(root):
        lib = load_library(root)
        it = next((i for i in lib["items"] if i["id"] == item["id"]), None)
        if it is None:
            return item

        for key in ("attribution", "url", "title"):
            if remote_item.get(key):
                it[key] = remote_item[key]
        if got_manifest:
            it["manifest"] = sidecar

        tags = ["quotesource"] + (["word-cut"] if kind == "cut" else [])
        if person:
            tags.append(person)
        it["tags"] = sorted(set(it.get("tags", []) + tags))

        if palette_name:
            pal = next((p for p in lib["palettes"]
                        if p["name"].lower() == palette_name.lower()), None)
            if not pal:
                pal = new_palette(palette_name)
                lib["palettes"].append(pal)
            if pal["id"] not in it["palettes"]:
                it["palettes"].append(pal["id"])

        save_library(root, lib)
    return it
