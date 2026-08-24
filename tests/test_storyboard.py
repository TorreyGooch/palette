"""Storyboards: the board document, the caption layout, and the render.

Pillow does its real work here — the render tests read pixels back out of the
composed image, because the thing worth proving is that panel 3 is where panel
3 belongs. Nothing decodes video and nothing touches the network.
"""
import io
import json

import pytest
from fastapi import HTTPException
from PIL import Image

from conftest import add_item
from palette_app import storyboard as sb


# ── helpers ───────────────────────────────────────────────────────────────────

def png_bytes(color=(200, 30, 30), size=(320, 180)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


def write_png(path, color=(200, 30, 30), size=(320, 180)):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path)
    return path


def panel(image=None, note="", **kw):
    p = {"image": image, "note": note}
    p.update(kw)
    return p


def column_colors(img: Image.Image, x: int, wanted):
    """Colours from `wanted` met walking down column x, in order, deduped.

    Lets a test assert panel order without hard-coding row geometry.
    """
    seen = []
    for y in range(img.height):
        px = img.getpixel((x, y))
        if px in wanted and (not seen or seen[-1] != px):
            seen.append(px)
    return seen


# ── board storage ─────────────────────────────────────────────────────────────

def test_board_roundtrips_through_disk(library):
    board = sb.new_board("Opening sequence")
    board["panels"].append(sb.new_panel("item-1", note="wide"))
    sb.save_board(library, board)

    loaded = sb.load_board(library, board["id"])
    assert loaded["name"] == "Opening sequence"
    assert loaded["panels"][0]["note"] == "wide"
    assert loaded["panels"][0]["item_id"] == "item-1"


def test_board_lands_outside_library_json(library):
    """A board is a document, not media — library.json must not grow one."""
    board = sb.save_board(library, sb.new_board("B"))
    lib = json.loads((library / "library.json").read_text(encoding="utf-8"))
    assert "storyboards" not in lib
    assert (library / "storyboards" / f"{board['id']}.json").exists()


def test_missing_board_loads_as_none(library):
    assert sb.load_board(library, "0e5d1f2a-0000-0000-0000-000000000000") is None


def test_delete_reports_whether_anything_went(library):
    board = sb.save_board(library, sb.new_board("B"))
    assert sb.delete_board(library, board["id"]) is True
    assert sb.delete_board(library, board["id"]) is False


@pytest.mark.parametrize("bad", [
    "../../etc/passwd", "..", "a/b", "a\\b", "", "x" * 65, "id.with.dots",
])
def test_board_id_that_is_not_an_id_is_refused(library, bad):
    """bid decides a filename, so anything path-shaped has to be rejected."""
    with pytest.raises(ValueError):
        sb.board_path(library, bad)


def test_index_is_newest_edit_first(library):
    a = sb.save_board(library, sb.new_board("A"))
    b = sb.save_board(library, sb.new_board("B"))
    a["modified"] = "2030-01-01T00:00:00"
    sb.board_path(library, a["id"]).write_text(json.dumps(a), encoding="utf-8")

    names = [x["name"] for x in sb.list_boards(library)]
    assert names == ["A", "B"]
    assert b["name"] == "B"


def test_index_counts_panels_without_loading_notes(library):
    board = sb.new_board("B")
    board["panels"] = [sb.new_panel("i1", note="x" * 500), sb.new_panel("i2")]
    sb.save_board(library, board)

    row = sb.list_boards(library)[0]
    assert row["panels"] == 2
    assert "note" not in json.dumps(row)


def test_a_corrupt_board_does_not_break_the_index(library):
    sb.save_board(library, sb.new_board("Good"))
    (sb.boards_dir(library) / "half-written.json").write_text("{oops", encoding="utf-8")

    assert [b["name"] for b in sb.list_boards(library)] == ["Good"]


def test_save_stamps_modified(library):
    board = sb.new_board("B")
    board["modified"] = "2001-01-01T00:00:00"
    sb.save_board(library, board)
    assert board["modified"] != "2001-01-01T00:00:00"


# ── small pure pieces ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("name,expected", [
    ("Opening sequence", "opening_sequence"),
    ("  Act II — the turn!  ", "act_ii_the_turn"),
    ("", "storyboard"),
    ("!!!", "storyboard"),
    ("../../escape", "escape"),
])
def test_slugify(name, expected):
    assert sb.slugify(name) == expected


