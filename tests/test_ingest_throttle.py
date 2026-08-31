"""Not looking like a bot, and knowing when to stop.

Two failures this guards against, both learned the hard way on a real channel:
requests spaced at an exact interval are trivially identifiable as automated,
and a loop that retries every episode against a live rate limit turns a soft
throttle into a hard one.

Nothing here touches the network - yt-dlp and the enumerator are stubbed.
"""
import sys
import types

import pytest

from quotesource import ingest


# ── the jittered pause ────────────────────────────────────────────────────────

def test_pause_stays_inside_the_jitter_band():
    for _ in range(200):
        assert 0.8 <= ingest._pause(2.0, 0.6) <= 3.2


def test_pause_is_not_a_metronome():
    """The whole point: a fixed cadence is the signature we are avoiding."""
    samples = {round(ingest._pause(2.0, 0.6), 6) for _ in range(50)}
    assert len(samples) > 40


def test_pause_of_zero_does_not_sleep():
    assert ingest._pause(0.0) == 0.0
    assert ingest._pause(-1.0) == 0.0


def test_pause_never_returns_a_negative_or_vanishing_delay():
    """Full jitter must not let a request follow instantly."""
    for _ in range(200):
        assert ingest._pause(1.0, 1.0) >= 0.05


def test_jitter_above_one_is_clamped():
    for _ in range(100):
        assert ingest._pause(2.0, 5.0) >= 0.05


def test_pause_defaults_to_the_module_constants(monkeypatch):
    monkeypatch.setattr(ingest, "SLEEP_BETWEEN_EPISODES", 10.0)
    monkeypatch.setattr(ingest, "SLEEP_JITTER", 0.0)
    assert ingest._pause() == pytest.approx(10.0)


# ── the gap between requests inside one fetch ─────────────────────────────────

def test_request_gap_defaults_to_one_second(monkeypatch):
    monkeypatch.delenv("QS_DOWNLOAD_SLEEP_S", raising=False)
    assert ingest._request_gap() == 1.0


def test_request_gap_is_tunable(monkeypatch):
    monkeypatch.setenv("QS_DOWNLOAD_SLEEP_S", "2.5")
    assert ingest._request_gap() == 2.5


def test_a_nonsense_gap_falls_back_rather_than_crashing(monkeypatch):
    monkeypatch.setenv("QS_DOWNLOAD_SLEEP_S", "soon")
    assert ingest._request_gap() == 1.0


def test_the_caption_fetch_asks_yt_dlp_to_space_its_own_requests(monkeypatch,
                                                                 tmp_path):
    """A polite gap between episodes still brackets a burst without this."""
    captured = {}

    class FakeYDL:
        def __init__(self, opts):
            captured.update(opts)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=False):
            raise RuntimeError("stop here - the options are what matter")

    monkeypatch.setenv("QUOTESOURCE_DATA", str(tmp_path))
    fake = types.ModuleType("yt_dlp")
    fake.YoutubeDL = FakeYDL
    monkeypatch.setitem(sys.modules, "yt_dlp", fake)

    with pytest.raises(RuntimeError):
        ingest._fetch_youtube_episode(
            {"id": "src"}, {"episode_id": "e1", "url": "u"}, True)

    assert captured["sleep_interval_requests"] == ingest._request_gap()


# ── one episode's backoff ─────────────────────────────────────────────────────

@pytest.fixture
def no_sleeping(monkeypatch):
    monkeypatch.setattr(ingest.time, "sleep", lambda *_: None)


def test_a_clean_fetch_reports_that_it_was_not_limited(monkeypatch, no_sleeping):
    monkeypatch.setattr(ingest, "_fetch_youtube_episode",
                        lambda *a, **k: {"ok": True})
    meta, hit = ingest._fetch_with_backoff({"id": "s"}, {"episode_id": "e"}, True)
    assert meta == {"ok": True}
    assert hit is False


