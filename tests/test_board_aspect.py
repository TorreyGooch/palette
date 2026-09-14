"""A board has a frame shape, and everything that draws a frame uses it.

The page drew every panel as 16:9 regardless of what the piece was, and the
render dialog's aspect was a per-render choice that reset on every load. For
a vertical video that meant references letterboxed into sideways boxes while
working, and a 16:9 export unless someone remembered to change a dropdown.

So the shape belongs to the board: stored once, read by the page and by the
render. It is additive - a board saved before it existed has no `aspect` and
opens as 16:9, exactly as it rendered before.
"""
import json

import pytest
from fastapi import HTTPException

from tests.test_storyboard import api  # noqa: F401


def a_board(api):
    return api.storyboard_create(body={"name": "Vertical piece"})["id"]


def test_a_board_carries_its_aspect(api):
    bid = a_board(api)

    saved = api.storyboard_update(bid, body={"aspect": 0.5625})

    assert saved["aspect"] == pytest.approx(0.5625)
    assert api.storyboard_get(bid)["aspect"] == pytest.approx(0.5625)


def test_a_new_board_reads_as_widescreen(api):
    from palette_app.storyboard import DEFAULT_ASPECT

    assert api.storyboard_get(a_board(api))["aspect"] == \
        pytest.approx(DEFAULT_ASPECT)


def test_a_board_saved_before_the_field_existed_still_opens(api, library):
    """Additive: nothing to migrate, but prove it rather than assume it."""
    from palette_app.storyboard import DEFAULT_ASPECT, board_path

    bid = a_board(api)
    path = board_path(library, bid)
    stored = json.loads(path.read_text(encoding="utf-8"))
    stored.pop("aspect", None)
    path.write_text(json.dumps(stored), encoding="utf-8")

    assert api.storyboard_get(bid)["aspect"] == pytest.approx(DEFAULT_ASPECT)


def test_clearing_it_goes_back_to_the_default(api):
    from palette_app.storyboard import DEFAULT_ASPECT

    bid = a_board(api)
    api.storyboard_update(bid, body={"aspect": 0.75})

    saved = api.storyboard_update(bid, body={"aspect": None})

    assert saved["aspect"] == pytest.approx(DEFAULT_ASPECT)


@pytest.mark.parametrize("bad", [0, -1, "wide", 20, 0.01])
def test_a_shape_no_frame_could_have_is_refused(api, bad):
    bid = a_board(api)

    with pytest.raises(HTTPException) as raised:
        api.storyboard_update(bid, body={"aspect": bad})

    assert raised.value.status_code == 400


def test_other_edits_leave_the_aspect_alone(api):
    bid = a_board(api)
    api.storyboard_update(bid, body={"aspect": 0.75})

    saved = api.storyboard_update(bid, body={"name": "Renamed",
                                             "panels": [{"image_prompt": "x"}]})

    assert saved["aspect"] == pytest.approx(0.75)


# -- the render follows the board ---------------------------------------------

@pytest.fixture
def captured_render(api, monkeypatch):
    seen = {}

    def fake(panels, out_path, **kw):
        seen.update(kw)
        return {"ok": True, "panels": len(panels), "grid": "1x1",
                "width": 1, "height": 1, "size_bytes": 1, "missing": []}

    monkeypatch.setattr(api, "render_storyboard", fake)
    return seen


def test_a_render_uses_the_boards_aspect(api, captured_render):
    bid = a_board(api)
    api.storyboard_update(bid, body={"aspect": 0.75,
                                     "panels": [{"image_prompt": "a cell"}]})

    api.storyboard_render(bid, body={})

    assert captured_render["aspect"] == pytest.approx(0.75)


def test_an_aspect_asked_for_at_render_time_still_wins(api, captured_render):
    """A one-off 16:9 export of a vertical board is a legitimate request."""
    bid = a_board(api)
    api.storyboard_update(bid, body={"aspect": 0.75,
                                     "panels": [{"image_prompt": "a cell"}]})

    api.storyboard_render(bid, body={"aspect": 1.7777777778})

    assert captured_render["aspect"] == pytest.approx(1.7777777778)