def test_slugify_is_bounded():
    assert len(sb.slugify("word " * 100)) <= 60


@pytest.mark.parametrize("tc,fps,expected", [
    (10.0, 30.0, 300),
    (10.0, 29.97, 300),
    (0.0, 30.0, 0),
    (None, 30.0, None),
    (10.0, None, None),
    (10.0, 0, None),
])
def test_derive_frame(tc, fps, expected):
    assert sb.derive_frame(tc, fps) == expected


# ── wrapping ──────────────────────────────────────────────────────────────────

CHAR = lambda s: len(s) * 10.0     # noqa: E731 — 10px per character


def test_wrap_breaks_on_words():
    assert sb.wrap_text("one two three", 80, CHAR) == ["one two", "three"]
    assert sb.wrap_text("one two three", 60, CHAR) == ["one", "two", "three"]


def test_wrap_keeps_paragraph_breaks():
    assert sb.wrap_text("a\n\nb", 100, CHAR) == ["a", "", "b"]


def test_wrap_drops_only_trailing_blanks():
    assert sb.wrap_text("a\n\n", 100, CHAR) == ["a"]


def test_wrap_hard_breaks_a_word_wider_than_the_column():
    """A long word must not run out past the panel it belongs to."""
    lines = sb.wrap_text("supercalifragilistic", 50, CHAR)
    assert lines == ["super", "calif", "ragil", "istic"]
    assert all(CHAR(line) <= 50 for line in lines)


def test_wrap_of_nothing_is_no_lines():
    assert sb.wrap_text("", 100, CHAR) == []
    assert sb.wrap_text(None, 100, CHAR) == []


def test_wrap_never_exceeds_the_column():
    text = "the quick brown fox jumps over the lazy dog " * 4
    assert all(CHAR(line) <= 80 for line in sb.wrap_text(text, 80, CHAR))


# ── the meta line ─────────────────────────────────────────────────────────────

def test_meta_line_is_just_a_number_when_nothing_is_known():
    assert sb.meta_line({}, 3) == "3."


def test_meta_line_carries_source_timecode_and_frame():
    line = sb.meta_line({"timecode": 83.5, "frame": 2505}, 2, "trippy.mp4")
    assert line.startswith("2.")
    assert "trippy.mp4" in line
    assert "1:23.5" in line
    assert "f2505" in line


def test_meta_line_omits_what_is_missing():
    assert "f" not in sb.meta_line({"timecode": 5.0}, 1).replace("5.0", "")


def test_meta_line_shows_frame_zero():
    """Frame 0 is a real frame; `if frame:` would swallow it."""
    assert "f0" in sb.meta_line({"frame": 0}, 1)


def test_meta_line_shows_timecode_zero():
    assert "0:00.0" in sb.meta_line({"timecode": 0.0}, 1)


# ── render ────────────────────────────────────────────────────────────────────

def test_empty_board_refuses_rather_than_writing_a_blank(tmp_path):
    out = tmp_path / "b.png"
    result = sb.render_storyboard([], out)
    assert result["ok"] is False
    assert not out.exists()


def test_render_writes_the_file_and_reports_its_shape(tmp_path):
    imgs = [write_png(tmp_path / f"{i}.png") for i in range(5)]
    out = tmp_path / "board.png"
    result = sb.render_storyboard([panel(p, "note") for p in imgs], out, cols=3)

    assert result["ok"] is True
    assert result["panels"] == 5
    assert result["grid"] == "3x2"
    assert out.exists()
    assert result["size_bytes"] == out.stat().st_size


@pytest.mark.parametrize("n,cols,grid", [
    (1, 3, "1x1"), (2, 3, "2x1"), (3, 3, "3x1"),
    (4, 3, "3x2"), (5, 3, "3x2"), (6, 3, "3x2"), (7, 3, "3x3"),
    (4, 1, "1x4"),
])
def test_the_last_row_is_not_padded_out(tmp_path, n, cols, grid):
    imgs = [write_png(tmp_path / f"{i}.png") for i in range(n)]
    result = sb.render_storyboard([panel(p) for p in imgs],
                                  tmp_path / "b.png", cols=cols)
    assert result["grid"] == grid


def test_the_grid_never_gets_wider_than_it_has_panels_for(tmp_path):
    """Two panels at cols=3 must not render a third of the canvas empty."""
    img = write_png(tmp_path / "a.png")
    result = sb.render_storyboard([panel(img)] * 2, tmp_path / "b.png",
                                  cols=3, tile_width=100, padding=10)
    assert result["grid"] == "2x1"
    assert result["width"] == 2 * 100 + 3 * 10


