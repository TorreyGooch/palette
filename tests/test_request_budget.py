"""Not meeting the limit, rather than handling it well.

The 429 arrived at roughly 120 requests inside 25 minutes — comfortably under
the ~300/day that had been the working figure. Density is what a limiter
watches, and jitter only ever fixed the *cadence* signature: it made the gaps
irregular and did nothing about how many requests went out per minute.

So the hourly allowance is a minimum gap as well as a ceiling. A cap on its
own permits thirty requests inside one minute followed by an idle hour, which
is the exact shape that drew the limit.
"""
import json
from datetime import datetime, timedelta

import pytest

from quotesource import ingest


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    monkeypatch.setenv("QUOTESOURCE_DATA", str(tmp_path))
    monkeypatch.setenv("QS_MAX_PER_HOUR", "30")
    monkeypatch.setenv("QS_MAX_PER_DAY", "200")
    monkeypatch.delenv("QS_IGNORE_COOLDOWN", raising=False)
    return tmp_path


def write_ledger(corpus, stamps):
    ingest.budget_path().write_text(
        json.dumps([s.isoformat(timespec="seconds") for s in stamps]),
        encoding="utf-8")


def ago(**kw):
    return datetime.now() - timedelta(**kw)


# -- what has been spent ------------------------------------------------------

def test_a_fresh_corpus_has_spent_nothing(corpus):
    state = ingest.budget_state()
    assert state["hour"] == 0 and state["day"] == 0
    assert state["wait_s"] == 0
    assert state["day_exhausted"] is False


def test_requests_older_than_a_day_fall_out_of_the_window(corpus):
    write_ledger(corpus, [ago(days=2), ago(days=1, minutes=5), ago(minutes=5)])
    state = ingest.budget_state()
    assert state["day"] == 1


def test_the_hourly_window_is_an_hour(corpus):
    write_ledger(corpus, [ago(hours=2), ago(minutes=30), ago(minutes=5)])
    state = ingest.budget_state()
    assert state["hour"] == 2
    assert state["day"] == 3


# -- spacing, which is the part a cap alone does not give ---------------------

def test_the_allowance_is_also_a_minimum_gap(corpus):
    """30/hour means one about every two minutes, not thirty in one minute."""
    assert ingest.budget_state()["spacing_s"] == 120.0


def test_a_recent_request_makes_the_next_one_wait(corpus):
    write_ledger(corpus, [ago(seconds=10)])
    assert ingest.budget_state()["wait_s"] == pytest.approx(110, abs=2)


def test_an_old_enough_request_imposes_no_wait(corpus):
    write_ledger(corpus, [ago(seconds=300)])
    assert ingest.budget_state()["wait_s"] == 0


def test_a_full_hour_waits_for_the_oldest_to_age_out(corpus):
    write_ledger(corpus, [ago(minutes=50) for _ in range(30)])
    state = ingest.budget_state()
    assert state["hour"] == 30
    assert state["wait_s"] == pytest.approx(600, abs=30)


# -- the daily cap stops rather than sleeps -----------------------------------

def test_the_daily_cap_refuses_rather_than_sleeping_through_it(corpus):
    """Sleeping out a day inside a process is fragile and pointless."""
    write_ledger(corpus, [ago(hours=n % 20, minutes=n) for n in range(200)])

    with pytest.raises(ingest.BudgetExhausted) as raised:
        ingest.await_slot(quiet=True)

    assert "resumable" in str(raised.value)


def test_an_exhausted_budget_before_any_work_refuses_outright(corpus,
                                                              monkeypatch):
    """Nothing done yet, so there is no partial run to report."""
    from quotesource import registry

    monkeypatch.setattr(ingest.time, "sleep", lambda *_: None)
    write_ledger(corpus, [ago(hours=n % 20, minutes=n) for n in range(200)])
    source = registry.add_source("s", "S", "youtube_channel", "https://x")
    walked = []
    monkeypatch.setattr(ingest, "_enumerate_youtube",
                        lambda *a, **k: walked.append(1) or [])

    with pytest.raises(ingest.BudgetExhausted):
        ingest.ingest_source(source, quiet=True)

    assert walked == [], "not even the channel walk"


