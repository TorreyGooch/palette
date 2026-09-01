"""The view a cut is chosen from has to be the view the cut takes.

`word_map` exists so a boundary is picked from real word timings rather than
from caption timestamps. It is therefore not an independent report: it is a
preview of `cut`, and where the two disagree the preview is wrong by
definition — the manifest that gets stored comes from the cut's pass.

They disagreed in two ways, both silent.

  the offset   `cut` applies `audio_provenance.offset_s` wherever it touches
               the audio, because borrowed feed audio can sit on a different
               timeline from the transcript that located the quote.
               `word_map` did not, so on the 55 episodes carrying a measured
               offset (up to -61s) it read a different passage than the cut
               would take. The alignment guard cannot catch this: the *cut* is
               correctly offset and passes. It is the view used to choose the
               cut that was wrong.

  the window   whisper is stable for a given window and disagrees between
               window widths — observed inserting a word a wider pass does
               not have, which shifts every index after it. `word_map`
               defaulted to a narrower window than the cut's, so a beat could
               be wrong at birth rather than only after a recut.
"""
import json

import pytest


@pytest.fixture
def episode(tmp_path, monkeypatch):
    """An episode whose decode calls are recorded rather than performed."""
    from quotesource import cut

    ep = tmp_path / "ep"
    ep.mkdir()
    seeks = []

    monkeypatch.setattr(cut, "_find_episode_dir", lambda _: ep)
    monkeypatch.setattr(cut, "_source_media", lambda *a: ep / "audio.m4a")
    monkeypatch.setattr(cut, "_decode_window",
                        lambda src, start, dur, dest: seeks.append((start, dur)))
    monkeypatch.setattr(cut, "window_words", lambda *a, **k: [
        {"word": "special.", "start": 0.10, "end": 0.40},
        {"word": "I", "start": 1.10, "end": 1.20},
    ])

    def write_meta(**provenance):
        meta = {"episode_id": "EP1", "source_id": "dwarkesh_yt"}
        if provenance:
            meta["audio_provenance"] = provenance
        (ep / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")

    write_meta()
    return {"dir": ep, "seeks": seeks, "meta": write_meta}


# -- the offset ---------------------------------------------------------------

def test_a_measured_offset_moves_where_the_audio_is_read(episode):
    """The bug: 55 real episodes carry one, the largest -61s."""
    from quotesource.cut import word_map

    episode["meta"](linked_from="dwarkesh/rss-x", offset_s=-61.0,
                    alignment="probed_constant")
    word_map("EP1", 500.0, 510.0, pad=15.0)

    seek, _ = episode["seeks"][0]
    assert seek == pytest.approx(485.0 - 61.0), "seek must carry the offset"


def test_an_episode_without_linked_audio_is_unmoved(episode):
    from quotesource.cut import word_map

    word_map("EP1", 500.0, 510.0, pad=15.0)

    seek, _ = episode["seeks"][0]
    assert seek == pytest.approx(485.0)


def test_reported_times_stay_in_transcript_time(episode):
    """The offset applies to the audio only.

    Everything reported has to keep pointing at the episode as published, or
    the range handed to `cut` would be offset a second time.
    """
    from quotesource.cut import word_map

    episode["meta"](linked_from="dwarkesh/rss-x", offset_s=-61.0,
                    alignment="probed_constant")
    out = word_map("EP1", 500.0, 510.0, pad=15.0)

    assert out["words"][0]["start"] == pytest.approx(485.10)
    assert out["window"][0] == pytest.approx(485.0)
    assert out["audio_offset_s"] == pytest.approx(-61.0)


def test_the_seek_matches_what_cut_would_use(episode):
    """Stated as the invariant rather than as a number.

    cut decodes at `max(0, start - WINDOW_PAD_S) + offset`. If that expression
    ever changes, this fails and says the preview drifted from the cut.
    """
    from quotesource import cut

    episode["meta"](linked_from="dwarkesh/rss-x", offset_s=-45.5,
                    alignment="probed_constant")
    cut.word_map("EP1", 500.0, 510.0)

    seek, duration = episode["seeks"][0]
    expected_start = max(0.0, 500.0 - cut.WINDOW_PAD_S)
    assert seek == pytest.approx(expected_start + -45.5)
    assert duration == pytest.approx((510.0 + cut.WINDOW_PAD_S) - expected_start)


def test_a_corrupt_metadata_file_does_not_stop_the_view(episode):
    """No offset is the honest fallback; refusing would be worse."""
    from quotesource.cut import word_map

    (episode["dir"] / "metadata.json").write_text("{ not json", encoding="utf-8")

    assert word_map("EP1", 500.0, 510.0)["audio_offset_s"] == 0.0


# -- the window ---------------------------------------------------------------

def test_the_default_window_is_the_cut_s_own(episode):
    """So what you choose from is what the manifest will be built from."""
    from quotesource import cut

    out = cut.word_map("EP1", 500.0, 510.0)

    assert out["window_pad_s"] == pytest.approx(cut.WINDOW_PAD_S)
    assert out["matches_cut_window"] is True
    seek, _ = episode["seeks"][0]
    assert seek == pytest.approx(500.0 - cut.WINDOW_PAD_S)


def test_a_narrower_window_is_allowed_but_says_it_will_not_match(episode):
    """A cheaper look is legitimate; silently differing from the cut is not."""
    from quotesource.cut import word_map

    out = word_map("EP1", 500.0, 510.0, pad=3.0)

    assert out["window_pad_s"] == pytest.approx(3.0)
    assert out["matches_cut_window"] is False


def test_the_view_reports_the_threshold_its_pauses_were_filtered_at(episode):
    """An empty pause list and an empty result look identical otherwise.

    A real session read "nothing printed" as "no pause data", on a speaker
    whose gaps run 120-180ms, and spent a second whisper call rediscovering
    numbers it already had. Reporting the filter distinguishes "no pauses" from
    "none above this".
    """
    from quotesource.cut import word_map

    out = word_map("EP1", 500.0, 510.0)

    assert out["min_gap"] == 0.15
    # The canned words hold one 0.70s gap, so this view has a pause and also
    # says what would have hidden it.
    assert [p["gap"] for p in out["pauses"]] == [pytest.approx(0.70)]
