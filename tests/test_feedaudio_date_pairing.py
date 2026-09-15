"""Pairing feed audio when the two titles disagree.

Dwarkesh words his YouTube and podcast titles differently for the same
conversation, so a title-first matcher left 10 of 19 unlinked episodes
unpaired while their audio sat on disk - and every cut from them fell back to
a 50 MB YouTube download that spends the request budget and may be refused.

The fallback is deliberately narrow: same upload day, duration within
tolerance, and exactly one candidate *in each direction*. A wrong pairing is
a fluent clip of the wrong sentence in the right voice, so every way of being
unsure is refused and reported rather than guessed through.

The titles below are real. Invented ones turned out too alike - they scored
over the title matcher's 0.60 and quietly tested the wrong path - so the first
test pins that they genuinely do not match on title.
"""
import json

import pytest

from quotesource import feedaudio
from tests.test_feedaudio import corpus  # noqa: F401


def episode(corpus, source, ep_id, *, title, duration=8433.0,
            upload_date="20260901", audio=False, provenance=None):
    d = corpus / "episodes" / source / ep_id
    d.mkdir(parents=True)
    meta = {"episode_id": ep_id, "title": title, "duration": duration}
    if upload_date is not None:
        meta["upload_date"] = upload_date
    if provenance:
        meta["audio_provenance"] = provenance
    (d / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")
    if audio:
        (d / "audio.mp3").write_bytes(b"feed audio")
    return d


def provenance(corpus, ep_id, source="yt"):
    path = corpus / "episodes" / source / ep_id / "metadata.json"
    return json.loads(path.read_text(encoding="utf-8")).get("audio_provenance")


# One real conversation, titled twice.
YT_TITLE = 'Ajeya Cotra – "This might be the clearest warning shot we get"'
FEED_TITLE = "Inside the OpenAI agent swarm that hacked its way out of a sandbox"
OTHER_TITLE = "Semiconductor export controls and the next decade of compute"


def test_the_fixture_titles_really_do_not_match_on_title(corpus):
    """Without this, every test below could pass through the title path."""
    feed = [(None, {"title": FEED_TITLE}), (None, {"title": OTHER_TITLE})]

    assert feedaudio.match_by_title(YT_TITLE, feed)[0] is None
    assert feedaudio.match_by_title(OTHER_TITLE, [(None, {"title": FEED_TITLE})])[0] is None


# -- what it pairs ------------------------------------------------------------

def test_differently_titled_same_conversation_is_linked(corpus):
    episode(corpus, "yt", "X50zezLFWWI", title=YT_TITLE)
    episode(corpus, "feed", "rss-806c", title=FEED_TITLE, audio=True)

    result = feedaudio.link_matching("yt", "feed", apply=True)

    assert result["linked"] == 1
    assert feedaudio.stored_audio(corpus / "episodes" / "yt" / "X50zezLFWWI")


def test_a_date_and_duration_pair_says_so_in_its_provenance(corpus):
    """Weaker evidence than a title, and a later reader must be able to tell."""
    episode(corpus, "yt", "v1", title=YT_TITLE)
    episode(corpus, "feed", "r1", title=FEED_TITLE, audio=True)

    feedaudio.link_matching("yt", "feed", apply=True)

    prov = provenance(corpus, "v1")
    assert prov["matched_on"] == "date_duration"
    assert prov["linked_from"] == "feed/r1"
    assert prov["alignment"] == "duration_exact"


def test_a_title_pair_says_so_too(corpus):
    episode(corpus, "yt", "v1", title="Terence Tao on proof")
    episode(corpus, "feed", "r1", title="Terence Tao on proof", audio=True)

    feedaudio.link_matching("yt", "feed", apply=True)

    assert provenance(corpus, "v1")["matched_on"] == "title"


def test_one_second_of_rounding_still_pairs(corpus):
    episode(corpus, "yt", "v1", title=YT_TITLE, duration=8433.0)
    episode(corpus, "feed", "r1", title=FEED_TITLE, duration=8434.0, audio=True)

    assert feedaudio.link_matching("yt", "feed", apply=True)["linked"] == 1


def test_the_dry_run_shows_both_titles_before_anything_is_applied(corpus):
    """The weaker pairs are the ones a person should read first."""
    episode(corpus, "yt", "v1", title=YT_TITLE)
    episode(corpus, "feed", "r1", title=FEED_TITLE, audio=True)

    result = feedaudio.link_matching("yt", "feed", apply=False)

    assert result["linked"] == 0 and result["linkable"] == 1
    assert result["matched_on"] == {"title": 0, "date_duration": 1}
    [pair] = result["date_duration_pairs"]
    assert pair["episode_id"] == "v1" and pair["feed_episode"] == "r1"
    assert pair["caption_title"] == YT_TITLE and pair["feed_title"] == FEED_TITLE
    assert feedaudio.stored_audio(corpus / "episodes" / "yt" / "v1") is None


# -- a title that found the wrong conversation --------------------------------

# Exact titles from dwarkesh_yt and the feed. The 2026 upload title-matches the
# 2023 appearance at 0.647; the real pair is the same-day feed episode.
SANDERSON_YT = ("Grant Sanderson (@3blue1brown) – AI disproved a famous math "
                "conjecture. Now what?")
SANDERSON_2023 = ("Grant Sanderson (@3blue1brown) — Past, present, & future "
                  "of mathematics")
SANDERSON_2026 = "Grant Sanderson – AI and the future of math"


def test_a_title_match_to_an_earlier_appearance_gives_way(corpus):
    episode(corpus, "yt", "TfyPshgMbug", title=SANDERSON_YT, duration=5619.0,
            upload_date="20260630")
    episode(corpus, "feed", "rss-c9d2", title=SANDERSON_2023, duration=5480.0,
            upload_date="20231012", audio=True)
    episode(corpus, "feed", "rss-e0da", title=SANDERSON_2026, duration=5619.0,
            upload_date="20260630", audio=True)
    feed = [(None, {"title": SANDERSON_2023}), (None, {"title": SANDERSON_2026})]
    assert feedaudio.match_by_title(SANDERSON_YT, feed)[0][1]["title"] == \
        SANDERSON_2023, "the premise: the title matcher really picks 2023"

    result = feedaudio.link_matching("yt", "feed", apply=True)

    assert result["linked"] == 1 and result["differs"] == 0
    prov = provenance(corpus, "TfyPshgMbug")
    assert prov["linked_from"] == "feed/rss-e0da"
    assert prov["matched_on"] == "date_duration"


def test_a_same_day_title_match_with_a_pre_roll_stays_for_the_probe(corpus):
    """Same title, same day, longer feed: the shape of a real pre-roll. That
    is the offset probe's to measure, not the fallback's to reassign."""
    episode(corpus, "yt", "v1", title="Terence Tao on proof", duration=5024.0,
            upload_date="20260320")
    episode(corpus, "feed", "r1", title="Terence Tao on proof", duration=5077.0,
            upload_date="20260320", audio=True)
    episode(corpus, "feed", "r2", title=OTHER_TITLE, duration=5024.0,
            upload_date="20260320", audio=True)

    result = feedaudio.link_matching("yt", "feed", apply=True)

    assert result["linked"] == 0 and result["differs"] == 1


# -- what it refuses ----------------------------------------------------------

def test_a_different_day_is_not_the_same_conversation(corpus):
    """The one real case: duration matched to the second, dates far apart."""
    episode(corpus, "yt", "v1", title=YT_TITLE, upload_date="20260508")
    episode(corpus, "feed", "r1", title=FEED_TITLE, upload_date="20240110",
            audio=True)

    result = feedaudio.link_matching("yt", "feed", apply=True)

    assert result["linked"] == 0 and result["unmatched"] == 1


def test_a_different_length_is_left_alone(corpus):
    episode(corpus, "yt", "v1", title=YT_TITLE, duration=8433.0)
    episode(corpus, "feed", "r1", title=FEED_TITLE, duration=8486.0, audio=True)

    assert feedaudio.link_matching("yt", "feed", apply=True)["linked"] == 0


@pytest.mark.parametrize("side", ["yt", "feed"])
def test_an_unknown_day_pairs_nothing(corpus, side):
    """Nothing to confirm the guess with, so no guess."""
    episode(corpus, "yt", "v1", title=YT_TITLE,
            upload_date=None if side == "yt" else "20260901")
    episode(corpus, "feed", "r1", title=FEED_TITLE, audio=True,
            upload_date=None if side == "feed" else "20260901")

    assert feedaudio.link_matching("yt", "feed", apply=True)["linked"] == 0


def test_two_candidate_feed_episodes_are_ambiguous(corpus):
    episode(corpus, "yt", "v1", title=YT_TITLE)
    episode(corpus, "feed", "r1", title=FEED_TITLE, audio=True)
    episode(corpus, "feed", "r2", title=OTHER_TITLE, audio=True)

    result = feedaudio.link_matching("yt", "feed", apply=True)

    assert result["linked"] == 0
    assert result["ambiguous"] == 1, "reported as ambiguous, not as unmatched"
    assert result["unmatched"] == 0


def test_two_uploads_claiming_one_feed_episode_are_both_refused(corpus):
    """Unique in the other direction: one feed episode cannot be both."""
    episode(corpus, "yt", "v1", title=YT_TITLE)
    episode(corpus, "yt", "v2", title=OTHER_TITLE)
    episode(corpus, "feed", "r1", title=FEED_TITLE, audio=True)

    result = feedaudio.link_matching("yt", "feed", apply=True)

    assert result["linked"] == 0 and result["ambiguous"] == 2
    assert provenance(corpus, "v1") is None and provenance(corpus, "v2") is None


def test_feed_audio_already_lent_is_not_lent_again(corpus):
    episode(corpus, "yt", "v0", title="An earlier pairing",
            provenance={"linked_from": "feed/r1", "offset_s": 0.0,
                        "alignment": "duration_exact"})
    episode(corpus, "yt", "v1", title=YT_TITLE)
    episode(corpus, "feed", "r1", title=FEED_TITLE, audio=True)

    result = feedaudio.link_matching("yt", "feed", apply=True)

    assert result["linked"] == 0 and result["ambiguous"] == 1


def test_audio_claimed_by_a_title_match_in_the_same_run_is_spoken_for(corpus):
    episode(corpus, "yt", "v1", title=FEED_TITLE)            # title matches r1
    episode(corpus, "yt", "v2", title=YT_TITLE)               # date+duration only
    episode(corpus, "feed", "r1", title=FEED_TITLE, audio=True)

    result = feedaudio.link_matching("yt", "feed", apply=True)

    assert result["linked"] == 1
    assert provenance(corpus, "v1")["matched_on"] == "title"
    assert provenance(corpus, "v2") is None


def test_disagreeing_series_numbers_are_never_paired_by_date(corpus):
    """The title matcher refuses these; the fallback must not undo that."""
    episode(corpus, "yt", "v1", title="Platonic Space discussion 2")
    episode(corpus, "feed", "r1", title="Platonic Space discussion 4", audio=True)

    assert feedaudio.link_matching("yt", "feed", apply=True)["linked"] == 0


def test_a_title_match_is_still_preferred(corpus):
    """The fallback only runs where no title matched."""
    episode(corpus, "yt", "v1", title="Terence Tao on proof")
    episode(corpus, "feed", "r1", title="Terence Tao on proof", audio=True)
    episode(corpus, "feed", "r2", title=OTHER_TITLE, audio=True)

    result = feedaudio.link_matching("yt", "feed", apply=True)

    assert result["linked"] == 1
    assert provenance(corpus, "v1")["linked_from"] == "feed/r1"