def test_panels_render_in_board_order(tmp_path):
    """The whole point of a storyboard: the sequence is the content."""
    colors = [(255, 0, 0), (0, 255, 0), (0, 0, 255)]
    imgs = [write_png(tmp_path / f"{i}.png", c) for i, c in enumerate(colors)]
    out = tmp_path / "b.png"
    sb.render_storyboard([panel(p) for p in imgs], out, cols=1,
                         tile_width=120, padding=10)

    with Image.open(out) as img:
        assert column_colors(img, 70, set(colors)) == colors


def test_reordering_the_list_reorders_the_image(tmp_path):
    colors = [(255, 0, 0), (0, 255, 0), (0, 0, 255)]
    imgs = [write_png(tmp_path / f"{i}.png", c) for i, c in enumerate(colors)]
    out = tmp_path / "b.png"
    reordered = [imgs[2], imgs[0], imgs[1]]
    sb.render_storyboard([panel(p) for p in reordered], out, cols=1,
                         tile_width=120, padding=10)

    with Image.open(out) as img:
        assert column_colors(img, 70, set(colors)) == [colors[2], colors[0], colors[1]]


def test_a_missing_image_is_reported_not_fatal(tmp_path):
    good = write_png(tmp_path / "good.png")
    panels = [panel(good, "kept"), panel(tmp_path / "gone.png", "also kept"),
              panel(good, "kept too")]
    out = tmp_path / "b.png"
    result = sb.render_storyboard(panels, out, cols=3)

    assert result["ok"] is True
    assert result["missing"] == [2]        # 1-based, matches the printed number
    assert out.exists()


def test_a_panel_with_no_image_at_all_is_reported(tmp_path):
    out = tmp_path / "b.png"
    result = sb.render_storyboard([panel(None, "note")], out)
    assert result["missing"] == [1]
    assert result["ok"] is True


def test_an_unreadable_file_is_missing_rather_than_a_crash(tmp_path):
    junk = tmp_path / "junk.png"
    junk.write_bytes(b"not an image")
    result = sb.render_storyboard([panel(junk)], tmp_path / "b.png")
    assert result["missing"] == [1]


def test_nothing_missing_reports_an_empty_list(tmp_path):
    img = write_png(tmp_path / "a.png")
    assert sb.render_storyboard([panel(img)], tmp_path / "b.png")["missing"] == []


def test_a_long_note_makes_the_board_taller(tmp_path):
    img = write_png(tmp_path / "a.png")
    short = sb.render_storyboard([panel(img, "hi")] * 2, tmp_path / "s.png", cols=2)
    long_ = sb.render_storyboard(
        [panel(img, "a note long enough to wrap over several lines " * 4),
         panel(img, "hi")], tmp_path / "l.png", cols=2)
    assert long_["height"] > short["height"]


def test_a_row_is_only_as_tall_as_its_own_longest_note(tmp_path):
    """One panel with a paragraph must not pad out every other row."""
    img = write_png(tmp_path / "a.png")
    long_note = "a note long enough to wrap over several lines " * 4

    one = sb.render_storyboard(
        [panel(img, long_note), panel(img, "hi"), panel(img, "hi"), panel(img, "hi")],
        tmp_path / "one.png", cols=2)
    both = sb.render_storyboard(
        [panel(img, long_note), panel(img, long_note), panel(img, "hi"), panel(img, "hi")],
        tmp_path / "both.png", cols=2)
    split = sb.render_storyboard(
        [panel(img, long_note), panel(img, "hi"), panel(img, long_note), panel(img, "hi")],
        tmp_path / "split.png", cols=2)

    # Two long notes sharing a row cost the same as one; on separate rows they
    # cost twice — which is what a per-row height means.
    assert both["height"] == one["height"]
    assert split["height"] > one["height"]


def test_the_title_band_adds_height(tmp_path):
    img = write_png(tmp_path / "a.png")
    without = sb.render_storyboard([panel(img)], tmp_path / "n.png")
    with_ = sb.render_storyboard([panel(img)], tmp_path / "t.png", title="Act I")
    assert with_["height"] > without["height"]


def test_width_follows_columns_and_padding(tmp_path):
    img = write_png(tmp_path / "a.png")
    result = sb.render_storyboard([panel(img)] * 3, tmp_path / "b.png",
                                  cols=3, tile_width=100, padding=10)
    assert result["width"] == 3 * 100 + 4 * 10


