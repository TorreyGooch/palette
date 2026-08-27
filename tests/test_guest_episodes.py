"""Adding one episode at a time, grouped by the person worth quoting.

The people this project most wants are guests, not hosts: they turn up once on
a show whose other three hundred episodes are irrelevant. Ingesting the channel
to reach one conversation spends bandwidth, disk and rate limit for nothing.

Nothing here touches the network - the per-episode fetch is stubbed.
"""
import json

import pytest

from quotesource import ingest, registry


@pytest.fixture
def corpus(monkeypatch, tmp_path):
    monkeypatch.setenv("QUOTESOURCE_DATA", str(tmp_path))
    return tmp_path


# ── reading the id out of a URL ───────────────────────────────────────────────

@pytest.mark.parametrize("url,expected", [
    ("https://www.youtube.com/watch?v=PWasTAtR6Ns", "PWasTAtR6Ns"),
    ("https://youtube.com/watch?v=PWasTAtR6Ns&t=477s", "PWasTAtR6Ns"),
    ("https://youtu.be/PWasTAtR6Ns", "PWasTAtR6Ns"),
    ("https://youtu.be/PWasTAtR6Ns?t=90", "PWasTAtR6Ns"),
    ("https://www.youtube.com/shorts/PWasTAtR6Ns", "PWasTAtR6Ns"),
    ("https://www.youtube.com/embed/PWasTAtR6Ns", "PWasTAtR6Ns"),
    ("https://www.youtube.com/live/PWasTAtR6Ns", "PWasTAtR6Ns"),
    ("  https://youtu.be/PWasTAtR6Ns  ", "PWasTAtR6Ns"),
    ("PWasTAtR6Ns", "PWasTAtR6Ns"),          # a bare id is accepted
    ("-dJSvsP4fzo", "-dJSvsP4fzo"),          # ids starting with a dash
])
def test_youtube_id_is_parsed_not_fetched(url, expected):
    """Asking YouTube for the id would spend a request to learn what we hold."""
    assert ingest.youtube_id(url) == expected


@pytest.mark.parametrize("url", [
    "https://example.com/nope", "", None, "not a url",
    "https://www.youtube.com/@SomeChannel",     # a channel is not an episode
    "https://vimeo.com/123456789",
])
def test_a_url_with_no_video_id_is_refused(url):
    assert ingest.youtube_id(url) is None


# ── adding one episode ────────────────────────────────────────────────────────

def guest_source(person="John Vervaeke", sid="guest_john_vervaeke"):
    return registry.add_source(sid, f"{person} (appearances)", "episodes", "",
                               people=[person])


def fake_fetch(meta=None):
    """Stand in for the real fetch, honouring its side effects.

    The real one creates the episode directory and writes metadata.json; the
    skip-and-retry logic reads that file, so a stub that only returns a dict
    would make every episode look like it had never been fetched.
    """
    def fetch(source, entry, quiet):
        ep_dir = ingest.episode_dir(source["id"], entry["episode_id"])
        ep_dir.mkdir(parents=True, exist_ok=True)
        out = {"episode_id": entry["episode_id"], "source_id": source["id"],
               "title": "Vervaeke on Meaning", "uploader": "Some Podcast",
               "upload_date": "20250101", "duration": 7200,
               "url": f"https://www.youtube.com/watch?v={entry['episode_id']}",
               "status": "captions"}
        out.update(meta or {})
        ingest._write_metadata(ep_dir, out)
        return out
    return fetch


def test_adding_an_episode_records_it_under_the_person(corpus, monkeypatch):
    monkeypatch.setattr(ingest, "_fetch_youtube_episode", fake_fetch())
    source = guest_source()

    row = ingest.add_episode("https://youtu.be/PWasTAtR6Ns", source, quiet=True)

    assert row["episode_id"] == "PWasTAtR6Ns"
    assert row["source"] == "guest_john_vervaeke"
    assert row["status"] == "captions"
    assert row["already_had_it"] is False


def test_the_show_it_appeared_on_is_captured(corpus, monkeypatch):
    """Which podcast this came from is worth keeping and costs nothing."""
    monkeypatch.setattr(ingest, "_fetch_youtube_episode", fake_fetch())
    row = ingest.add_episode("https://youtu.be/PWasTAtR6Ns", guest_source(),
                             quiet=True)
    assert row["show"] == "Some Podcast"


def test_a_bad_url_is_refused_before_any_request(corpus, monkeypatch):
    called = []
    monkeypatch.setattr(ingest, "_fetch_youtube_episode",
                        lambda *a, **k: called.append(1))
    with pytest.raises(ValueError):
        ingest.add_episode("https://example.com/nope", guest_source(), quiet=True)
    assert called == []


def test_adding_the_same_episode_twice_costs_nothing(corpus, monkeypatch):
    calls = {"n": 0}

    def counting(source, entry, quiet):
        calls["n"] += 1
        return fake_fetch()(source, entry, quiet)

    monkeypatch.setattr(ingest, "_fetch_youtube_episode", counting)
    source = guest_source()
    ingest.add_episode("https://youtu.be/PWasTAtR6Ns", source, quiet=True)
    again = ingest.add_episode("https://youtu.be/PWasTAtR6Ns", source, quiet=True)

    assert calls["n"] == 1
    assert again["already_had_it"] is True


def test_a_pending_episode_is_retried(corpus, monkeypatch):
    """A transient caption failure should not be mistaken for done."""
    monkeypatch.setattr(ingest, "_fetch_youtube_episode",
                        fake_fetch({"status": "captions_pending"}))
    source = guest_source()
    ingest.add_episode("https://youtu.be/PWasTAtR6Ns", source, quiet=True)

    monkeypatch.setattr(ingest, "_fetch_youtube_episode", fake_fetch())
    again = ingest.add_episode("https://youtu.be/PWasTAtR6Ns", source, quiet=True)
    assert again["already_had_it"] is False
    assert again["status"] == "captions"


