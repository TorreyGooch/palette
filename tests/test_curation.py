"""Taking things back out, and noticing a thing added twice.

`qs guest add` could add an episode and nothing could remove one, so a
hand-curated source had no hand-curated undo: correcting a single wrong pick
meant dropping the whole source and re-adding everything else.

The case that forced it: two uploads of one 109-minute lecture under
different video ids. Nothing noticed at add time, and the copy that was kept
turned out to be the one with no captions.
"""
import json

import pytest

from quotesource import ingest, registry


@pytest.fixture
def corpus(monkeypatch, tmp_path):
    monkeypatch.setenv("QUOTESOURCE_DATA", str(tmp_path))
    return tmp_path


def guest_source(person="James A. Shapiro", sid="guest_james_a_shapiro"):
    return registry.add_source(sid, f"{person} (appearances)", "episodes", "",
                               people=[person])


def stage(source, episode_id, title="A Lecture", duration=6540.0,
          status="captions", extra_files=()):
    """An episode on disk, as a fetch would have left it."""
    ep_dir = ingest.episode_dir(source["id"], episode_id)
    ep_dir.mkdir(parents=True, exist_ok=True)
    ingest._write_metadata(ep_dir, {
        "episode_id": episode_id, "source_id": source["id"], "title": title,
        "duration": duration, "status": status,
        "url": f"https://www.youtube.com/watch?v={episode_id}"})
    for name in extra_files:
        (ep_dir / name).write_bytes(b"x" * 2048)
    return ep_dir


# -- removing one episode ----------------------------------------------------

def test_a_dry_run_reports_and_removes_nothing(corpus):
    source = guest_source()
    ep_dir = stage(source, "agYo_1Whvp0", extra_files=("audio.m4a",))

    report = ingest.remove_episode("agYo_1Whvp0")

    assert report["applied"] is False
    assert report["source"] == "guest_james_a_shapiro"
    assert report["stored_audio"] == ["audio.m4a"]
    assert report["bytes"] > 0
    assert ep_dir.is_dir(), "a dry run must not delete anything"


def test_the_expensive_part_is_called_out_on_its_own(corpus):
    """Captions refetch in seconds; audio is ~50 MB through a throttled pipe."""
    source = guest_source()
    stage(source, "E1")
    assert ingest.remove_episode("E1")["stored_audio"] == []

    stage(source, "E2", extra_files=("audio.opus",))
    assert ingest.remove_episode("E2")["stored_audio"] == ["audio.opus"]


def test_applying_removes_the_episode_from_disk(corpus):
    source = guest_source()
    ep_dir = stage(source, "agYo_1Whvp0", extra_files=("audio.m4a",))

    report = ingest.remove_episode("agYo_1Whvp0", apply=True)

    assert report["applied"] is True
    assert not ep_dir.exists()


def test_removing_also_clears_the_index(corpus):
    """Leaving the chunks would be worse than not removing it at all.

    Search would go on returning quotes from something no longer on disk and
    no longer cuttable.
    """
    from quotesource.embedder import EMBED_SCHEMA
    from quotesource.indexer import _ensure_schema, connect

    source = guest_source()
    stage(source, "EP")
    con = connect()
    _ensure_schema(con)
    con.executescript(EMBED_SCHEMA)
    con.execute("INSERT INTO episodes (episode_id, source_id, title, "
                "upload_date, url) VALUES "
                "('EP','guest_james_a_shapiro','t','x','u')")
    cur = con.execute("INSERT INTO chunks (episode_id, start, end, text) "
                      "VALUES ('EP', 0, 1, 'a quote')")
    con.execute("INSERT INTO chunks_fts(rowid, text) VALUES (?,?)",
                (cur.lastrowid, "a quote"))
    con.execute("INSERT INTO embeddings (chunk_id, vector) VALUES (?,?)",
                (cur.lastrowid, b"\x00" * 16))
    con.commit()
    con.close()

    report = ingest.remove_episode("EP", apply=True)

    assert report["chunks_removed"] == 1
    con = connect()
    assert con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0] == 0
    con.close()


def test_an_unknown_episode_is_refused_not_silently_ignored(corpus):
    guest_source()
    with pytest.raises(LookupError):
        ingest.remove_episode("NOSUCHVIDEO", apply=True)


def test_the_wrong_source_does_not_match(corpus):
    source = guest_source()
    stage(source, "EP")
    registry.add_source("other", "Other", "episodes", "")

    with pytest.raises(LookupError):
        ingest.remove_episode("EP", source_id="other")


# -- noticing a duplicate at add time ----------------------------------------

