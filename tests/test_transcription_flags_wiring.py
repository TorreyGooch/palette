"""Where the transcription flags reach a person.

`quotesource/timing.py` decides what looks wrong; these tests pin that the
answer arrives where it is needed: recorded by the cut, returned by the words
endpoint (so a check does not mean reading a file off disk), and shown on the
board beat whose own words are affected.
"""
import json

import pytest

from tests.conftest import add_item
from tests.test_storyboard import api  # noqa: F401


def spoken(tokens, start=0.0, each=0.2, gap=0.05):
    out, t = [], start
    for word in tokens:
        out.append({"word": word, "start": round(t, 3), "end": round(t + each, 3)})
        t += each + gap
    return out


def looped(tokens, at, times):
    return [{"word": w, "start": at, "end": at} for _ in range(times) for w in tokens]


PHRASE = "but the intentional stance is that they reason about peers".split()


# -- the cut joins clip time to episode time correctly --------------------------

def test_local_agreement_reads_the_transcript_at_the_clips_place(tmp_path):
    """Clip words start at 0; the transcript is in episode time. Getting the
    join wrong compares every window against the wrong stretch of talk."""
    from quotesource.cut import local_agreement

    tokens = [f"word{i}" for i in range(40)]
    clip_words = spoken(tokens)
    (tmp_path / "transcript.json").write_text(json.dumps({"segments": [
        {"start": 100.0, "end": 100.0 + clip_words[-1]["end"] + 0.1,
         "text": " ".join(tokens)}]}), encoding="utf-8")

    right = local_agreement(tmp_path, 100.0, clip_words)
    wrong = local_agreement(tmp_path, 0.0, clip_words)

    assert right["min"] == 1.0 and right["flagged"] is False
    assert wrong["min"] == 0.0 and wrong["flagged"] is True


def test_a_short_clip_has_no_local_score(tmp_path):
    from quotesource.cut import local_agreement

    (tmp_path / "transcript.json").write_text('{"segments": []}', encoding="utf-8")
    assert local_agreement(tmp_path, 0.0, spoken(["only", "a", "few"])) is None


# -- the words endpoint ----------------------------------------------------------

def staged_clip(library, words, item_id="clip1", diagnostics=None):
    name = f"qs_cut_EP_{item_id}.m4a"
    add_item(library, name, item_id)
    manifest = {"duration": 30.0, "attribution": {"precision": "word_accurate"},
                "words": words,
                "cut_diagnostics": diagnostics or {"caption_alignment": 0.8169}}
    (library / "media" / name).with_suffix(".words.json").write_text(
        json.dumps(manifest), encoding="utf-8")
    return item_id


def test_the_words_endpoint_flags_a_looped_clip(api, library):
    words = spoken(PHRASE) + looped(PHRASE, 3.0, 3) + [
        {"word": "that��be", "start": 3.0, "end": 22.56}]
    iid = staged_clip(library, words)

    out = api.item_words(iid)

    assert out["transcription_flags"]["suspect"] is True
    assert out["transcription_flags"]["longest_word"]["seconds"] == pytest.approx(19.56)


def test_the_words_endpoint_passes_a_clean_clip(api, library):
    iid = staged_clip(library, spoken(PHRASE * 3))

    assert api.item_words(iid)["transcription_flags"]["suspect"] is False


def test_the_words_endpoint_returns_what_the_cut_recorded(api, library):
    """So an integrity check is an API call, not a file read."""
    local = {"min": 0.21, "words": [30, 59], "at": [7.5, 15.0], "flagged": True,
             "window_words": 30}
    iid = staged_clip(library, spoken(PHRASE),
                      diagnostics={"caption_alignment": 0.8169,
                                   "caption_alignment_local": local})

    diagnostics = api.item_words(iid)["cut_diagnostics"]

    assert diagnostics["caption_alignment"] == pytest.approx(0.8169)
    assert diagnostics["caption_alignment_local"] == local


def test_a_clip_cut_before_the_checks_existed_is_still_checked(api, library):
    """Flags are computed on read; nothing needs to have stored them."""
    words = spoken(PHRASE) + looped(PHRASE, 3.0, 2)
    iid = staged_clip(library, words, diagnostics={"caption_alignment": 0.9})

    out = api.item_words(iid)

    assert "caption_alignment_local" not in out["cut_diagnostics"]
    assert out["transcription_flags"]["suspect"] is True


# -- a board beat ------------------------------------------------------------------

def test_only_the_beat_over_the_loop_carries_the_flags(library):
    """A loop in the second half is not the first half's problem."""
    from palette_app import narration

    clean = spoken(PHRASE * 2)
    loop = looped(PHRASE, clean[-1]["end"], 3)
    words = clean + loop
    iid = staged_clip(library, words)
    item = {"id": iid, "filename": f"qs_cut_EP_{iid}.m4a"}

    first = narration.bind(library / "media", item, 0, len(clean) - 1)
    second = narration.bind(library / "media", item, len(clean), len(words) - 1)

    assert "transcription_flags" not in first
    assert second["transcription_flags"]["suspect"] is True


def test_a_board_view_carries_the_flag_to_the_page(api, library):
    words = spoken(PHRASE) + looped(PHRASE, 3.0, 3)
    iid = staged_clip(library, words)
    board = api.storyboard_create(body={"name": "Flags"})

    saved = api.storyboard_update(board["id"], body={"panels": [
        {"narration": {"item_id": iid}}]})

    flags = saved["panels"][0]["narration"]["transcription_flags"]
    assert flags["suspect"] is True and flags["reasons"]
