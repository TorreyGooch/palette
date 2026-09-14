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
QUOTE_FG = (232, 226, 205)
QUOTE_RULE = (120, 110, 80)
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


def new_panel(item_id: Optional[str] = None, *, note: str = "",
              source_item_id: Optional[str] = None,
              timecode: Optional[float] = None, frame: Optional[int] = None,
              narration: Optional[dict] = None,
              image_prompt: str = "", video_prompt: str = "",
              candidates: Optional[list] = None) -> dict:
    """One beat of a piece: seen, heard, or asked for — at least one of them.

    Three text fields.

      note           why this beat is here. The audit trail that makes a board
                     a decision rather than an asset list.
      image_prompt   what is in this moment and how to shoot it.
      video_prompt   how it moves. Authored last, once the beat is settled and
                     the references exist.

    There was a fourth, `description`, split from `image_prompt` on the
    argument that plain language survives a change of model and a prompt does
    not. Three beats of real writing showed that holds for *how a thing is
    shot* and collapses for *what is in it*: "single lobster on wet dark rock"
    became "single lobster on wet black basalt", and the only thing the prompt
    added was styling. The subject got written twice, so the fields merged.

    What that knowingly gives up: rewriting a prompt for a new model now takes
    the plain-language account with it. Weighed and accepted — the simpler
    shape is worth more than the durable half of a field nobody wrote twice
    willingly.

    `note` stays separate, and that split did hold under the same test: "the
    argument is about mechanism, so look at it the way a biologist would" is
    not the same kind of sentence as anything you would hand a model.

    All three are authored rather than derived, so storing them is not a
    derive-don't-store violation: unlike `frame`, there is nothing to
    recompute them from.

    `candidates` holds generated references that have not been chosen between.
    A beat may have several and select none — that is the normal state after
    an unattended generation run, and selecting is a judgement left to a
    person unless someone asks otherwise.
    """
    return {
        "id": str(uuid.uuid4()),
        "item_id": item_id,
        "note": note,
        "source_item_id": source_item_id,
        "timecode": timecode,
        "frame": frame,
        "narration": narration,
        "image_prompt": image_prompt,
        "video_prompt": video_prompt,
        "candidates": list(candidates or []),
    }


