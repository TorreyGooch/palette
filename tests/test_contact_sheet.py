"""Paging a whole video into a series of contact sheets.

ffmpeg is stubbed: these tests are about where the grid breaks and what each
sheet claims about itself, not about decoding. The stub writes real JPEGs into
the same temp dir the real command would have, so Pillow does its actual work.
"""
import asyncio
import json
from pathlib import Path

import pytest

from palette_app.api import media


def _fake_ffmpeg(n_frames: int, size=(64, 36)):
    """Stand in for the extract pass, honouring the -vf select pattern's output."""
    async def run(cmd):
        from PIL import Image

        out_tmpl = Path(cmd[-1])
        for i in range(1, n_frames + 1):
            Image.new("RGB", size, (i * 3 % 256, 40, 80)).save(
                out_tmpl.with_name(out_tmpl.name.replace("%05d", f"{i:05d}"))
            )
        return 0, b"", b""

    return run


@pytest.fixture
def sheet(tmp_path, monkeypatch):
    """Render into tmp_path with a stubbed extractor; returns a callable."""
    def render(n_frames: int, **kw):
        monkeypatch.setattr(media, "_run", _fake_ffmpeg(n_frames))
        out = tmp_path / "clip_sheet_20260814.jpg"
        return asyncio.run(media.contact_sheet(tmp_path / "src.mp4", out, **kw)), out

    return render


# ── single sheet: unchanged ───────────────────────────────────────────────────

def test_without_rows_it_is_still_one_sheet(sheet):
    result, out = sheet(10, cols=4)

    assert result["ok"] is True
    assert result["sheet_count"] == 1
    assert out.exists(), "the caller's own path is used when there is no series"
    assert not list(out.parent.glob("*_p0*.jpg"))


def test_flat_keys_survive_for_older_callers(sheet):
    """grid/width/height were the whole contract before paging existed."""
    result, _ = sheet(10, cols=4)

    assert result["grid"] == "4x3"
    assert result["width"] > 0 and result["height"] > 0
    assert result["frames"] == 10


# ── the series ────────────────────────────────────────────────────────────────

def test_rows_splits_into_numbered_sheets(sheet):
    result, out = sheet(13, cols=3, rows=2)   # 6 per sheet → 6, 6, 1

    assert result["sheet_count"] == 3
    assert [s["frames"] for s in result["sheets"]] == [6, 6, 1]
    assert [s["filename"] for s in result["sheets"]] == [
        "clip_sheet_20260814_p001.jpg",
        "clip_sheet_20260814_p002.jpg",
        "clip_sheet_20260814_p003.jpg",
    ]
    assert all((out.parent / s["filename"]).exists() for s in result["sheets"])
    assert not out.exists(), "the unsuffixed name would sort into the middle of the series"


def test_pages_sort_lexically_past_nine(sheet):
    """_p10 next to _p9 would put sheet 10 before sheet 2 in every file browser."""
    result, _ = sheet(24, cols=2, rows=1)     # 2 per sheet → 12 sheets

    names = [s["filename"] for s in result["sheets"]]
    assert names == sorted(names)
    assert names[-1].endswith("_p012.jpg")


def test_last_sheet_is_not_padded_out(sheet):
    """A trailing sheet with one tile should be one row tall, not six."""
    result, _ = sheet(13, cols=3, rows=2)

    full, last = result["sheets"][0], result["sheets"][-1]
    assert full["grid"] == "3x2" and last["grid"] == "3x1"
    assert last["height"] < full["height"]


def test_paging_does_not_move_the_sampling(sheet):
    """Tile k must be the same source frame however the grid is broken up."""
    paged, _ = sheet(12, cols=2, rows=2, every_n=15, fps=30)
    single, _ = sheet(12, cols=2, every_n=15, fps=30)

    assert paged["sheets"][0]["first_frame"] == single["sheets"][0]["first_frame"]
    assert paged["sheets"][-1]["last_frame"] == single["sheets"][0]["last_frame"]