def test_max_width_scales_the_whole_board_down(tmp_path):
    img = write_png(tmp_path / "a.png")
    big = sb.render_storyboard([panel(img)] * 3, tmp_path / "b.png",
                               cols=3, tile_width=400, padding=10)
    small = sb.render_storyboard([panel(img)] * 3, tmp_path / "s.png",
                                 cols=3, tile_width=400, padding=10,
                                 max_width=600)
    assert big["width"] > 600
    assert small["width"] == 600
    assert small["height"] < big["height"]


def test_max_width_leaves_a_smaller_board_alone(tmp_path):
    img = write_png(tmp_path / "a.png")
    result = sb.render_storyboard([panel(img)], tmp_path / "b.png",
                                  tile_width=100, padding=10, max_width=5000)
    assert result["width"] == 120


def test_aspect_decides_the_panel_box(tmp_path):
    img = write_png(tmp_path / "a.png")
    wide = sb.render_storyboard([panel(img)], tmp_path / "w.png",
                                tile_width=320, aspect=16 / 9)
    tall = sb.render_storyboard([panel(img)], tmp_path / "t.png",
                                tile_width=320, aspect=9 / 16)
    assert tall["height"] > wide["height"]
    assert tall["width"] == wide["width"]


def test_a_tall_image_is_letterboxed_not_stretched(tmp_path):
    """Panels share one box, so a 9:16 still must fit inside a 16:9 frame."""
    img = write_png(tmp_path / "tall.png", (255, 0, 0), size=(180, 320))
    out = tmp_path / "b.png"
    sb.render_storyboard([panel(img)], out, cols=1, tile_width=320,
                         aspect=16 / 9, padding=10)
    with Image.open(out) as im:
        # The panel box is 320x180; a stretched image would paint the corners.
        assert im.getpixel((12, 12)) != (255, 0, 0)
        assert im.getpixel((170, 100)) == (255, 0, 0)


def test_the_suffix_decides_the_format(tmp_path):
    img = write_png(tmp_path / "a.png")
    sb.render_storyboard([panel(img)], tmp_path / "b.png")
    sb.render_storyboard([panel(img)], tmp_path / "b.jpg")
    with Image.open(tmp_path / "b.png") as i:
        assert i.format == "PNG"
    with Image.open(tmp_path / "b.jpg") as i:
        assert i.format == "JPEG"


def test_render_creates_the_output_folder(tmp_path):
    img = write_png(tmp_path / "a.png")
    out = tmp_path / "nested" / "deeper" / "b.png"
    assert sb.render_storyboard([panel(img)], out)["ok"] is True
    assert out.exists()


def test_a_single_panel_still_renders(tmp_path):
    img = write_png(tmp_path / "a.png")
    result = sb.render_storyboard([panel(img, "the only shot")], tmp_path / "b.png")
    assert result["grid"] == "1x1"
    assert result["panels"] == 1


# ── API routes ────────────────────────────────────────────────────────────────

@pytest.fixture
def api(library, monkeypatch):
    """The route functions, pointed at a throwaway library."""
    from palette_app import main

    monkeypatch.setattr(main, "get_library_path", lambda: library)
    return main


def image_item(library, name, iid):
    item = add_item(library, name, iid, content=png_bytes())
    item["type"] = "image"
    lib = json.loads((library / "library.json").read_text(encoding="utf-8"))
    for entry in lib["items"]:
        if entry["id"] == iid:
            entry["type"] = "image"
    (library / "library.json").write_text(json.dumps(lib, indent=2), encoding="utf-8")
    return item


def test_create_then_open_a_board(api, library):
    board = api.storyboard_create(body={"name": "Act I"})
    opened = api.storyboard_get(board["id"])
    assert opened["name"] == "Act I"
    assert opened["panels"] == []


def test_a_nameless_board_still_gets_a_name(api):
    assert api.storyboard_create(body={})["name"] == "Untitled board"


def test_unknown_board_is_404(api):
    with pytest.raises(HTTPException) as e:
        api.storyboard_get("0e5d1f2a-0000-0000-0000-000000000000")
    assert e.value.status_code == 404


def test_a_path_shaped_board_id_is_404_not_a_crash(api):
    """The id reaches a filename; a traversal attempt answers like any miss."""
    with pytest.raises(HTTPException) as e:
        api.storyboard_get("../../library")
    assert e.value.status_code == 404