def new_board(name: str, description: str = "") -> dict:
    """A piece, and what it is.

    `description` is the whole video in plain language — the thing no beat can
    say, because a beat only knows about its own frame. It is also the level
    that survives everything below it: shots get recut, prompts get rewritten
    for a new model, references get regenerated, and what the piece is about
    does not move.

    A beat briefly had a `description` of its own, and it kept turning into
    the subject line of its own prompt written twice. The thing that actually
    wanted describing in plain language was never the shot; it was the piece.

    A whole-video prompt will want to live here too, beside this rather than
    instead of it, once there is a video model to write one at. Deliberately
    absent until then: the shape of that prompt is decided by whatever
    consumes it, and guessing now would only have to be undone.
    """
    now = datetime.now().isoformat()
    return {
        "id": str(uuid.uuid4()),
        "name": name or "Untitled board",
        "description": description,
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
    """Write a board atomically.

    It used to be a plain write_text, which truncates first - so a generate
    job attaching references and an autosave landing together could leave a
    half-written board, and a board is someone's notes. Callers that load,
    change and save must also hold `library_lock`; this only guarantees the
    file is never a fragment.
    """
    from .library import write_json_atomic

    board["modified"] = datetime.now().isoformat()
    path = board_path(root, board["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(path, board, indent=2, ensure_ascii=False)
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
    subtitle: Optional[str] = None,
) -> dict:
    """Compose an ordered, annotated board into one image.

    `panels` are dicts of {image: Path|None, quote: str|None, caption: str,
    asked: bool, timecode, frame, source_title}. `caption` is the one text
    printed under a panel; `asked` says the beat has writing, so having no
    picture yet is not a fault. `subtitle` is printed under the title, and
    only when there is a title.

    A beat that is heard and not seen has no image and renders as a quote card,
    because the words are the spine and a beat carrying only words is a normal
    beat, not a hole. A beat whose image file has gone renders as a marked
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

    # One caption per panel: what happens in the shot. It used to be the note
    # and then the image prompt, both in full - the reasoning and the craft
    # instructions - and a board of real writing became a wall of text that
    # buried the pictures it was annotating. Both still live on the page, where
    # the work happens. The PNG is the handoff, and what it hands over is what
    # the video should do.
    #
    # Row height still follows the longest caption *in that row*, so one panel
    # with a paragraph does not pad out the whole board.
    captions = [wrap_text(p.get("caption") or "", tw,
                          lambda s: width_of(s, note_font)) for p in panels]

    rows = [list(range(i, min(i + ncols, len(panels))))
            for i in range(0, len(panels), ncols)]
    row_heights = []
    for row in rows:
        lines = max((len(captions[i]) for i in row), default=0)
        row_heights.append(th + gap + meta_h + (lines * note_h) + gap)

    sheet_w = ncols * tw + (ncols + 1) * padding
    # What the whole piece is, once, under its name - the account a reader
    # needs before any single shot makes sense. It belongs to the header, so
    # dropping the title drops it too.
    sub_lines = (wrap_text((subtitle or "").strip(), sheet_w - 2 * padding,
                           lambda s: width_of(s, note_font))
                 if title and (subtitle or "").strip() else [])
    head_h = ((line_height(title_font) + padding
               + (len(sub_lines) * note_h + gap if sub_lines else 0))
              if title else 0)
    sheet_h = head_h + sum(row_heights) + (len(rows) + 1) * padding

    sheet = Image.new("RGB", (sheet_w, sheet_h), BG)
    draw = ImageDraw.Draw(sheet)

    if title:
        draw.text((padding, padding // 2), title, fill=TITLE_FG, font=title_font)
        sy = padding // 2 + line_height(title_font)
        for line in sub_lines:
            draw.text((padding, sy), line, fill=NOTE_FG, font=note_font)
            sy += note_h

    missing = []
    y = head_h + padding
    for r, row in enumerate(rows):
        for c, idx in enumerate(row):
            panel = panels[idx]
            x = padding + c * (tw + padding)

            draw.rectangle([x, y, x + tw - 1, y + th - 1], fill=PANEL_BG)
            image_path = panel.get("image")
            quote = (panel.get("quote") or "").strip()
            asked = bool(panel.get("asked"))
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
            # An image that was asked for and could not be shown is always
            # worth reporting, even when a quote carries the beat anyway.
            if image_path and not drawn:
                missing.append(idx + 1)
            if not drawn and quote:
                lines = wrap_text('"' + quote + '"', tw - 3 * gap,
                                  lambda s: width_of(s, note_font))
                shown = lines[:max(1, int((th - gap) // note_h))]
                qy = y + max(gap, (th - len(shown) * note_h) // 2)
                draw.rectangle([x + gap, qy - 4, x + gap + 2,
                                qy + len(shown) * note_h], fill=QUOTE_RULE)
                for line in shown:
                    draw.text((x + 2 * gap, qy), line, fill=QUOTE_FG,
                              font=note_font)
                    qy += note_h
                drawn = True
            if not drawn:
                # Nothing to show at all is worth reporting. A beat with
                # writing on it asked for no image yet, so it is not missing
                # anything - the same distinction the page draws, and the
                # reason missing[] is trustworthy.
                if not image_path and not asked:
                    missing.append(idx + 1)
                # Three states, not two: a lost file is a fault, a beat with
                # nothing shot yet is doing its job, and an empty beat is
                # neither.
                label = ("image unavailable" if image_path
                         else "nothing shot yet" if asked
                         else "empty beat")
                draw.text((x + (tw - width_of(label, meta_font)) / 2,
                           y + th / 2 - meta_h / 2),
                          label, fill=MISSING_FG, font=meta_font)
            draw.rectangle([x, y, x + tw - 1, y + th - 1], outline=RULE)

            ty = y + th + gap
            draw.text((x, ty), meta_line(panel, idx + 1, panel.get("source_title")),
                      fill=META_FG, font=meta_font)
            ty += meta_h
            for line in captions[idx]:
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