def test_a_fetch_that_recovers_still_admits_it_was_limited(monkeypatch, no_sleeping):
    """The endpoint already asked us to slow down; success does not undo that."""
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("HTTP Error 429: Too Many Requests")
        return {"ok": True}

    monkeypatch.setattr(ingest, "_fetch_youtube_episode", flaky)
    _, hit = ingest._fetch_with_backoff({"id": "s"}, {"episode_id": "e"}, True)
    assert hit is True


def test_exhausting_the_schedule_raises_rate_limited(monkeypatch, no_sleeping):
    monkeypatch.setattr(ingest, "BACKOFF_SCHEDULE", [1])
    monkeypatch.setattr(ingest, "_fetch_youtube_episode", _always_limited)
    with pytest.raises(ingest.RateLimited):
        ingest._fetch_with_backoff({"id": "s"}, {"episode_id": "e"}, True)


def test_an_ordinary_error_is_not_retried(monkeypatch, no_sleeping):
    """Only rate limits get the backoff; a real error should surface at once."""
    calls = {"n": 0}

    def broken(*a, **k):
        calls["n"] += 1
        raise ValueError("no captions for this video")

    monkeypatch.setattr(ingest, "_fetch_youtube_episode", broken)
    with pytest.raises(ValueError):
        ingest._fetch_with_backoff({"id": "s"}, {"episode_id": "e"}, True)
    assert calls["n"] == 1


def _always_limited(*a, **k):
    raise RuntimeError("HTTP Error 429: Too Many Requests")


# ── the circuit breaker ───────────────────────────────────────────────────────

@pytest.fixture
def run_ingest(monkeypatch, tmp_path):
    """ingest_source over N fake episodes with a scripted fetch outcome."""
    monkeypatch.setenv("QUOTESOURCE_DATA", str(tmp_path))
    monkeypatch.setattr(ingest.time, "sleep", lambda *_: None)
    monkeypatch.setattr(ingest, "BACKOFF_SCHEDULE", [])

    def go(outcomes):
        seen = []

        def fetch(source, entry, quiet):
            seen.append(entry["episode_id"])
            outcome = outcomes[entry["episode_id"]]
            if outcome is not None:
                raise outcome
            return {"ok": True}

        monkeypatch.setattr(ingest, "_enumerate_youtube",
                            lambda url, stype: [{"episode_id": k, "url": "u",
                                                 "title": k, "duration": 3600}
                                                for k in outcomes])
        monkeypatch.setattr(ingest, "_fetch_youtube_episode", fetch)
        result = ingest.ingest_source(
            {"id": "src", "type": "youtube_channel", "url": "u"}, quiet=True)
        return result, seen

    return go


def limited():
    return RuntimeError("HTTP Error 429: Too Many Requests")


def test_consecutive_rate_limits_stop_the_run(run_ingest):
    """The bug this fixes: the loop used to walk the whole channel retrying."""
    result, seen = run_ingest({"a": limited(), "b": limited(),
                               "c": None, "d": None, "e": None})
    assert result["stopped"] == "rate_limited"
    assert seen == ["a", "b"]           # c, d, e were never requested


def test_a_completed_run_says_it_was_not_stopped(run_ingest):
    result, seen = run_ingest({"a": None, "b": None, "c": None})
    assert result["stopped"] is None
    assert result["new"] == 3
    assert seen == ["a", "b", "c"]


def test_a_success_between_limits_resets_the_count(run_ingest):
    """One 429, then recovery, is a blip - not a reason to abandon the run."""
    result, seen = run_ingest({"a": limited(), "b": None, "c": limited(),
                               "d": None, "e": None})
    assert result["stopped"] is None
    assert seen == ["a", "b", "c", "d", "e"]


def test_ordinary_failures_never_trip_the_breaker(run_ingest):
    """A channel full of caption-less videos is not a rate limit."""
    result, seen = run_ingest({"a": ValueError("no captions"),
                               "b": ValueError("no captions"),
                               "c": ValueError("no captions")})
    assert result["stopped"] is None
    assert result["failed"] == 3
    assert seen == ["a", "b", "c"]


