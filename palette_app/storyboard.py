"""Storyboards: an ordered, annotated sequence of chosen panels.

A contact sheet is mechanical - every Nth frame, whether or not it means
anything. A storyboard is the opposite: a few frames chosen on purpose, put in
the order that tells the story, each carrying the note that says why it is
there. The two share a grid and nothing else, so this keeps its own model
rather than growing more parameters onto contact_sheet().

Boards are documents, not media, so they live one JSON file per board under
<root>/storyboards instead of inside library.json. Note text would bloat the
media database, and a board being edited would otherwise contend with every
tag and palette write for the same file.
"""
import json
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from .api.media import fmt_timecode

BOARDS_DIRNAME = "storyboards"

# Panels are letterboxed into one uniform box so the grid reads as a sequence
# of frames. Mixed aspect ratios pasted at their own size read as a scrapbook.
DEFAULT_ASPECT = 16 / 9

BG = (16, 16, 16)
PANEL_BG = (8, 8, 8)
RULE = (52, 52, 52)
NOTE_FG = (222, 222, 222)
META_FG = (255, 255, 80)
TITLE_FG = (245, 245, 245)
MISSING_FG = (150, 90, 90)


# -- Board storage ------------------------------------------------------------

def boards_dir(root: Path) -> Path:
    """Where a library keeps its boards. Deliberately does not create it.

    This is on the read path too - listing boards, opening one and deleting
    one all resolve through here. A GET that quietly makes a folder in
    someone's library is a side effect nobody asked for: merely visiting the
    Storyboard page was enough to grow a storyboards/ directory. Only
    save_board creates it, because only save_board has something to put in it.
    """
    return root / BOARDS_DIRNAME


def slugify(name: str) -> str:
    """A filename-safe stem. Never empty, so an export always has a name."""
    s = re.sub(r"[^a-zA-Z0-9]+", "_", (name or "")).strip("_").lower()
    return s[:60] or "storyboard"


def new_panel(item_id: str, *, note: str = "", source_item_id: Optional[str] = None,
              timecode: Optional[float] = None, frame: Optional[int] = None) -> dict:
    return {
        "id": str(uuid.uuid4()),
        "item_id": item_id,
        "note": note,
        "source_item_id": source_item_id,
        "timecode": timecode,
        "frame": frame,
    }


def new_board(name: str) -> dict:
    now = datetime.now().isoformat()
    return {
        "id": str(uuid.uuid4()),
        "name": name or "Untitled board",
        "created": now,
        "modified": now,
        "panels": [],
    }


def board_path(root: Path, bid: str) -> Path:
    # bid reaches this from a URL path segment and decides a filename, so it
    # has to be proved to be an id rather than a route out of the folder.
    if not re.fullmatch(r"[A-Za-z0-9-]{1,64}", bid or ""):
        raise ValueError("bad board id: %r" % (bid,))
    return boards_dir(root) / (bid + ".json")