def test_adding_an_episode_goes_through_the_same_backoff(corpus, monkeypatch):
    """It must inherit the politeness, not open a second unthrottled path."""
    monkeypatch.setattr(ingest.time, "sleep", lambda *_: None)
    monkeypatch.setattr(ingest, "BACKOFF_SCHEDULE", [1])
    calls = {"n": 0}

    def flaky(source, entry, quiet):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("HTTP Error 429: Too Many Requests")
        return fake_fetch()(source, entry, quiet)

    monkeypatch.setattr(ingest, "_fetch_youtube_episode", flaky)
    row = ingest.add_episode("https://youtu.be/PWasTAtR6Ns", guest_source(),
                             quiet=True)
    assert row["rate_limited"] is True
    assert row["status"] == "captions"


# ── the episodes source type ──────────────────────────────────────────────────

def test_episodes_is_a_valid_source_type():
    assert "episodes" in registry.VALID_TYPES


def test_a_guest_source_needs_no_url(corpus):
    source = guest_source()
    assert source["url"] == ""
    assert source["people"] == ["John Vervaeke"]


def test_ingesting_a_guest_source_enumerates_what_is_on_disk(corpus, monkeypatch):
    """There is no feed to walk, so ingest can only retry what is already here."""
    monkeypatch.setattr(ingest, "_fetch_youtube_episode",
                        fake_fetch({"status": "captions_pending"}))
    monkeypatch.setattr(ingest.time, "sleep", lambda *_: None)
    source = guest_source()
    ingest.add_episode("https://youtu.be/PWasTAtR6Ns", source, quiet=True)
    ingest.add_episode("https://youtu.be/mO9LUWs5M60", source, quiet=True)

    found = ingest._enumerate_episodes_source(source)
    assert sorted(e["episode_id"] for e in found) == ["PWasTAtR6Ns", "mO9LUWs5M60"]


def test_enumerating_an_empty_guest_source_is_not_an_error(corpus):
    assert ingest._enumerate_episodes_source(guest_source()) == []


def test_ingest_on_a_guest_source_retries_the_pending_ones(corpus, monkeypatch):
    monkeypatch.setattr(ingest.time, "sleep", lambda *_: None)
    monkeypatch.setattr(ingest, "_fetch_youtube_episode",
                        fake_fetch({"status": "captions_pending"}))
    source = guest_source()
    ingest.add_episode("https://youtu.be/PWasTAtR6Ns", source, quiet=True)

    monkeypatch.setattr(ingest, "_fetch_youtube_episode", fake_fetch())
    result = ingest.ingest_source(source, quiet=True)
    assert result["retried"] == 1
    assert result["stopped"] is None


def test_a_finished_guest_episode_is_skipped_by_ingest(corpus, monkeypatch):
    monkeypatch.setattr(ingest.time, "sleep", lambda *_: None)
    monkeypatch.setattr(ingest, "_fetch_youtube_episode", fake_fetch())
    source = guest_source()
    ingest.add_episode("https://youtu.be/PWasTAtR6Ns", source, quiet=True)

    result = ingest.ingest_source(source, quiet=True)
    assert result["skipped"] == 1
    assert result["new"] == 0


# ── the CLI ───────────────────────────────────────────────────────────────────

def test_the_guest_source_id_is_derived_from_the_person():
    from quotesource.cli import _guest_source_id

    assert _guest_source_id("John Vervaeke") == "guest_john_vervaeke"
    assert _guest_source_id("Randolph M. Nesse") == "guest_randolph_m_nesse"
    assert _guest_source_id("  Dennett  ") == "guest_dennett"


def test_a_nameless_person_still_yields_a_usable_id():
    from quotesource.cli import _guest_source_id

    assert _guest_source_id("!!!") == "guest"
    assert _guest_source_id("") == "guest"


def test_guest_add_creates_the_source_and_stages_the_episode(corpus, monkeypatch,
                                                             capsys):
    from quotesource import cli

    monkeypatch.setattr(ingest, "_fetch_youtube_episode", fake_fetch())
    monkeypatch.setattr(ingest.time, "sleep", lambda *_: None)
    cli.main(["guest", "add", "https://youtu.be/PWasTAtR6Ns",
              "--person", "John Vervaeke", "--quiet"])

    out = json.loads(capsys.readouterr().out)
    assert out["source"] == "guest_john_vervaeke"
    assert out["added"] == 1
    assert out["failed"] == 0
    assert registry.get_source("guest_john_vervaeke")["people"] == ["John Vervaeke"]


def test_guest_add_reports_a_bad_url_without_failing_the_rest(corpus, monkeypatch,
                                                              capsys):
    from quotesource import cli

    monkeypatch.setattr(ingest, "_fetch_youtube_episode", fake_fetch())
    monkeypatch.setattr(ingest.time, "sleep", lambda *_: None)
    cli.main(["guest", "add", "https://example.com/nope",
              "https://youtu.be/PWasTAtR6Ns",
              "--person", "John Vervaeke", "--quiet"])

    out = json.loads(capsys.readouterr().out)
    assert out["added"] == 1
    assert out["failed"] == 1


def test_guest_list_shows_only_episode_sources(corpus, monkeypatch, capsys):
    from quotesource import cli

    guest_source()
    registry.add_source("lex", "Lex", "youtube_channel", "https://x", people=["Lex"])
    cli.main(["guest", "list"])

    rows = json.loads(capsys.readouterr().out)
    assert [r["id"] for r in rows] == ["guest_john_vervaeke"]
