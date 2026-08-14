import asyncio
import json
import math
import shutil
import tempfile
from pathlib import Path
from typing import Optional


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


async def _run(cmd: list[str]) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    return proc.returncode, out, err


async def probe(video_path: Path) -> dict:
    """Return {duration, fps} for a video file."""
    cmd = [
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-show_format", "-show_streams", str(video_path),
    ]
    code, out, _ = await _run(cmd)
    result = {"duration": None, "fps": None}
    try:
        data = json.loads(out)
        result["duration"] = float(data["format"]["duration"])
        for s in data.get("streams", []):
            if s.get("codec_type") == "video":
                num, den = s.get("r_frame_rate", "30/1").split("/")
                result["fps"] = round(float(num) / float(den), 6)
                break
    except Exception:
        pass
    return result


async def video_thumbnail(video_path: Path, thumb_path: Path, time: float = 0.5) -> bool:
    cmd = [
        "ffmpeg", "-y",
        "-ss", str(max(time, 0)),
        "-i", str(video_path),
        "-vframes", "1",
        "-vf", "scale=320:-1",
        "-q:v", "3",
        str(thumb_path),
    ]
    await _run(cmd)
    return thumb_path.exists()


def image_thumbnail(image_path: Path, thumb_path: Path) -> bool:
    try:
        from PIL import Image

        img = Image.open(image_path)
        img.thumbnail((320, 320))
        img = img.convert("RGB")
        img.save(thumb_path, "JPEG", quality=85)
        return True
    except Exception:
        return False


async def audio_thumbnail(audio_path: Path, thumb_path: Path) -> bool:
    """Waveform image for audio items."""
    code, _, _ = await _run([
        "ffmpeg", "-y", "-i", str(audio_path),
        "-filter_complex",
        "showwavespic=s=320x180:colors=4a9eff,drawbox=x=0:y=89:w=iw:h=1:color=4a9eff",
        "-frames:v", "1", str(thumb_path),
    ])
    return code == 0 and thumb_path.exists()


async def extract_clip(source: Path, dest: Path, start: float, end: float) -> bool:
    # Fast seek + re-encode for frame-accurate cuts (same approach as vidset).
    duration = round(end - start, 6)
    cmd = [
        "ffmpeg", "-y",
        "-ss", str(start),
        "-i", str(source),
        "-t", str(duration),
        "-c:v", "libx264", "-crf", "18", "-preset", "fast",
        "-c:a", "aac",
        str(dest),
    ]
    code, _, _ = await _run(cmd)
    return code == 0 and dest.exists()


# ── Contact sheet ─────────────────────────────────────────────────────────────

def fmt_timecode(seconds: float) -> str:
    """m:ss.s, or h:mm:ss.s past the hour. Short enough to burn into a tile."""
    if seconds >= 3600:
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        return f"{h}:{m:02d}:{seconds % 60:04.1f}"
    return f"{int(seconds // 60)}:{seconds % 60:04.1f}"


def page_paths(out_path: Path, page: int) -> Path:
    """Sheet 3 of a series named .../foo_sheet_2026.jpg → .../foo_sheet_2026_p003.jpg."""
    return out_path.with_name(f"{out_path.stem}_p{page:03d}{out_path.suffix}")


