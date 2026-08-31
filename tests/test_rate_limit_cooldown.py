"""After YouTube says stop, we stay stopped — across runs and across sessions.

A hard 429 during a levin_yt batch was survived rather than obeyed: the run
fetched twenty more episodes afterwards, and nothing prevented a fresh
`qs ingest` two minutes later. Stopping a run is not the same as not going
back, and the directive is the second one.

The cooldown is a file rather than a variable because three sessions share
this project and none of them can see another's in-flight requests. A file is
the only thing all three can read.
"""
import json
from datetime import datetime, timedelta

import pytest

from quotesource import ingest


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    monkeypatch.setenv("QUOTESOURCE_DATA", str(tmp_path))
    monkeypatch.delenv("QS_IGNORE_COOLDOWN", raising=False)
    monkeypatch.delenv("QS_RATE_LIMIT_COOLDOWN_H", raising=False)
    return tmp_path


def limited(message="HTTP Error 429: Too Many Requests"):
    return RuntimeError(message)


# -- classifying by whose problem it is --------------------------------------

@pytest.mark.parametrize("message,expected", [
    ("HTTP Error 429: Too Many Requests", "client"),
    ("ERROR: unable to download: HTTP Error 403: Forbidden", "client"),
    ("HTTP Error 503: Service Unavailable", "server"),
    ("HTTP Error 502: Bad Gateway", "server"),
    ("HTTPSConnectionPool: Read timed out.", "transport"),
    ("Connection reset by peer", "transport"),
    ("no captions for this video", "other"),
])
def test_failures_are_sorted_by_who_they_are_about(message, expected):
    """429 and 503 are opposites and shared one schedule.

    A 503 says the server is unwell and wants you back; a 429 says the server
    is healthy and rationing you specifically. Retrying is textbook for the
    first and self-incriminating for the second.
    """
    assert ingest.failure_policy(RuntimeError(message)) == expected


def test_an_unknown_failure_is_not_retried():
    """An unrecognised error is not evidence that asking again is safe."""
    assert ingest.failure_policy(RuntimeError("something new")) == "other"


# -- Retry-After, when the server names one ----------------------------------

@pytest.mark.parametrize("message,expected", [
    ("HTTP Error 429; Retry-After: 3600", 3600.0),
    ("retry after 90", 90.0),
    ("Retry-After=120", 120.0),
])
def test_a_named_retry_after_is_read(message, expected):
    assert ingest.retry_after_seconds(RuntimeError(message)) == expected


@pytest.mark.parametrize("message", [
    "HTTP Error 429: Too Many Requests",     # none given
    "Retry-After: 0",                        # nonsense
    "Retry-After: 99999999",                 # beyond a week; not credible
])
def test_an_absent_or_implausible_retry_after_reads_as_none(message):
    assert ingest.retry_after_seconds(RuntimeError(message)) is None


def test_a_named_retry_after_beats_our_own_figure(corpus, monkeypatch):
    """The server saying exactly what it wants outranks any schedule we pick."""
    monkeypatch.setenv("QS_RATE_LIMIT_COOLDOWN_H", "6")
    state = ingest.begin_cooldown(limited("429; Retry-After: 1800"))

    assert state["from_retry_after"] is True
    assert state["seconds"] == 1800


def test_without_one_the_configured_cooldown_is_used(corpus, monkeypatch):
    monkeypatch.setenv("QS_RATE_LIMIT_COOLDOWN_H", "2")
    state = ingest.begin_cooldown(limited())

    assert state["from_retry_after"] is False
    assert state["seconds"] == 7200


# -- the standoff itself ------------------------------------------------------

def test_no_cooldown_means_no_refusal(corpus):
    assert ingest.cooldown_state() is None
    ingest.check_cooldown()          # must not raise


def test_an_active_cooldown_refuses_before_any_request(corpus):
    ingest.begin_cooldown(limited(), "levin_yt")

    with pytest.raises(ingest.InCooldown) as raised:
        ingest.check_cooldown()

    message = str(raised.value)
    assert "not asking again" in message
    assert "resumable" in message, "it must say waiting is cheap"


def test_an_expired_cooldown_lets_work_resume(corpus):
    ingest.begin_cooldown(limited())
    path = ingest.cooldown_path()
    state = json.loads(path.read_text(encoding="utf-8"))
    state["until"] = (datetime.now() - timedelta(minutes=1)).isoformat()
    path.write_text(json.dumps(state), encoding="utf-8")

    assert ingest.cooldown_state() is None
    ingest.check_cooldown()


def test_the_cooldown_survives_a_new_run(corpus, monkeypatch):
    """Every `qs ingest` used to start with amnesia. That is the whole bug."""
    from quotesource import registry

    monkeypatch.setattr(ingest.time, "sleep", lambda *_: None)
    source = registry.add_source("s", "S", "youtube_channel", "https://x")
    monkeypatch.setattr(ingest, "_enumerate_youtube",
                        lambda *a, **k: [{"episode_id": "E1", "url": "u",
                                          "title": "t", "duration": 60}])

    def refuse(*a, **k):
        raise RuntimeError("HTTP Error 429: Too Many Requests")

    monkeypatch.setattr(ingest, "_fetch_with_backoff", refuse)
    first = ingest.ingest_source(source, quiet=True)
    assert first["stopped"] == "rate_limited"
    assert first["cooldown"]["until"]

    # A second run, minutes later, must not reach the network at all.
    reached = []
    monkeypatch.setattr(ingest, "_fetch_with_backoff",
                        lambda *a, **k: reached.append(1))
    with pytest.raises(ingest.InCooldown):
        ingest.ingest_source(source, quiet=True)
    assert reached == []


def test_reading_the_cooldown_does_not_clear_it(corpus):
    """A read path does not write, and when it happened is worth keeping."""
    ingest.begin_cooldown(limited())
    ingest.cooldown_state()
    ingest.cooldown_state()
    assert ingest.cooldown_path().exists()


def test_a_corrupt_cooldown_file_does_not_wedge_the_corpus(corpus):
    """Failing open is right here: it is a standoff marker, not the data."""
    ingest.cooldown_path().write_text("{ not json", encoding="utf-8")
    assert ingest.cooldown_state() is None
    ingest.check_cooldown()


def test_the_override_exists_and_is_explicit(corpus, monkeypatch):
    """Deliberately awkward: overriding it is asking to be limited harder."""
    ingest.begin_cooldown(limited())
    monkeypatch.setenv("QS_IGNORE_COOLDOWN", "1")
    ingest.check_cooldown()


def test_status_reports_the_standoff(corpus):
    """Finding this out used to mean grepping a log in /tmp."""
    from quotesource.status import corpus_status

    assert corpus_status()["cooldown"]["active"] is False

    ingest.begin_cooldown(limited(), "levin_yt")
    block = corpus_status()["cooldown"]
    assert block["active"] is True
    assert block["source"] == "levin_yt"
    assert block["remaining_s"] > 0
