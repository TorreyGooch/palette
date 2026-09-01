"""One conversation arriving under two source ids.

Search returns the same moment twice, under two attributions, and nothing says
they are one thing. Both roles hit this on the same day from opposite ends:
the Researcher recomputed a thoughtforms/levin_yt overlap by hand three times
in one session, and the Storyboarder got a Vervaeke hit from a second source
and could not tell whether it was material they already had.

It is derivable, which is why it is computed rather than recorded by hand —
but it is expensive enough that nobody does it, which is why it now happens at
index time, when the episode table is already open.

**The duration check is the part a naive version gets wrong.** Across
thoughtforms vs levin_yt, 108 pairs matched on title at 0.85; 102 agreed on
duration within 90s and 6 did not, and those 6 were genuinely different
material — one running 3247s on RSS against 1339s on YouTube, an excerpt
rather than the same talk. A title-only matcher throws real content away, and
that is the expensive direction: a missed duplicate costs some GPU, a false
one hides an episode nobody can then find.
"""
import sqlite3

import pytest

from quotesource.indexer import _ensure_schema, link_duplicates


@pytest.fixture
def con():
    connection = sqlite3.connect(":memory:")
    _ensure_schema(connection)
    return connection


def episode(con, episode_id, source_id, title, duration):
    con.execute(
        "INSERT INTO episodes (episode_id, source_id, title, duration) "
        "VALUES (?,?,?,?)", (episode_id, source_id, title, duration))
    con.commit()


def links(con) -> dict:
    return {eid: dup for eid, dup in con.execute(
        "SELECT episode_id, duplicate_of FROM episodes")}


TALK = "The Bioelectric Code and the Origins of Cognition"


# -- the case it exists for ---------------------------------------------------

def test_one_talk_under_two_sources_is_linked(con):
    episode(con, "aaa", "levin_yt", TALK, 3600)
    episode(con, "bbb", "thoughtforms", TALK, 3630)

    link_duplicates(con)

    assert links(con) == {"aaa": None, "bbb": "aaa"}


def test_the_canonical_is_the_lowest_id_so_it_is_stable_across_runs(con):
    """Otherwise the canonical depends on row order and churns every reindex."""
    episode(con, "zzz", "levin_yt", TALK, 3600)
    episode(con, "aaa", "thoughtforms", TALK, 3610)

    link_duplicates(con)

    assert links(con) == {"zzz": "aaa", "aaa": None}


def test_a_third_copy_joins_the_same_group(con):
    episode(con, "aaa", "levin_yt", TALK, 3600)
    episode(con, "bbb", "thoughtforms", TALK, 3620)
    episode(con, "ccc", "toe", TALK, 3640)

    link_duplicates(con)

    assert links(con) == {"aaa": None, "bbb": "aaa", "ccc": "aaa"}


# -- what must NOT be called a duplicate --------------------------------------

def test_two_episodes_of_one_source_are_not_this_problem(con):
    """A channel posting clips beside full episodes is --min-duration's job."""
    episode(con, "aaa", "levin_yt", TALK, 3600)
    episode(con, "bbb", "levin_yt", TALK, 3610)

    link_duplicates(con)

    assert links(con) == {"aaa": None, "bbb": None}


def test_a_title_match_with_distant_durations_is_left_alone(con):
    """The measured case: 3247s on RSS against 1339s on YouTube.

    Same title, genuinely different material — an excerpt, not the talk. Six
    of 108 real pairs looked like this, and a title-only matcher would have
    discarded every one of them as duplicate.
    """
    episode(con, "aaa", "levin_yt", TALK, 1339)
    episode(con, "bbb", "thoughtforms", TALK, 3247)

    link_duplicates(con)

    assert links(con) == {"aaa": None, "bbb": None}


def test_a_near_miss_on_duration_is_still_refused(con):
    episode(con, "aaa", "levin_yt", TALK, 3600)
    episode(con, "bbb", "thoughtforms", TALK, 3600 + 91)

    link_duplicates(con)

    assert links(con)["bbb"] is None


def test_close_durations_with_unrelated_titles_are_not_linked(con):
    episode(con, "aaa", "levin_yt", TALK, 3600)
    episode(con, "bbb", "lexfridman", "Ivanka Trump on real estate", 3605)

    link_duplicates(con)

    assert links(con) == {"aaa": None, "bbb": None}