async def contact_sheet(
    video_path: Path,
    out_path: Path,
    every_n: int = 30,
    cols: int = 4,
    tile_width: int = 320,
    padding: int = 8,
    order: str = "rows",       # "rows" (L→R, T→B) | "cols" (T→B, L→R)
    labels: bool = False,
    max_width: Optional[int] = None,
    start: Optional[float] = None,
    end: Optional[float] = None,
    rows: Optional[int] = None,   # tiles per sheet = cols*rows; None → one tall sheet
    fps: Optional[float] = None,  # lets labels and the index carry real timecodes
) -> dict:
    """Extract every Nth frame and compose it into one grid image, or into a
    series of them when `rows` is set.

    A whole video at one sample per second is thousands of tiles. As a single
    image that is unreadable and, past Pillow's limits, unopenable; paged into
    cols×rows sheets it stays legible and each sheet is small enough to hand to
    a model. Sampling is identical either way — paging only decides where the
    grid breaks, so tile k lands on the same source frame whatever `rows` is.
    """
    from PIL import Image, ImageDraw, ImageFont

    ncols = max(1, cols)
    per_sheet = ncols * rows if rows and rows > 0 else None

    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        # Extract sampled frames scaled to tile width
        cmd = ["ffmpeg", "-y"]
        if start is not None:
            cmd += ["-ss", str(start)]
        cmd += ["-i", str(video_path)]
        if end is not None:
            dur = end - (start or 0)
            cmd += ["-t", str(dur)]
        cmd += [
            "-vf", f"select=not(mod(n\\,{every_n})),scale={tile_width}:-2",
            "-vsync", "vfr",
            "-q:v", "3",
            str(tmpdir / "f_%05d.jpg"),
        ]
        code, _, err = await _run(cmd)
        frames = sorted(tmpdir.glob("f_*.jpg"))
        if not frames:
            return {"ok": False, "error": err.decode(errors="replace")[-500:]}

        loop = asyncio.get_event_loop()

        # Frame numbers and times are absolute in the source, not relative to
        # the page or to `start` — a label is only useful if you can seek to it.
        t0 = start or 0.0
        base_frame = int(round(t0 * fps)) if fps else 0

        def _time_of(global_idx: int) -> Optional[float]:
            return t0 + (global_idx * every_n) / fps if fps else None

        def _compose_page(page_frames: list, dest: Path, first_idx: int) -> dict:
            imgs = [Image.open(f) for f in page_frames]
            try:
                tw = imgs[0].width
                th = max(i.height for i in imgs)
                n = len(imgs)
                nrows = math.ceil(n / ncols)

                sheet_w = ncols * tw + (ncols + 1) * padding
                sheet_h = nrows * th + (nrows + 1) * padding
                sheet = Image.new("RGB", (sheet_w, sheet_h), (16, 16, 16))
                draw = ImageDraw.Draw(sheet)
                try:
                    font = ImageFont.truetype("arial.ttf", max(12, tw // 20))
                except Exception:
                    font = ImageFont.load_default()

                for idx, img in enumerate(imgs):
                    if order == "cols":
                        col = idx // nrows
                        row = idx % nrows
                    else:
                        row = idx // ncols
                        col = idx % ncols
                    x = padding + col * (tw + padding)
                    y = padding + row * (th + padding)
                    sheet.paste(img, (x, y))
                    if labels:
                        g = first_idx + idx
                        text = f"{base_frame + g * every_n}"
                        at = _time_of(g)
                        if at is not None:
                            text += f"  {fmt_timecode(at)}"
                        tx, ty = x + 4, y + 4
                        # simple outline for readability
                        for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                            draw.text((tx + dx, ty + dy), text, fill=(0, 0, 0), font=font)
                        draw.text((tx, ty), text, fill=(255, 255, 80), font=font)

                if max_width and sheet.width > max_width:
                    ratio = max_width / sheet.width
                    sheet = sheet.resize(
                        (max_width, int(sheet.height * ratio)), Image.LANCZOS
                    )

                sheet.save(dest, quality=92)
                last = first_idx + n - 1
                return {
                    "filename": dest.name,
                    "frames": n,
                    "grid": f"{ncols}x{nrows}",
                    "width": sheet.width,
                    "height": sheet.height,
                    "first_frame": base_frame + first_idx * every_n,
                    "last_frame": base_frame + last * every_n,
                    "start_time": _time_of(first_idx),
                    "end_time": _time_of(last),
                }
            finally:
                for i in imgs:
                    i.close()

        def _compose_all():
            # Only one page's worth of images is ever open at a time, which is
            # what makes a two-hour source survivable.
            size = per_sheet or len(frames)
            pages = [frames[i:i + size] for i in range(0, len(frames), size)]
            sheets = []
            for p, chunk in enumerate(pages, start=1):
                dest = out_path if per_sheet is None else page_paths(out_path, p)
                sheets.append({"page": p, **_compose_page(chunk, dest, (p - 1) * size)})
            first = sheets[0]
            return {
                "ok": True,
                "frames": len(frames),
                "sheets": sheets,
                "sheet_count": len(sheets),
                # First sheet's shape, kept flat so single-sheet callers that
                # predate paging read the same fields they always did.
                "grid": first["grid"],
                "width": first["width"],
                "height": first["height"],
            }

        return await loop.run_in_executor(None, _compose_all)


# ── Video export ──────────────────────────────────────────────────────────────

async def export_video(
    video_path: Path,
    out_path: Path,
    start: Optional[float] = None,
    end: Optional[float] = None,
    scale_width: Optional[int] = None,
    fps: Optional[float] = None,
) -> dict:
    cmd = ["ffmpeg", "-y"]
    if start is not None:
        cmd += ["-ss", str(start)]
    cmd += ["-i", str(video_path)]
    if end is not None:
        cmd += ["-t", str(end - (start or 0))]
    vf = []
    if scale_width:
        vf.append(f"scale={scale_width}:-2")
    if vf:
        cmd += ["-vf", ",".join(vf)]
    if fps:
        cmd += ["-r", str(fps)]
    cmd += ["-c:v", "libx264", "-crf", "18", "-preset", "fast", "-c:a", "aac", str(out_path)]
    code, _, err = await _run(cmd)
    if code != 0 or not out_path.exists():
        return {"ok": False, "error": err.decode(errors="replace")[-500:]}
    return {"ok": True, "size_bytes": out_path.stat().st_size}
