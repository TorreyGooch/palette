"""Phase 2 — Whisper backfill.

qs transcribe <episode-id>
qs transcribe --batch [--source <id>] [--limit N]

- Downloads speech-quality audio into the episode dir (audio.m4a) and KEEPS
  it: the audio store is also what makes audio-mode pulls fully local.
- Transcribes with faster-whisper (GPU if available). Backend knobs via env:
    QS_WHISPER_MODEL    (default: large-v3 on cuda, base on cpu)
    QS_WHISPER_DEVICE   (default: auto)
    QS_WHISPER_COMPUTE  (default: float16 on cuda, int8 on cpu)
- Replaces the caption transcript; the previous transcript.json is preserved
  as transcript.<source>.json so provenance is never lost.
- Batch queue priority: needs_transcription > youtube_auto > youtube_manual.
  Resumable (whisper-done episodes are skipped), throttled downloads, hard
  stop when free disk falls below QS_DISK_FLOOR_GB (default 20).
"""
import json
import shutil
import time
from datetime import datetime
from pathlib import Path

from .ingest import load_metadata, episode_status
from .paths import ensure_root

DISK_FLOOR_GB_DEFAULT = 20
SLEEP_BETWEEN_DOWNLOADS = 2.0


def _disk_floor_gb() -> float:
    import os

    return float(os.environ.get("QS_DISK_FLOOR_GB", DISK_FLOOR_GB_DEFAULT))


def _free_gb(path: Path) -> float:
    return shutil.disk_usage(path).free / 1024 ** 3


def _whisper_config():
    import os

    device = os.environ.get("QS_WHISPER_DEVICE", "auto")
    cuda = device == "cuda"
    if device == "auto":
        try:
            import ctranslate2

            cuda = ctranslate2.get_cuda_device_count() > 0
        except Exception:
            cuda = False
        device = "cuda" if cuda else "cpu"
    model = os.environ.get("QS_WHISPER_MODEL", "large-v3" if cuda else "base")
    compute = os.environ.get("QS_WHISPER_COMPUTE",
                             "float16" if cuda else "int8")
    return model, device, compute


_model_cache: dict = {}


def _get_model():
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise RuntimeError(
            "faster-whisper is not installed. On the transcription machine: "
            "pip install faster-whisper (needs a Python version with "
            "ctranslate2 wheels; use a 3.12 venv if 3.14 lacks them)."
        )
    model, device, compute = _whisper_config()
    key = (model, device, compute)
    if key not in _model_cache:
        _model_cache[key] = WhisperModel(model, device=device, compute_type=compute)
    return _model_cache[key], key


def download_audio(ep_dir: Path, quiet: bool = True) -> Path | None:
    """Fetch speech-quality audio into the episode dir (kept permanently)."""
    audio_path = ep_dir / "audio.m4a"
    if audio_path.exists():
        return audio_path
    meta = load_metadata(ep_dir)
    if not meta:
        return None
    url = meta.get("audio_url") or meta.get("url")
    if not url:
        return None

    import yt_dlp

    opts = {
        "quiet": True, "no_warnings": True, "noprogress": True,
        "format": "bestaudio[ext=m4a]/bestaudio/best",
        "outtmpl": str(ep_dir / "audio.%(ext)s"),
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "m4a",
            "preferredquality": "64",
        }],
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])
    if not audio_path.exists():
        cand = list(ep_dir.glob("audio.*"))
        cand = [c for c in cand if c.suffix != ".json"]
        if cand:
            cand[0].rename(audio_path)
    return audio_path if audio_path.exists() else None


def transcribe_episode(ep_dir: Path, quiet: bool = False) -> dict:
    meta = load_metadata(ep_dir)
    if not meta:
        raise FileNotFoundError(f"no metadata in {ep_dir}")
    episode_id = meta["episode_id"]

    audio = download_audio(ep_dir, quiet=quiet)
    if not audio:
        raise RuntimeError(f"{episode_id}: audio download failed")

    model, (mname, device, compute) = _get_model()
    if not quiet:
        print(f"  {episode_id}  transcribing ({mname}/{device}/{compute})…", flush=True)

    t0 = time.time()
    segments_iter, info = model.transcribe(str(audio), vad_filter=True)
    segments = [
        {"start": round(s.start, 3), "end": round(s.end, 3),
         "text": s.text.strip()}
        for s in segments_iter if s.text.strip()
    ]
    elapsed = time.time() - t0

    # preserve the previous transcript with its provenance
    tpath = ep_dir / "transcript.json"
    if tpath.exists():
        old = json.loads(tpath.read_text(encoding="utf-8"))
        old_src = old.get("transcript_source", "unknown")
        backup = ep_dir / f"transcript.{old_src}.json"
        if not backup.exists():
            tpath.rename(backup)

    transcript = {
        "episode_id": episode_id,
        "source_id": meta["source_id"],
        "transcript_source": "whisper",
        "language": getattr(info, "language", "en"),
        "model": f"faster-whisper/{mname}/{compute}",
        "segments": segments,
        "normalized_at": datetime.now().isoformat(),
    }
    tpath.write_text(json.dumps(transcript, ensure_ascii=False, indent=1),
                     encoding="utf-8")

    meta["status"] = "whisper"
    (ep_dir / "metadata.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")

    if not quiet:
        dur = meta.get("duration") or 0
        speed = (dur / elapsed) if elapsed and dur else 0
        print(f"  {episode_id}  done: {len(segments)} segments in "
              f"{elapsed/60:.1f} min ({speed:.1f}x realtime)", flush=True)

    return {"episode_id": episode_id, "segments": len(segments),
            "elapsed_s": round(elapsed, 1)}


PRIORITY = {"needs_transcription": 0, "captions_pending": 1}


def _queue(source: str | None = None) -> list[Path]:
    root = ensure_root()
    out = []
    episodes_root = root / "episodes"
    for src_dir in sorted(episodes_root.iterdir()) if episodes_root.exists() else []:
        if not src_dir.is_dir():
            continue
        if source and src_dir.name != source:
            continue
        for ep_dir in sorted(src_dir.iterdir()):
            if not ep_dir.is_dir():
                continue
            st = episode_status(ep_dir)
            if st == "whisper":
                continue
            meta = load_metadata(ep_dir)
            if not meta:
                continue
            # priority: no transcript at all, then auto captions, then manual
            if st in PRIORITY:
                prio = PRIORITY[st]
            else:
                prio = 2 if meta.get("caption_kind") == "auto" else 3
            out.append((prio, ep_dir))
    out.sort(key=lambda x: (x[0], str(x[1])))
    return [d for _, d in out]


def transcribe_batch(source: str | None = None, limit: int | None = None,
                     quiet: bool = False) -> dict:
    root = ensure_root()
    queue = _queue(source)
    if limit is not None:
        queue = queue[:limit]

    result = {"queued": len(queue), "done": 0, "failed": 0,
              "stopped_reason": None, "failures": []}

    for ep_dir in queue:
        if _free_gb(root) < _disk_floor_gb():
            result["stopped_reason"] = (
                f"free disk below {_disk_floor_gb()} GB floor")
            break
        try:
            transcribe_episode(ep_dir, quiet=quiet)
            result["done"] += 1
        except Exception as e:
            result["failed"] += 1
            result["failures"].append({"episode": ep_dir.name, "error": str(e)})
            print(f"  {ep_dir.name}  FAILED: {e}", flush=True)
        time.sleep(SLEEP_BETWEEN_DOWNLOADS)

    return result