def test_adding_panels_appends_in_order(api, library):
    image_item(library, "a.png", "img-a")
    image_item(library, "b.png", "img-b")
    board = api.storyboard_create(body={"name": "B"})

    result = api.storyboard_add_panels(board["id"], body={"item_ids": ["img-a", "img-b"]})
    assert result["added"] == 2
    assert [p["item_id"] for p in result["panels"]] == ["img-a", "img-b"]


def test_panels_naming_items_that_do_not_exist_are_skipped(api, library):
    image_item(library, "a.png", "img-a")
    board = api.storyboard_create(body={"name": "B"})

    result = api.storyboard_add_panels(board["id"], body={"item_ids": ["img-a", "ghost"]})
    assert result["added"] == 1
    assert [p["item_id"] for p in result["panels"]] == ["img-a"]


def test_a_panel_carries_the_url_the_browser_needs(api, library):
    image_item(library, "a.png", "img-a")
    board = api.storyboard_create(body={"name": "B"})
    result = api.storyboard_add_panels(board["id"], body={"item_ids": ["img-a"]})

    assert result["panels"][0]["image_url"] == "/api/media/a.png"
    assert result["panels"][0]["missing"] is False


def test_a_panel_whose_image_left_the_library_says_so(api, library):
    image_item(library, "a.png", "img-a")
    board = api.storyboard_create(body={"name": "B"})
    api.storyboard_add_panels(board["id"], body={"item_ids": ["img-a"]})

    lib = json.loads((library / "library.json").read_text(encoding="utf-8"))
    lib["items"] = []
    (library / "library.json").write_text(json.dumps(lib), encoding="utf-8")

    opened = api.storyboard_get(board["id"])
    assert opened["panels"][0]["missing"] is True
    assert opened["panels"][0]["image_url"] is None


def test_patch_replaces_the_panel_list_wholesale(api, library):
    image_item(library, "a.png", "img-a")
    image_item(library, "b.png", "img-b")
    board = api.storyboard_create(body={"name": "B"})
    added = api.storyboard_add_panels(board["id"], body={"item_ids": ["img-a", "img-b"]})

    flipped = list(reversed(added["panels"]))
    result = api.storyboard_update(board["id"], body={"panels": flipped})
    assert [p["item_id"] for p in result["panels"]] == ["img-b", "img-a"]

    # and it survives a reload — the board is the stored document
    assert [p["item_id"] for p in api.storyboard_get(board["id"])["panels"]] \
        == ["img-b", "img-a"]


def test_patch_keeps_panel_ids_across_a_reorder(api, library):
    image_item(library, "a.png", "img-a")
    image_item(library, "b.png", "img-b")
    board = api.storyboard_create(body={"name": "B"})
    added = api.storyboard_add_panels(board["id"], body={"item_ids": ["img-a", "img-b"]})
    ids = {p["id"] for p in added["panels"]}

    result = api.storyboard_update(board["id"],
                                   body={"panels": list(reversed(added["panels"]))})
    assert {p["id"] for p in result["panels"]} == ids


def test_patch_can_rename_without_touching_panels(api, library):
    image_item(library, "a.png", "img-a")
    board = api.storyboard_create(body={"name": "Old"})
    api.storyboard_add_panels(board["id"], body={"item_ids": ["img-a"]})

    result = api.storyboard_update(board["id"], body={"name": "New"})
    assert result["name"] == "New"
    assert len(result["panels"]) == 1


def test_an_empty_rename_keeps_the_old_name(api):
    board = api.storyboard_create(body={"name": "Keep me"})
    assert api.storyboard_update(board["id"], body={"name": "   "})["name"] == "Keep me"


def test_a_panel_with_no_item_id_is_dropped(api, library):
    image_item(library, "a.png", "img-a")
    board = api.storyboard_create(body={"name": "B"})
    result = api.storyboard_update(
        board["id"], body={"panels": [{"item_id": "img-a"}, {"note": "orphan"}]})
    assert len(result["panels"]) == 1


def test_frame_is_derived_from_timecode_and_source_rate(api, library):
    image_item(library, "a.png", "img-a")
    add_item(library, "src.mp4", "vid-1", type="video", fps=30.0, duration=600.0)
    board = api.storyboard_create(body={"name": "B"})

    result = api.storyboard_update(board["id"], body={"panels": [
        {"item_id": "img-a", "timecode": 12.0, "source_item_id": "vid-1"}]})
    assert result["panels"][0]["frame"] == 360
    assert result["panels"][0]["source_title"] == "src.mp4"