def save_board(root: Path, board: dict) -> dict:
    board["modified"] = datetime.now().isoformat()
    path = board_path(root, board["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(board, indent=2, ensure_ascii=False),
                    encoding="utf-8")
    return board


def load_board(root: Path, bid: str) -> Optional[dict]:
    p = board_path(root, bid)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        return None


def delete_board(root: Path, bid: str) -> bool:
    p = board_path(root, bid)
    if not p.exists():
        return False
    p.unlink()
    return True


def list_boards(root: Path) -> list:
    """Summaries only, newest edit first - the index never loads note text."""
    out = []
    d = boards_dir(root)
    if not d.is_dir():
        return out
    for p in sorted(d.glob("*.json")):
        try:
            b = json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            continue        # a half-written board must not break the index
        out.append({"id": b.get("id", p.stem), "name": b.get("name"),
                    "panels": len(b.get("panels") or []),
                    "created": b.get("created"), "modified": b.get("modified")})
    out.sort(key=lambda b: b.get("modified") or "", reverse=True)
    return out


def derive_frame(timecode: Optional[float], fps: Optional[float]) -> Optional[int]:
    """The frame a timecode lands on, when the source's rate is known."""
    if timecode is None or not fps:
        return None
    return int(round(timecode * fps))


# -- Text ---------------------------------------------------------------------

def wrap_text(text: str, max_width: float, measure) -> list:
    """Greedy word wrap. `measure(str) -> width`, so this stays font-agnostic.

    Explicit newlines in a note are the author's paragraph breaks and survive.
    A single word wider than the column is broken rather than allowed to run
    off the panel it belongs to.
    """
    lines = []
    for para in (text or "").split("\n"):
        words = para.split()
        if not words:
            lines.append("")
            continue
        cur = ""
        for word in words:
            trial = (cur + " " + word).strip()
            if cur and measure(trial) > max_width:
                lines.append(cur)
                cur = word
            else:
                cur = trial
            while measure(cur) > max_width and len(cur) > 1:
                cut = len(cur) - 1
                while cut > 1 and measure(cur[:cut]) > max_width:
                    cut -= 1
                lines.append(cur[:cut])
                cur = cur[cut:]
        if cur:
            lines.append(cur)
    while lines and lines[-1] == "":
        lines.pop()
    return lines


def meta_line(panel: dict, number: int, source_title: Optional[str] = None) -> str:
    """The one line under a panel saying which shot this is and where from."""
    parts = ["%d." % number]
    if source_title:
        parts.append(source_title)
    if panel.get("timecode") is not None:
        parts.append(fmt_timecode(float(panel["timecode"])))
    if panel.get("frame") is not None:
        parts.append("f%d" % int(panel["frame"]))
    return "  ·  ".join(parts)


def _font(size: int):
    from PIL import ImageFont

    for name in ("arial.ttf", "Arial.ttf", "DejaVuSans.ttf", "Helvetica.ttc"):
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    try:
        return ImageFont.load_default(size=size)   # Pillow >= 10.1
    except TypeError:
        return ImageFont.load_default()


# -- Render -------------------------------------------------------------------

def render_storyboard(
    panels: list,
    out_path: Path,
    title: Optional[str] = None,
    cols: int = 3,
    tile_width: int = 360,
    aspect: float = DEFAULT_ASPECT,
    padding: int = 16,
    max_width: Optional[int] = None,
) -> dict:
    """Compose an ordered, annotated board into one image.

    `panels` are dicts of {image: Path|None, note: str, timecode, frame,
    source_title}. A panel whose file has gone missing renders as a marked
    placeholder rather than aborting the board - losing one frame should not
    cost the notes written on all the others.
    """
    from PIL import Image, ImageDraw

    if not panels:
        return {"ok": False, "error": "storyboard has no panels"}

    # A board with fewer panels than columns would otherwise render most
    # of an empty canvas, so the grid never gets wider than it has panels
    # for - the same reason the last row is not padded out.
    ncols = max(1, min(cols, len(panels)))
    tw = max(64, tile_width)
    th = max(1, round(tw / (aspect or DEFAULT_ASPECT)))

    meta_font = _font(max(11, tw // 26))
    note_font = _font(max(12, tw // 24))
    title_font = _font(max(18, tw // 12))

    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))

    def width_of(text, font):
        return probe.textlength(text, font=font)

    def line_height(font):
        box = font.getbbox("Ag")
        return int((box[3] - box[1]) * 1.55) or 14

    meta_h = line_height(meta_font)
    note_h = line_height(note_font)
    gap = max(6, padding // 2)

    # Wrap every note first: a row is only as tall as its own longest caption,
    # so one panel with a paragraph does not pad out the whole board.
    wrapped = [wrap_text(p.get("note") or "", tw,
                         lambda s: width_of(s, note_font)) for p in panels]

    rows = [list(range(i, min(i + ncols, len(panels))))
            for i in range(0, len(panels), ncols)]
    row_heights = []
    for row in rows:
        lines = max((len(wrapped[i]) for i in row), default=0)
        row_heights.append(th + gap + meta_h + (lines * note_h) + gap)

    head_h = (line_height(title_font) + padding) if title else 0
    sheet_w = ncols * tw + (ncols + 1) * padding
    sheet_h = head_h + sum(row_heights) + (len(rows) + 1) * padding

    sheet = Image.new("RGB", (sheet_w, sheet_h), BG)
    draw = ImageDraw.Draw(sheet)

    if title:
        draw.text((padding, padding // 2), title, fill=TITLE_FG, font=title_font)

    missing = []
    y = head_h + padding
    for r, row in enumerate(rows):
        for c, idx in enumerate(row):
            panel = panels[idx]
            x = padding + c * (tw + padding)

            draw.rectangle([x, y, x + tw - 1, y + th - 1], fill=PANEL_BG)
            image_path = panel.get("image")
            drawn = False
            if image_path and Path(image_path).exists():
                try:
                    with Image.open(image_path) as im:
                        im = im.convert("RGB")
                        im.thumbnail((tw, th), Image.LANCZOS)
                        sheet.paste(im, (x + (tw - im.width) // 2,
                                         y + (th - im.height) // 2))
                    drawn = True
                except Exception:
                    drawn = False
            if not drawn:
                missing.append(idx + 1)
                label = "image unavailable"
                draw.text((x + (tw - width_of(label, meta_font)) / 2,
                           y + th / 2 - meta_h / 2),
                          label, fill=MISSING_FG, font=meta_font)
            draw.rectangle([x, y, x + tw - 1, y + th - 1], outline=RULE)

            ty = y + th + gap
            draw.text((x, ty), meta_line(panel, idx + 1, panel.get("source_title")),
                      fill=META_FG, font=meta_font)
            ty += meta_h
            for line in wrapped[idx]:
                draw.text((x, ty), line, fill=NOTE_FG, font=note_font)
                ty += note_h
        y += row_heights[r] + padding

    if max_width and sheet.width > max_width:
        ratio = max_width / sheet.width
        sheet = sheet.resize((max_width, max(1, int(sheet.height * ratio))),
                             Image.LANCZOS)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.suffix.lower() in (".jpg", ".jpeg"):
        sheet.save(out_path, quality=92)
    else:
        sheet.save(out_path)

    return {"ok": True, "panels": len(panels),
            "grid": "%dx%d" % (ncols, len(rows)),
            "width": sheet.width, "height": sheet.height,
            "missing": missing,
            "size_bytes": out_path.stat().st_size}
