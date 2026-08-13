"""Duration parsing and the clip-re-upload filter.

The filter is what keeps a channel's excerpt uploads from putting the same
words in the corpus twice under different episode ids.
"""
import pytest

from quotesource.registry import parse_duration


@pytest.mark.parametrize("raw,seconds", [
    ("1800", 1800), (1800, 1800), (1800.0, 1800),
    ("30m", 1800), ("1h", 3600), ("1h30m", 5400),
    ("90s", 90), ("2h5m30s", 7530),
    (None, None), ("", None),
])
def test_parse_duration(raw, seconds):
    assert parse_duration(raw) == seconds


@pytest.mark.parametrize("raw", ["abc", "30x", "1h!", "m30"])
def test_parse_duration_rejects_nonsense(raw):
    with pytest.raises(ValueError):
        parse_duration(raw)


def apply_filter(entries, minimum):
    """Mirror of the filter in ingest_source, kept in one place to assert on."""
    return [e for e in entries
            if not (e.get("duration") is not None and e["duration"] < minimum)]


def test_filter_drops_short_and_keeps_long():
    entries = [{"episode_id": "clip", "duration": 240},
               {"episode_id": "episode", "duration": 7200}]

    kept = [e["episode_id"] for e in apply_filter(entries, 1800)]
    assert kept == ["episode"]


def test_filter_keeps_unknown_durations():
    """Dropping a real episode is worse than admitting a clip."""
    entries = [{"episode_id": "unknown", "duration": None},
               {"episode_id": "clip", "duration": 60}]

    kept = [e["episode_id"] for e in apply_filter(entries, 1800)]
    assert kept == ["unknown"]


def test_filter_boundary_is_inclusive():
    entries = [{"episode_id": "exactly", "duration": 1800},
               {"episode_id": "just-under", "duration": 1799}]

    kept = [e["episode_id"] for e in apply_filter(entries, 1800)]
    assert kept == ["exactly"]


def test_min_duration_is_stored_on_the_source(tmp_path, monkeypatch):
    """Stored on the source so later `qs ingest --all` keeps the filter."""
    monkeypatch.setenv("QUOTESOURCE_DATA", str(tmp_path))
    from quotesource import registry

    entry = registry.add_source("lex", "Lex", "youtube_channel",
                                "https://example.com", min_duration="30m")
    assert entry["min_duration"] == 1800
    assert registry.get_source("lex")["min_duration"] == 1800


def test_no_min_duration_key_when_unset(tmp_path, monkeypatch):
    monkeypatch.setenv("QUOTESOURCE_DATA", str(tmp_path))
    from quotesource import registry

    entry = registry.add_source("jbp", "JBP", "youtube_channel",
                                "https://example.com")
    assert "min_duration" not in entry