def test_an_ordinary_failure_between_limits_does_not_shield_them(run_ingest):
    """Only a success clears the count - a different error is not a reprieve.

    Otherwise a channel that alternates 429s with caption-less videos would
    never trip the breaker and would grind on indefinitely.
    """
    result, seen = run_ingest({"a": limited(), "b": ValueError("no captions"),
                               "c": limited(), "d": None})
    assert result["stopped"] == "rate_limited"
    assert seen == ["a", "b", "c"]


def test_the_threshold_is_tunable(run_ingest, monkeypatch):
    monkeypatch.setattr(ingest, "RATE_LIMIT_STOP", 3)
    result, seen = run_ingest({"a": limited(), "b": limited(),
                               "c": limited(), "d": None})
    assert result["stopped"] == "rate_limited"
    assert seen == ["a", "b", "c"]


def test_a_stopped_run_still_reports_what_it_managed(run_ingest):
    result, _ = run_ingest({"a": None, "b": limited(), "c": limited(),
                            "d": None})
    assert result["new"] == 1
    assert result["failed"] == 2
    assert result["episodes"] == ["a"]


def test_the_loop_sleeps_a_different_amount_each_time(monkeypatch, tmp_path):
    """Guards the integration, not just the helper: no metronome in the loop."""
    slept = []
    monkeypatch.setenv("QUOTESOURCE_DATA", str(tmp_path))
    monkeypatch.setattr(ingest.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(ingest, "_enumerate_youtube",
                        lambda url, stype: [{"episode_id": f"e{i}", "url": "u",
                                             "title": "t", "duration": 3600}
                                            for i in range(12)])
    monkeypatch.setattr(ingest, "_fetch_youtube_episode",
                        lambda *a, **k: {"ok": True})

    ingest.ingest_source({"id": "src", "type": "youtube_channel", "url": "u"},
                         quiet=True)
    assert len(slept) >= 10
    assert len(set(slept)) > len(slept) * 0.8


# ── what kind of failure was that ─────────────────────────────────────────────
#
# A run leaves behind the question "retry now, or wait a day?", and answering
# it meant eyeballing an error string. Three Vervaeke episodes failed on
# `Read timed out` and all three cleared on a plain re-run; nothing in the
# result said they were the retryable sort.

def test_a_rate_limit_is_known_from_the_exception_not_its_words():
    """Type is certain where matching prose is a guess."""
    from quotesource.ingest import RateLimited, failure_kind

    assert failure_kind(RateLimited("anything at all"),
                        rate_limited=True) == "rate_limited"


@pytest.mark.parametrize("message", [
    "HTTPSConnectionPool: Read timed out. (read timeout=20)",
    "The read operation timed out",
    "TimeoutError",
])
def test_a_timeout_is_classified_as_transient(message):
    from quotesource.ingest import failure_kind

    assert failure_kind(message) == "timeout"


@pytest.mark.parametrize("message", [
    "HTTP Error 429: Too Many Requests",
    "yt-dlp: error: 429 returned",
])
def test_a_429_in_the_text_still_reads_as_rate_limited(message):
    from quotesource.ingest import failure_kind

    assert failure_kind(message) == "rate_limited"


def test_anything_unrecognised_is_other_rather_than_guessed():
    from quotesource.ingest import failure_kind

    assert failure_kind("Video unavailable in your country") == "other"
    assert failure_kind("") == "other"
    assert failure_kind(None) == "other"


def test_a_recorded_failure_carries_its_kind(run_ingest):
    """The whole point is that a caller does not have to parse the prose."""
    result, _ = run_ingest(
        {"a": RuntimeError("HTTPSConnectionPool: Read timed out.")})

    assert result["failed"] == 1
    assert result["failures"][0]["kind"] == "timeout"
    assert "timed out" in result["failures"][0]["error"], "the prose is kept too"


def test_a_rate_limited_failure_is_recorded_as_one(run_ingest):
    """These are the ones to wait on rather than retry, and now say so."""
    result, _ = run_ingest({"a": limited(), "b": limited()})

    assert result["stopped"] == "rate_limited"
    assert [f["kind"] for f in result["failures"]] == ["rate_limited",
                                                       "rate_limited"]