def test_sheets_report_contiguous_ranges(sheet):
    result, _ = sheet(12, cols=2, rows=2, every_n=15, fps=30)

    sheets = result["sheets"]
    assert sheets[0]["first_frame"] == 0
    for a, b in zip(sheets, sheets[1:]):
        assert b["first_frame"] == a["last_frame"] + 15
        assert b["start_time"] > a["end_time"]


# ── timecodes ─────────────────────────────────────────────────────────────────

def test_frame_numbers_are_absolute_not_segment_relative(sheet):
    """Sampling from 60s in must not label the first tile frame 0 — the point
    of a label is that you can seek to it in the source."""
    result, _ = sheet(4, cols=2, rows=2, every_n=30, fps=30, start=60.0)

    first = result["sheets"][0]
    assert first["first_frame"] == 1800
    assert first["start_time"] == pytest.approx(60.0)
    assert first["end_time"] == pytest.approx(63.0)


def test_no_fps_means_no_invented_timecodes(sheet):
    result, _ = sheet(4, cols=2, rows=2, every_n=30)

    first = result["sheets"][0]
    assert first["start_time"] is None and first["end_time"] is None
    assert first["first_frame"] == 0


def test_labels_render_without_fps(sheet):
    """The label path reads the timecode; None must not blow up the compose."""
    result, _ = sheet(4, cols=2, rows=2, labels=True)

    assert result["ok"] is True


@pytest.mark.parametrize("seconds,expected", [
    (0.0, "0:00.0"),
    (61.5, "1:01.5"),
    (599.9, "9:59.9"),
    (3600.0, "1:00:00.0"),
    (3725.4, "1:02:05.4"),
])
def test_timecode_formatting(seconds, expected):
    assert media.fmt_timecode(seconds) == expected


# ── failure ───────────────────────────────────────────────────────────────────

def test_no_frames_is_reported_not_crashed(tmp_path, monkeypatch):
    async def run(cmd):
        return 1, b"", b"moov atom not found"

    monkeypatch.setattr(media, "_run", run)
    result = asyncio.run(
        media.contact_sheet(tmp_path / "src.mp4", tmp_path / "out.jpg", rows=2)
    )
    assert result["ok"] is False
    assert "moov atom" in result["error"]


# ── where a series lands on disk ──────────────────────────────────────────────

@pytest.fixture
def render(library, monkeypatch):
    """POST /api/export/contact-sheet against a throwaway library."""
    from tests.conftest import add_item
    from palette_app import main

    item = add_item(library, "v.mp4", "vid1", type="video", fps=30.0, duration=12.0)
    monkeypatch.setattr(main, "_root", lambda: library)

    def go(n_frames=9, **body):
        monkeypatch.setattr(media, "_run", _fake_ffmpeg(n_frames))
        return asyncio.run(main.export_contact_sheet({"item_id": item["id"], **body}))

    return go


def test_a_series_gets_its_own_folder(render, library):
    """Forty loose files in exports/ can only be handed over as a glob."""
    result = render(9, cols=2, rows=2, every_n=30)

    folder = library / "exports" / result["dir"]
    assert folder.is_dir()
    assert result["dir"].startswith("v_sheet_"), "folder should name its source"
    assert sorted(p.name for p in folder.iterdir()) == [
        "index.json", "sheet_p001.jpg", "sheet_p002.jpg", "sheet_p003.jpg",
    ]
    assert not list((library / "exports").glob("*.jpg")), "nothing loose beside it"


def test_series_urls_carry_the_folder(render):
    result = render(9, cols=2, rows=2)

    assert all(u.startswith(result["dir"] + "/") for u in result["filenames"])
    assert [s["url"] for s in result["sheets"]] == result["filenames"]
    assert result["index"] == f"{result['dir']}/index.json"


def test_dir_path_is_absolute_enough_to_paste(render, library):
    result = render(9, cols=2, rows=2)

    assert Path(result["dir_path"]) == library / "exports" / result["dir"]


