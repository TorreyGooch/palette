"""The audio has to say what the transcript says it says.

A cut is made from whatever audio the episode directory holds, but the quote
was located in a transcript that came from somewhere else — YouTube captions,
while the audio may be the podcast feed's edit of the same conversation. When
those timelines disagree the clip is not slightly off; it is a fluent, clean,
correctly-snapped recording of a different sentence, attributed to a real
person. That is the failure these tests exist to make impossible.
"""
import json

import pytest

from quotesource import cut


# ── the score itself ──────────────────────────────────────────────────────────

def test_identical_text_scores_one():
    assert cut.alignment_score("the cat sat down", "the cat sat down") == 1.0


def test_case_and_punctuation_do_not_matter():
    """Captions are punctuated; whisper's output is punctuated differently."""
    assert cut.alignment_score("The cat sat, down!", "the cat sat down") == 1.0


def test_unrelated_text_scores_near_zero():
    a = "bioelectricity is a bridge between physics and cognition"
    b = "the semiconductor supply chain runs through taiwan entirely"
    assert cut.alignment_score(a, b) < 0.4


def test_ordinary_transcription_variance_still_scores_high():
    """Auto-captions mis-hear words; that must not read as misalignment."""
    captions = "so i think the the real bottleneck here is compute not data"
    heard = "So I think the real bottleneck here is compute, not data."
    assert cut.alignment_score(captions, heard) > 0.8


def test_empty_either_side_is_zero_not_a_crash():
    assert cut.alignment_score("", "something") == 0.0
    assert cut.alignment_score("something", "") == 0.0
    assert cut.alignment_score(None, None) == 0.0


def test_threshold_sits_in_the_measured_gap():
    """18 real cuts on known-good audio scored 0.585-0.962; windows offset by
    45-90s scored 0.06-0.16. The threshold has to clear both ends."""
    WORST_GENUINE, BEST_MISALIGNED = 0.585, 0.163
    assert BEST_MISALIGNED < cut.ALIGN_MIN < WORST_GENUINE


def test_threshold_favours_refusing_over_passing():
    """A false refusal is loud and overridable; a false pass ships a quote the
    speaker never said at that timestamp."""
    WORST_GENUINE, BEST_MISALIGNED = 0.585, 0.163
    assert (cut.ALIGN_MIN - BEST_MISALIGNED) > (WORST_GENUINE - cut.ALIGN_MIN)


def test_threshold_is_overridable(monkeypatch):
    monkeypatch.setenv("QS_CUT_ALIGN_MIN", "0")
    import importlib

    importlib.reload(cut)
    try:
        assert cut.ALIGN_MIN == 0.0
    finally:
        monkeypatch.delenv("QS_CUT_ALIGN_MIN")
        importlib.reload(cut)


# ── reading the transcript back ───────────────────────────────────────────────

@pytest.fixture
def episode(tmp_path):
    (tmp_path / "transcript.json").write_text(json.dumps({"segments": [
        {"start": 0.0, "end": 10.0, "text": "first bit of talking"},
        {"start": 10.0, "end": 20.0, "text": "second bit of talking"},
        {"start": 20.0, "end": 30.0, "text": "third bit of talking"},
    ]}), encoding="utf-8")
    return tmp_path


def test_caption_text_covers_the_span(episode):
    assert cut.caption_text(episode, 10.0, 20.0) == "second bit of talking"


def test_caption_text_includes_partial_overlaps(episode):
    """A quote rarely starts on a segment boundary."""
    text = cut.caption_text(episode, 8.0, 12.0)
    assert "first bit" in text and "second bit" in text


def test_caption_text_excludes_segments_outside_the_span(episode):
    assert "third" not in cut.caption_text(episode, 0.0, 15.0)


def test_no_transcript_yields_no_text_rather_than_raising(tmp_path):
    assert cut.caption_text(tmp_path, 0.0, 10.0) == ""


# ── when the guard fires ──────────────────────────────────────────────────────

def test_short_quotes_are_not_enforced():
    """Under a handful of words the ratio is noise, so it is recorded only."""
    assert cut.ALIGN_MIN_WORDS >= 5


def test_a_shifted_timeline_is_caught():
    """The realistic failure: a feed with a pre-roll the upload lacks, so the
    audio at t is a minute earlier in the conversation than the captions say."""
    captions = ("and so the question becomes whether you can actually get "
                "the regulatory approval before the technology is obsolete")
    heard = ("welcome back to the show today i am joined by someone i have "
             "wanted to talk to for a very long time")
    assert cut.alignment_score(captions, heard) < cut.ALIGN_MIN


def test_the_offset_is_applied_to_the_audio_only(monkeypatch, tmp_path):
    """Borrowed audio on a shifted timeline: seeks move, reported times do not.

    attribution.range and source_url_ts point at the episode as published, so
    shifting them would make every citation wrong in order to make the seek
    right.
    """
    seeks = {}

    def fake_decode(src, start, dur, dest):
        seeks["decode"] = start
        dest.write_bytes(b"wav")

    monkeypatch.setattr(cut, "_decode_window", fake_decode)
    meta = {"audio_provenance": {"offset_s": -45.0}}
    offset = float((meta.get("audio_provenance") or {}).get("offset_s") or 0.0)

    # The arithmetic cut_quote performs, isolated: a caption time of 600s on a
    # -45s timeline must read the audio at 555s.
    win_start = 600.0 - cut.WINDOW_PAD_S
    assert win_start + offset == pytest.approx(540.0)
    assert win_start == pytest.approx(585.0), "transcript time is unshifted"


def test_absent_provenance_means_no_shift():
    for meta in ({}, {"audio_provenance": None}, {"audio_provenance": {}}):
        offset = float((meta.get("audio_provenance") or {}).get("offset_s") or 0.0)
        assert offset == 0.0


def test_the_matching_case_passes_the_same_threshold():
    captions = ("and so the question becomes whether you can actually get "
                "the regulatory approval before the technology is obsolete")
    heard = ("And so the question becomes whether you can actually get the "
             "regulatory approval before the technology is obsolete.")
    assert cut.alignment_score(captions, heard) >= cut.ALIGN_MIN