def test_running_out_mid_run_keeps_what_it_managed(corpus, monkeypatch):
    """The opposite case: throwing away a partial run would be worse."""
    from quotesource import registry

    monkeypatch.setattr(ingest.time, "sleep", lambda *_: None)
    monkeypatch.setenv("QS_MAX_PER_DAY", "3")
    source = registry.add_source("s", "S", "youtube_channel", "https://x")
    monkeypatch.setattr(ingest, "_enumerate_youtube",
                        lambda *a, **k: [{"episode_id": f"e{i}", "url": "u",
                                          "title": "t", "duration": 3600}
                                         for i in range(6)])
    monkeypatch.setattr(ingest, "_fetch_youtube_episode",
                        lambda *a, **k: {"ok": True})

    result = ingest.ingest_source(source, quiet=True)

    assert result["stopped"] == "budget"
    assert result["new"] == 2, "one walk plus two fetches spent the three"
    assert result["rate_limited"] is False
    assert ingest.cooldown_state() is None, "nothing to cool down from"


# -- every request counts, including the expensive one ------------------------

def test_the_channel_walk_spends_from_the_budget(corpus, monkeypatch):
    """Re-running an ingest re-walks the channel, and that is a request too."""
    from quotesource import registry

    monkeypatch.setattr(ingest.time, "sleep", lambda *_: None)
    source = registry.add_source("s", "S", "youtube_channel", "https://x")
    monkeypatch.setattr(ingest, "_enumerate_youtube", lambda *a, **k: [])

    ingest.ingest_source(source, quiet=True)

    assert ingest.budget_state()["day"] == 1


def test_each_episode_fetch_spends_one(corpus, monkeypatch):
    from quotesource import registry

    monkeypatch.setattr(ingest.time, "sleep", lambda *_: None)
    source = registry.add_source("s", "S", "youtube_channel", "https://x")
    monkeypatch.setattr(ingest, "_enumerate_youtube",
                        lambda *a, **k: [{"episode_id": f"e{i}", "url": "u",
                                          "title": "t", "duration": 3600}
                                         for i in range(4)])
    monkeypatch.setattr(ingest, "_fetch_youtube_episode",
                        lambda *a, **k: {"ok": True})

    ingest.ingest_source(source, quiet=True)

    assert ingest.budget_state()["day"] == 5, "one walk plus four fetches"


def test_a_podcast_feed_does_not_spend_the_youtube_budget(corpus, monkeypatch):
    """A CDN that wants you to have the file has no limit worth the name."""
    from quotesource import registry

    monkeypatch.setattr(ingest.time, "sleep", lambda *_: None)
    source = registry.add_source("r", "R", "rss", "https://feed")
    monkeypatch.setattr(ingest, "_enumerate_rss",
                        lambda *a, **k: [{"episode_id": "e1", "url": "u",
                                          "title": "t", "duration": 3600}])
    monkeypatch.setattr(ingest, "_fetch_rss_episode", lambda *a, **k: {"ok": 1})

    ingest.ingest_source(source, quiet=True)

    assert ingest.budget_state()["day"] == 0


def test_a_guest_add_spends_one(corpus, monkeypatch):
    from quotesource import registry

    monkeypatch.setattr(ingest.time, "sleep", lambda *_: None)
    source = registry.add_source("g", "G", "episodes", "", people=["X"])

    def fake(src, entry, quiet):
        ep_dir = ingest.episode_dir(src["id"], entry["episode_id"])
        ep_dir.mkdir(parents=True, exist_ok=True)
        out = {"episode_id": entry["episode_id"], "source_id": src["id"],
               "title": "t", "status": "captions", "duration": 60}
        ingest._write_metadata(ep_dir, out)
        return out

    monkeypatch.setattr(ingest, "_fetch_youtube_episode", fake)
    ingest.add_episode("https://youtu.be/PWasTAtR6Ns", source, quiet=True)

    assert ingest.budget_state()["day"] == 1


# -- shared, because two sessions must not each spend the whole allowance -----

def test_the_ledger_is_on_disk_for_every_session_to_see(corpus):
    ingest.record_request()
    assert ingest.budget_path().exists()
    assert ingest.budget_state()["day"] == 1

    # A second session reads the same file rather than starting from zero.
    assert len(ingest._read_ledger()) == 1


def test_a_corrupt_ledger_does_not_wedge_ingest(corpus):
    ingest.budget_path().write_text("{ not json", encoding="utf-8")
    assert ingest.budget_state()["day"] == 0


def test_status_reports_what_is_left(corpus):
    from quotesource.status import corpus_status

    write_ledger(corpus, [ago(minutes=5) for _ in range(3)])
    budget = corpus_status()["budget"]
    assert budget["hour"] == 3
    assert budget["hour_max"] == 30
    assert budget["day_max"] == 200