def test_index_names_sheets_relative_to_its_own_folder(render, library):
    """So the folder still makes sense after it is renamed or moved."""
    result = render(9, cols=2, rows=2, every_n=30)

    index = json.loads(
        (library / "exports" / result["index"]).read_text(encoding="utf-8"))
    assert index["source"] == "v.mp4"
    assert index["fps"] == 30.0
    assert index["sampled"]["every_n"] == 30
    assert len(index["sheets"]) == 3
    assert [s["filename"] for s in index["sheets"]] == [
        "sheet_p001.jpg", "sheet_p002.jpg", "sheet_p003.jpg",
    ]


def test_single_sheet_stays_loose_with_no_folder(render, library):
    """One file in a folder of its own is a box around nothing."""
    result = render(9, cols=2)

    assert result["dir"] is None and result["index"] is None
    assert len(result["filenames"]) == 1
    assert (library / "exports" / result["filename"]).is_file()
    assert not [p for p in (library / "exports").iterdir() if p.is_dir()]


# ── history listing ───────────────────────────────────────────────────────────

def test_a_series_is_one_row_of_history_not_forty(render, library, monkeypatch):
    from palette_app import main

    render(9, cols=2, rows=2)
    (library / "exports" / "plain.mp4").write_bytes(b"x" * 10)

    entries = main.list_exports()

    assert len(entries) == 2
    series = next(e for e in entries if e["kind"] == "series")
    assert series["sheets"] == 3
    assert series["first"].endswith("sheet_p001.jpg")
    assert series["index"].endswith("index.json")
    assert series["size_bytes"] > 0


def test_empty_folders_are_not_listed(render, library):
    from palette_app import main

    (library / "exports" / "stray").mkdir()
    assert main.list_exports() == []


def test_history_is_newest_first(render, library):
    import os

    from palette_app import main

    first = render(4, cols=2, rows=2)
    os.utime(library / "exports" / first["dir"], (1_000, 1_000))
    second = render(4, cols=2, rows=2, every_n=15)

    entries = main.list_exports()
    assert [e["filename"] for e in entries] == [second["dir"], first["dir"]]


# ── two renders in the same second ────────────────────────────────────────────

def test_a_second_render_does_not_land_in_the_first_folder(render, library):
    """The stamp is only second-resolution. Sharing a folder leaves the shorter
    run holding the longer one's sheets, with an index that lists neither set."""
    first = render(9, cols=2, rows=2)
    second = render(4, cols=2, rows=2, every_n=90)

    assert second["dir"] != first["dir"]
    folder = library / "exports" / second["dir"]
    sheets = sorted(p.name for p in folder.glob("*.jpg"))
    assert sheets == ["sheet_p001.jpg"], "no leftovers from the earlier render"
    index = json.loads((folder / "index.json").read_text(encoding="utf-8"))
    assert [s["filename"] for s in index["sheets"]] == sheets


def test_the_first_render_is_left_intact(render, library):
    first = render(9, cols=2, rows=2)
    render(4, cols=2, rows=2, every_n=90)

    folder = library / "exports" / first["dir"]
    assert sorted(p.name for p in folder.glob("*.jpg")) == [
        "sheet_p001.jpg", "sheet_p002.jpg", "sheet_p003.jpg",
    ]


# ── serving ───────────────────────────────────────────────────────────────────

def test_a_sheet_inside_a_series_is_servable(render, library, monkeypatch):
    from palette_app import main

    result = render(9, cols=2, rows=2)
    monkeypatch.setattr(main, "_root", lambda: library)

    response = main.serve_export(result["filenames"][0])
    assert Path(response.path).name == "sheet_p001.jpg"


@pytest.mark.parametrize("attack", [
    "../library.json",
    "../../secrets.txt",
    "sub/../../library.json",
])
def test_traversal_out_of_exports_is_refused(library, monkeypatch, attack):
    """Export paths gained a folder, so ".." now has somewhere to climb to."""
    from fastapi import HTTPException
    from palette_app import main

    monkeypatch.setattr(main, "_root", lambda: library)
    with pytest.raises(HTTPException) as e:
        main.serve_export(attack)
    assert e.value.status_code == 404