def test_a_stale_client_frame_is_overwritten_by_the_derived_one(api, library):
    """The frame is a function of the timecode, so the client cannot pin it."""
    image_item(library, "a.png", "img-a")
    add_item(library, "src.mp4", "vid-1", type="video", fps=30.0)
    board = api.storyboard_create(body={"name": "B"})

    result = api.storyboard_update(board["id"], body={"panels": [
        {"item_id": "img-a", "timecode": 12.0,
         "source_item_id": "vid-1", "frame": 99999}]})
    assert result["panels"][0]["frame"] == 360


def test_without_a_source_the_typed_frame_is_kept(api, library):
    image_item(library, "a.png", "img-a")
    board = api.storyboard_create(body={"name": "B"})
    result = api.storyboard_update(board["id"], body={"panels": [
        {"item_id": "img-a", "timecode": 12.0, "frame": 42}]})
    assert result["panels"][0]["frame"] == 42


def test_blank_timecode_clears_rather_than_becoming_zero(api, library):
    image_item(library, "a.png", "img-a")
    board = api.storyboard_create(body={"name": "B"})
    result = api.storyboard_update(board["id"], body={"panels": [
        {"item_id": "img-a", "timecode": "", "frame": ""}]})
    assert result["panels"][0]["timecode"] is None
    assert result["panels"][0]["frame"] is None


def test_render_writes_a_png_into_exports(api, library):
    image_item(library, "a.png", "img-a")
    board = api.storyboard_create(body={"name": "Act I"})
    api.storyboard_add_panels(board["id"], body={"item_ids": ["img-a"]})

    result = api.storyboard_render(board["id"], body={"cols": 2})
    out = library / "exports" / result["filename"]
    assert out.exists()
    assert result["filename"].startswith("act_i_storyboard_")
    assert result["filename"].endswith(".png")
    with Image.open(out) as im:
        assert im.format == "PNG"


def test_rendering_an_empty_board_is_a_400(api):
    board = api.storyboard_create(body={"name": "B"})
    with pytest.raises(HTTPException) as e:
        api.storyboard_render(board["id"], body={})
    assert e.value.status_code == 400


def test_render_names_the_board_by_default(api, library):
    image_item(library, "a.png", "img-a")
    board = api.storyboard_create(body={"name": "Titled"})
    api.storyboard_add_panels(board["id"], body={"item_ids": ["img-a"]})

    titled = api.storyboard_render(board["id"], body={})
    untitled = api.storyboard_render(board["id"], body={"title": ""})
    assert titled["height"] > untitled["height"]


def test_render_reports_a_panel_whose_file_is_gone(api, library):
    image_item(library, "a.png", "img-a")
    board = api.storyboard_create(body={"name": "B"})
    api.storyboard_add_panels(board["id"], body={"item_ids": ["img-a"]})
    (library / "media" / "a.png").unlink()

    result = api.storyboard_render(board["id"], body={})
    assert result["missing"] == [1]
    assert result["ok"] is True


def test_delete_removes_the_board(api):
    board = api.storyboard_create(body={"name": "B"})
    api.storyboard_delete(board["id"])
    with pytest.raises(HTTPException):
        api.storyboard_get(board["id"])


def test_deleting_twice_is_a_404(api):
    board = api.storyboard_create(body={"name": "B"})
    api.storyboard_delete(board["id"])
    with pytest.raises(HTTPException) as e:
        api.storyboard_delete(board["id"])
    assert e.value.status_code == 404


def test_the_index_lists_created_boards(api):
    api.storyboard_create(body={"name": "One"})
    api.storyboard_create(body={"name": "Two"})
    assert {b["name"] for b in api.storyboards_index()} == {"One", "Two"}


def test_deleting_a_board_leaves_the_images_in_the_library(api, library):
    image_item(library, "a.png", "img-a")
    board = api.storyboard_create(body={"name": "B"})
    api.storyboard_add_panels(board["id"], body={"item_ids": ["img-a"]})
    api.storyboard_delete(board["id"])

    lib = json.loads((library / "library.json").read_text(encoding="utf-8"))
    assert [i["id"] for i in lib["items"]] == ["img-a"]
    assert (library / "media" / "a.png").exists()
