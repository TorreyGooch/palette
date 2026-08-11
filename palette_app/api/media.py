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
) -> dict:
    """Extract every Nth frame, compose into a grid image. Returns metadata."""
    from PIL import Image, ImageDraw, ImageFont

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

        def _compose():
            imgs = [Image.open(f) for f in frames]
            tw = imgs[0].width
            th = max(i.height for i in imgs)
            n = len(imgs)
            ncols = max(1, cols)
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
                    frame_no = idx * every_n
                    text = f"{frame_no}"
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

            sheet.save(out_path, quality=92)
            return {
                "ok": True,
                "frames": n,
                "grid": f"{ncols}x{nrows}",
                "width": sheet.width,
                "height": sheet.height,
            }

        return await loop.run_in_executor(None, _compose)


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
