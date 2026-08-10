import asyncio
import shutil
from pathlib import Path
from typing import Optional


def _time_to_seconds(t: str) -> float:
    parts = t.strip().split(":")
    parts = [float(p) for p in parts]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    return parts[0]


async def download_url(
    url: str,
    dest_dir: Path,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
) -> list[dict]:
    """Download via yt-dlp. Returns list of {title, filename}."""
    import yt_dlp

    ydl_opts = {
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "outtmpl": str(dest_dir / "%(title)s.%(ext)s"),
        "windowsfilenames": True,
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
    }

    if start_time or end_time:
        start_s = _time_to_seconds(start_time) if start_time else 0
        end_s = _time_to_seconds(end_time) if end_time else None
        ranges = [[start_s, end_s]] if end_s else [[start_s, float("inf")]]
        ydl_opts["download_ranges"] = yt_dlp.utils.download_range_func(None, ranges)
        ydl_opts["force_keyframes_at_cuts"] = True

    loop = asyncio.get_event_loop()

    def _run():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if info is None:
                return []
            entries = info.get("entries") or [info]
            results = []
            for entry in entries:
                if not entry:
                    continue
                filename = ydl.prepare_filename(entry)
                p = Path(filename)
                if not p.exists():
                    p = p.with_suffix(".mp4")
                results.append(
                    {"title": entry.get("title", p.stem), "filename": str(p)}
                )
            return results

    return await loop.run_in_executor(None, _run)


async def import_local(source_path: Path, dest_dir: Path) -> dict:
    """Copy a local video file into source/. Returns {title, filename}."""
    dest = dest_dir / source_path.name
    if source_path.resolve() != dest.resolve():
        await asyncio.get_event_loop().run_in_executor(
            None, shutil.copy2, str(source_path), str(dest)
        )
    title = source_path.stem.replace("_", " ").replace("-", " ")
    return {"title": title, "filename": dest.name}
