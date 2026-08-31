"""Whether a transcript's wording is worth trusting.

`transcript_source: manual` looks like a quality signal and is not one:
creators routinely upload an unedited auto-caption dump as a manual track. So
one Vervaeke episode reads "Plato is deeply influenced by the natural
philosophers" and another reads "my my contention and what i'm going to argue
is it's no coincidence", and the stored field says the same thing about both.

What separates them is punctuation and capitals, which an auto-caption stream
has neither of. This is a prompt to verify, never a verdict: `raw` means read
the context before quoting, and `clean` does not excuse skipping it.
"""
import pytest

from quotesource.indexer import (_ensure_schema, _transcript_text,
                                 build_index, caption_quality, connect)

CLEAN = (
    "Plato is deeply influenced by the natural philosophers. He takes their "
    "argument seriously, and then he turns it. What matters here is not the "
    "conclusion but the move itself, because the move is what we inherited. "
    "Socrates would have put it differently, of course."
)
RAW = (
    "my my contention and what i'm going to argue is it's no coincidence "
    "that we're seeing this now and the reason i think that is because the "
    "the machinery that produces meaning is the same machinery that produces "
    "everything else we care about and that's what i want to talk about today"
)


def test_written_prose_reads_as_clean():
    assert caption_quality(CLEAN) == "clean"


def test_an_auto_caption_dump_reads_as_raw():
    assert caption_quality(RAW) == "raw"


def test_the_two_extremes_can_share_one_source():
    """Which is the whole reason per-source guidance is not enough."""
    assert caption_quality(CLEAN) != caption_quality(RAW)


def test_punctuation_without_capitals_is_still_raw():
    """Two ratios, not one: lowercase prose with commas is not quotable."""
    text = " ".join(["the machinery that produces meaning is the same."] * 6)
    assert caption_quality(text) == "raw"


def test_capitals_without_punctuation_are_still_raw():
    text = " ".join(["The Machinery That Produces Meaning Is The Same"] * 6)
    assert caption_quality(text) == "raw"


@pytest.mark.parametrize("text", ["", "   ", "a few words only", None])
def test_too_little_text_is_unknown_rather_than_guessed(text):
    """Crying `raw` on every stub would train people to ignore the field."""
    assert caption_quality(text) == "unknown"


def test_transcript_text_joins_the_segments():
    assert _transcript_text({"segments": [{"text": "one"}, {"text": "two"}]}) \
        == "one two"
    assert _transcript_text({}) == ""
    assert _transcript_text(None) == ""


# -- the stored column --------------------------------------------------------

@pytest.fixture
def corpus(tmp_path, monkeypatch):
    monkeypatch.setenv("QUOTESOURCE_DATA", str(tmp_path))
    return tmp_path


def episode(corpus, episode_id, text, source="src"):
    import json

    ep_dir = corpus / "episodes" / source / episode_id
    ep_dir.mkdir(parents=True, exist_ok=True)
    (ep_dir / "metadata.json").write_text(json.dumps(
        {"episode_id": episode_id, "title": "T", "upload_date": "20260101",
         "url": f"https://y/{episode_id}", "duration": 600}), encoding="utf-8")
    (ep_dir / "transcript.json").write_text(json.dumps(
        {"transcript_source": "manual",
         "segments": [{"start": 0.0, "end": 30.0, "text": text}]}),
        encoding="utf-8")
    return ep_dir


def quality_of(episode_id):
    con = connect()
    row = con.execute("SELECT caption_quality FROM episodes WHERE episode_id=?",
                      (episode_id,)).fetchone()
    con.close()
    return row[0] if row else None


def test_indexing_records_the_quality(corpus):
    episode(corpus, "CLEANEP", CLEAN)
    episode(corpus, "RAWEP", RAW)

    build_index()

    assert quality_of("CLEANEP") == "clean"
    assert quality_of("RAWEP") == "raw"


def test_both_are_recorded_as_manual_which_is_the_point(corpus):
    episode(corpus, "CLEANEP", CLEAN)
    episode(corpus, "RAWEP", RAW)
    build_index()

    con = connect()
    sources = {row[0] for row in
               con.execute("SELECT transcript_source FROM episodes")}
    con.close()
    assert sources == {"manual"}, "the stored field cannot tell them apart"


def test_the_column_is_added_to_an_index_that_predates_it(corpus):
    """Every existing index is one of these; none of them may break."""
    con = connect()
    _ensure_schema(con)
    con.execute("ALTER TABLE episodes DROP COLUMN caption_quality")
    con.commit()
    columns = {r[1] for r in con.execute("PRAGMA table_info(episodes)")}
    assert "caption_quality" not in columns
    con.close()

    con = connect()
    _ensure_schema(con)
    columns = {r[1] for r in con.execute("PRAGMA table_info(episodes)")}
    con.close()
    assert "caption_quality" in columns


def test_an_older_episode_is_rated_without_being_rechunked(corpus):
    """Re-indexing to fill this in would strand every vector already computed.

    New chunk ids mean new embeddings, which is hours of GPU time to answer a
    question the transcript on disk answers for free.
    """
    episode(corpus, "RAWEP", RAW)
    build_index()

    con = connect()
    before = [r[0] for r in con.execute(
        "SELECT id FROM chunks WHERE episode_id='RAWEP' ORDER BY id")]
    con.execute("UPDATE episodes SET caption_quality=NULL")
    con.commit()
    con.close()

    stats = build_index()

    con = connect()
    after = [r[0] for r in con.execute(
        "SELECT id FROM chunks WHERE episode_id='RAWEP' ORDER BY id")]
    con.close()

    assert quality_of("RAWEP") == "raw", "the backfill ran"
    assert stats["rated"] == 1
    assert stats["indexed"] == 0, "and it did not re-index"
    assert after == before, "chunk ids survived, so the vectors did too"


def test_a_search_hit_carries_the_quality(corpus):
    from quotesource.search import grep

    episode(corpus, "RAWEP", RAW)
    build_index()

    hits = grep("machinery")
    assert hits, "the fixture text should match"
    assert hits[0]["caption_quality"] == "raw"