def test_a_series_number_is_never_crossed(con):
    """"Ep. 30" and "Ep. 33" are different recordings and one digit barely
    moves a similarity ratio. Already the rule for pairing feed audio."""
    episode(con, "aaa", "vervaeke_amc",
            "Ep. 30 - Awakening from the Meaning Crisis - Relevance", 3600)
    episode(con, "bbb", "guest_john_vervaeke",
            "Ep. 33 - Awakening from the Meaning Crisis - Relevance", 3610)

    link_duplicates(con)

    assert links(con) == {"aaa": None, "bbb": None}


def test_an_episode_of_unknown_duration_is_never_matched(con):
    """Duration is what confirms the guess, so without one there is nothing
    to confirm it with."""
    episode(con, "aaa", "levin_yt", TALK, 3600)
    episode(con, "bbb", "thoughtforms", TALK, None)

    link_duplicates(con)

    assert links(con) == {"aaa": None, "bbb": None}


# -- it is derived, so it is recomputed ---------------------------------------

def test_a_link_that_no_longer_holds_is_cleared(con):
    """Recomputed rather than updated: a stale link says two different talks
    are one, which is worse than saying nothing."""
    episode(con, "aaa", "levin_yt", TALK, 3600)
    episode(con, "bbb", "thoughtforms", TALK, 3620)
    link_duplicates(con)
    assert links(con)["bbb"] == "aaa"

    con.execute("UPDATE episodes SET title = ? WHERE episode_id = 'bbb'",
                ("Something else entirely about frogs",))
    con.commit()
    link_duplicates(con)

    assert links(con)["bbb"] is None


def test_a_later_episode_makes_a_duplicate_of_an_older_one(con):
    """Why the pass is global rather than per-episode: indexing one new
    episode can make a duplicate of something indexed months ago."""
    episode(con, "aaa", "levin_yt", TALK, 3600)
    link_duplicates(con)
    assert links(con) == {"aaa": None}

    episode(con, "bbb", "thoughtforms", TALK, 3620)
    link_duplicates(con)

    assert links(con)["bbb"] == "aaa"


def test_it_reports_how_many_it_found(con):
    episode(con, "aaa", "levin_yt", TALK, 3600)
    episode(con, "bbb", "thoughtforms", TALK, 3620)

    assert link_duplicates(con) == {"duplicates": 1, "groups": 1}


def test_the_column_survives_an_index_that_predates_it(con):
    """The migration is safe precisely because this database is derived."""
    columns = {row[1] for row in con.execute("PRAGMA table_info(episodes)")}
    assert "duplicate_of" in columns


# -- it has to reach the reader -----------------------------------------------

def test_a_hit_carries_the_link(tmp_path, monkeypatch):
    from quotesource import search

    monkeypatch.setenv("QUOTESOURCE_DATA", str(tmp_path))
    row = ("bbb", "thoughtforms", 1.0, 2.0, "text", 0.8, "T", "20240101",
           "https://y/watch?v=bbb", "clean", "aaa")

    assert search._hit(row)["duplicate_of"] == "aaa"


# -- null must not mean two things --------------------------------------------

def test_a_run_records_that_it_happened(con):
    """`duplicate_of: null` otherwise says both "checked, no twin" and "never
    checked", and a reader cannot tell which — which would be read as
    permission to cut the same moment twice under two attributions."""
    episode(con, "aaa", "levin_yt", TALK, 3600)

    link_duplicates(con)

    stamp = con.execute("SELECT value FROM index_meta "
                        "WHERE key = 'duplicates_linked_at'").fetchone()
    assert stamp and stamp[0]


def test_before_any_run_there_is_no_stamp(con):
    assert con.execute("SELECT value FROM index_meta "
                       "WHERE key = 'duplicates_linked_at'").fetchone() is None


def test_a_later_run_replaces_the_stamp_rather_than_adding_one(con):
    episode(con, "aaa", "levin_yt", TALK, 3600)
    link_duplicates(con)
    link_duplicates(con)

    rows = con.execute("SELECT value FROM index_meta "
                       "WHERE key = 'duplicates_linked_at'").fetchall()
    assert len(rows) == 1


def test_status_reports_whether_it_has_ever_run(tmp_path, monkeypatch):
    from quotesource import indexer
    from quotesource.status import corpus_status

    monkeypatch.setenv("QUOTESOURCE_DATA", str(tmp_path))
    (tmp_path / "episodes").mkdir(parents=True, exist_ok=True)
    connection = indexer.connect()
    indexer._ensure_schema(connection)
    connection.close()

    block = corpus_status()["index"]["duplicates"]
    assert block == {"linked_at": None, "episodes": 0}