def test_the_same_talk_uploaded_twice_is_flagged(corpus):
    source = guest_source()
    stage(source, "2iYfKYLgKPU",
          title="Evolution: A View from the 21st Century", duration=6540.0)

    twin = ingest.find_duplicate(
        source, "agYo_1Whvp0",
        "Evolution: A View from the 21st Century", 6541.0)

    assert twin["episode_id"] == "2iYfKYLgKPU"
    assert twin["title_ratio"] >= 0.85


def test_a_different_talk_of_the_same_length_is_not_flagged(corpus):
    """Two conference talks can legitimately run to the same second."""
    source = guest_source()
    stage(source, "AAA", title="On Natural Genetic Engineering",
          duration=3600.0)

    assert ingest.find_duplicate(source, "BBB", "Cells Are Cognitive",
                                 3600.0) is None


def test_a_similar_title_at_a_different_length_is_not_flagged(corpus):
    source = guest_source()
    stage(source, "AAA", title="Evolution: A View from the 21st Century",
          duration=6540.0)

    assert ingest.find_duplicate(
        source, "BBB", "Evolution: A View from the 21st Century",
        900.0) is None


def test_an_episode_never_flags_itself(corpus):
    source = guest_source()
    stage(source, "AAA", title="A Lecture", duration=6540.0)

    assert ingest.find_duplicate(source, "AAA", "A Lecture", 6540.0) is None


def test_an_unknown_duration_cannot_be_compared(corpus):
    """Guessing on title alone would flag every episode of a numbered series."""
    source = guest_source()
    stage(source, "AAA", title="A Lecture", duration=6540.0)

    assert ingest.find_duplicate(source, "BBB", "A Lecture", None) is None


def test_add_hangs_the_warning_off_the_row_it_already_returns(corpus,
                                                              monkeypatch):
    source = guest_source()
    stage(source, "2iYfKYLgKPU", title="One Long Lecture", duration=6540.0)

    def fake_fetch(src, entry, quiet):
        ep_dir = ingest.episode_dir(src["id"], entry["episode_id"])
        ep_dir.mkdir(parents=True, exist_ok=True)
        out = {"episode_id": entry["episode_id"], "source_id": src["id"],
               "title": "One Long Lecture", "duration": 6540.0,
               "status": "captions", "uploader": "Some Channel"}
        ingest._write_metadata(ep_dir, out)
        return out

    monkeypatch.setattr(ingest, "_fetch_youtube_episode", fake_fetch)
    row = ingest.add_episode("https://youtu.be/agYo_1Whvp0", source, quiet=True)

    assert row["possible_duplicate"]["episode_id"] == "2iYfKYLgKPU"
    assert row["status"] == "captions", "warned about, never refused"


# -- the CLI -----------------------------------------------------------------

def test_guest_remove_without_yes_is_a_dry_run(corpus, capsys):
    from quotesource import cli

    source = guest_source()
    ep_dir = stage(source, "agYo_1Whvp0")
    cli.main(["guest", "remove", "agYo_1Whvp0"])

    out = json.loads(capsys.readouterr().out)
    assert out["removed"] == 0
    assert "dry run" in out["note"]
    assert ep_dir.is_dir()


def test_guest_remove_with_yes_applies(corpus, capsys):
    from quotesource import cli

    source = guest_source()
    ep_dir = stage(source, "agYo_1Whvp0")
    cli.main(["guest", "remove", "https://youtu.be/agYo_1Whvp0", "--yes"])

    out = json.loads(capsys.readouterr().out)
    assert out["removed"] == 1
    assert not ep_dir.exists()


def test_sources_add_accepts_the_natural_positional_form(corpus, capsys):
    from quotesource import cli

    cli.main(["sources", "add", "lex", "Lex Fridman", "youtube_channel",
              "https://youtube.com/@lexfridman"])

    out = json.loads(capsys.readouterr().out)
    assert out["id"] == "lex"
    assert out["type"] == "youtube_channel"
    assert registry.get_source("lex")["name"] == "Lex Fridman"


def test_an_incomplete_sources_add_names_what_is_missing(corpus, capsys):
    from quotesource import cli

    with pytest.raises(SystemExit):
        cli.main(["sources", "add", "lex"])

    # errors go to stderr, exit 2 - see _fail
    err = json.loads(capsys.readouterr().err)["error"]
    assert "--name" in err and "--type" in err and "--url" in err
    assert "--id" not in err, "it was supplied positionally"


def test_status_says_which_interpreter_is_running(corpus, capsys):
    """`./qs` finds one with the deps; an ad-hoc `python3 -c` does not."""
    import sys

    from quotesource import cli

    cli.main(["status"])
    assert json.loads(capsys.readouterr().out)["python"] == sys.executable
