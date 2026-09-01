"""Why a source is configured the way it is, and what was deliberately left out.

Two decisions were being made and then forgotten.

`min_duration: 1800` is a number with no argument attached. vervaeke_amc has
1800, levin_yt has 900, jordanpeterson has none, and nothing distinguishes a
measured threshold from an oversight. Prose would not fix it — "dropped the
trailer" cannot be checked a year later.

Rejections were invisible entirely. Finding guests means reading search results
and throwing most of them away, and that judgement is the expensive part: two
searches for "James Shapiro" returned Denis Noble talks and intelligent-design
repackagings of Shapiro's work, neither detectable from a title. Nothing
recorded any of it, so the next person re-evaluates the same URLs or adds one.
"""
import json

import pytest


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    monkeypatch.setenv("QUOTESOURCE_DATA", str(tmp_path))
    monkeypatch.setenv("QS_MAX_PER_HOUR", "1000000")
    monkeypatch.setenv("QS_MAX_PER_DAY", "1000000")
    monkeypatch.delenv("QS_IGNORE_COOLDOWN", raising=False)
    return tmp_path


# ── the evidence behind a threshold ──────────────────────────────────────────

def entries(*durations):
    return [{"episode_id": f"e{i}", "url": "u", "title": f"t{i}",
             "duration": d} for i, d in enumerate(durations)]


def test_a_threshold_records_what_the_channel_looked_like(corpus, monkeypatch):
    """The real case: 51 items, one below the line, and it was a trailer."""
    from quotesource import ingest, registry

    monkeypatch.setattr(ingest.time, "sleep", lambda *_: None)
    source = registry.add_source("v", "V", "youtube_playlist", "https://x",
                                 min_duration=1800)
    monkeypatch.setattr(ingest, "_enumerate_youtube",
                        lambda *a, **k: entries(*([3600] * 49 + [3396, 171])))
    monkeypatch.setattr(ingest, "_fetch_youtube_episode",
                        lambda *a, **k: {"ok": True})

    ingest.ingest_source(source, quiet=True)

    evidence = registry.get_source("v")["min_duration_evidence"]
    assert evidence["enumerated"] == 51
    assert evidence["excluded"] == 1
    assert evidence["longest_excluded_s"] == 171
    assert evidence["shortest_kept_s"] == 3396
    assert evidence["at"]


def test_the_threshold_it_was_measured_against_is_recorded_beside_it(
        corpus, monkeypatch):
    """A --min-duration override applies to one run only.

    Evidence measured against a different number than the one on file would
    otherwise read as though it justified it.
    """
    from quotesource import ingest, registry

    monkeypatch.setattr(ingest.time, "sleep", lambda *_: None)
    source = registry.add_source("v", "V", "youtube_playlist", "https://x",
                                 min_duration=1800)
    monkeypatch.setattr(ingest, "_enumerate_youtube",
                        lambda *a, **k: entries(3600, 1200))
    monkeypatch.setattr(ingest, "_fetch_youtube_episode",
                        lambda *a, **k: {"ok": True})

    ingest.ingest_source(source, quiet=True, min_duration=900)

    stored = registry.get_source("v")
    assert stored["min_duration"] == 1800, "the source's own value is untouched"
    assert stored["min_duration_evidence"]["threshold"] == 900


def test_a_source_with_no_threshold_records_nothing(corpus, monkeypatch):
    """There is no decision to explain, so there is nothing to store."""
    from quotesource import ingest, registry

    monkeypatch.setattr(ingest.time, "sleep", lambda *_: None)
    source = registry.add_source("p", "P", "youtube_channel", "https://x")
    monkeypatch.setattr(ingest, "_enumerate_youtube",
                        lambda *a, **k: entries(3600, 120))
    monkeypatch.setattr(ingest, "_fetch_youtube_episode",
                        lambda *a, **k: {"ok": True})

    ingest.ingest_source(source, quiet=True)

    assert "min_duration_evidence" not in registry.get_source("p")


def test_the_run_reports_it_too(corpus, monkeypatch):
    from quotesource import ingest, registry

    monkeypatch.setattr(ingest.time, "sleep", lambda *_: None)
    source = registry.add_source("v", "V", "youtube_playlist", "https://x",
                                 min_duration=1800)
    monkeypatch.setattr(ingest, "_enumerate_youtube",
                        lambda *a, **k: entries(3600, 171))
    monkeypatch.setattr(ingest, "_fetch_youtube_episode",
                        lambda *a, **k: {"ok": True})

    result = ingest.ingest_source(source, quiet=True)

    assert result["min_duration_evidence"]["excluded"] == 1


def test_a_later_ingest_refreshes_the_snapshot(corpus, monkeypatch):
    """It describes the channel as last seen, not as first registered.

    If forty items now fall under a threshold that once excluded one, the
    number has gone wrong for what the channel became — and only a current
    snapshot makes that visible.
    """
    from quotesource import ingest, registry

    monkeypatch.setattr(ingest.time, "sleep", lambda *_: None)
    source = registry.add_source("v", "V", "youtube_playlist", "https://x",
                                 min_duration=1800)
    monkeypatch.setattr(ingest, "_fetch_youtube_episode",
                        lambda *a, **k: {"ok": True})

    monkeypatch.setattr(ingest, "_enumerate_youtube",
                        lambda *a, **k: entries(3600, 171))
    ingest.ingest_source(source, quiet=True)
    assert registry.get_source("v")["min_duration_evidence"]["excluded"] == 1

    monkeypatch.setattr(ingest, "_enumerate_youtube",
                        lambda *a, **k: entries(3600, 171, 200, 300))
    ingest.ingest_source(source, quiet=True)

    assert registry.get_source("v")["min_duration_evidence"]["excluded"] == 3


# ── what was looked at and declined ──────────────────────────────────────────

def test_a_rejection_records_the_reason_and_the_date(corpus):
    from quotesource import rejections

    out = rejections.reject("iGCQSMsNAOc", "Denis Noble, not Shapiro",
                            person="James A. Shapiro")

    assert out["reason"] == "Denis Noble, not Shapiro"
    assert out["person"] == "James A. Shapiro"
    assert out["at"]


def test_a_rejection_without_a_reason_is_refused(corpus):
    """A rejection with no reason tells the next person only that someone
    said no, which is the thing this file exists to prevent."""
    from quotesource import rejections

    with pytest.raises(ValueError, match="reason"):
        rejections.reject("iGCQSMsNAOc", "   ")


def test_it_is_keyed_by_video_not_by_source(corpus):
    """A rejection is a fact about a video.

    The same Denis Noble result could plausibly be offered again for a Noble
    source later, and the note about who it actually is stays true.
    """
    from quotesource import rejections

    rejections.reject("iGCQSMsNAOc", "Denis Noble, not Shapiro",
                      person="James A. Shapiro")
    stored = json.loads(rejections.rejections_path().read_text(encoding="utf-8"))

    assert list(stored) == ["iGCQSMsNAOc"]
    assert "source" not in stored["iGCQSMsNAOc"]


def test_a_rejection_can_be_taken_back(corpus):
    from quotesource import rejections

    rejections.reject("iGCQSMsNAOc", "wrong person")
    assert rejections.unreject("iGCQSMsNAOc") is True
    assert rejections.rejection_for("iGCQSMsNAOc") is None
    assert rejections.unreject("iGCQSMsNAOc") is False


def test_a_corrupt_file_costs_a_warning_not_an_ingest(corpus):
    """Advisory, not the corpus. It fails open on purpose."""
    from quotesource import rejections

    rejections.rejections_path().write_text("{ not json", encoding="utf-8")

    assert rejections.load_rejections() == {}
    assert rejections.rejection_for("anything") is None


def test_guest_add_warns_about_a_rejected_video(corpus, monkeypatch):
    """The whole value: the fact arrives where the consumer already looks.

    A file nobody reads is dead weight.
    """
    from quotesource import ingest, registry, rejections

    monkeypatch.setattr(ingest.time, "sleep", lambda *_: None)
    rejections.reject("PWasTAtR6Ns", "intelligent-design repackaging",
                      person="James A. Shapiro")
    source = registry.add_source("g", "G", "episodes", "", people=["X"])
    monkeypatch.setattr(ingest, "_fetch_youtube_episode",
                        lambda *a, **k: {"title": "t", "status": "captions",
                                         "duration": 60})

    row = ingest.add_episode("https://youtu.be/PWasTAtR6Ns", source, quiet=True)

    assert row["rejected"]["reason"] == "intelligent-design repackaging"


def test_it_warns_and_never_refuses(corpus, monkeypatch):
    """A judgement can be wrong, and the same video may be wanted for someone
    else. Matches the duplicate guard, which also reports and proceeds."""
    from quotesource import ingest, registry, rejections

    monkeypatch.setattr(ingest.time, "sleep", lambda *_: None)
    rejections.reject("PWasTAtR6Ns", "Denis Noble, not Shapiro")
    source = registry.add_source("g", "G", "episodes", "", people=["X"])
    fetched = []

    def fetch(*a, **k):
        fetched.append(1)
        return {"title": "t", "status": "captions", "duration": 60}

    monkeypatch.setattr(ingest, "_fetch_youtube_episode", fetch)
    row = ingest.add_episode("https://youtu.be/PWasTAtR6Ns", source, quiet=True)

    assert fetched == [1], "it must still have been added"
    assert row["status"] == "captions"


def test_an_unrejected_video_says_nothing(corpus, monkeypatch):
    from quotesource import ingest, registry

    monkeypatch.setattr(ingest.time, "sleep", lambda *_: None)
    source = registry.add_source("g", "G", "episodes", "", people=["X"])
    monkeypatch.setattr(ingest, "_fetch_youtube_episode",
                        lambda *a, **k: {"title": "t", "status": "captions",
                                         "duration": 60})

    row = ingest.add_episode("https://youtu.be/PWasTAtR6Ns", source, quiet=True)

    assert row["rejected"] is None


def test_the_listing_is_ordered_newest_first(corpus):
    """Same-day entries fall back to id, so the order is deterministic rather
    than whatever the dict happened to hold."""
    from quotesource import rejections

    rejections.reject("aaa", "one")
    rejections.reject("bbb", "two")
    # An older judgement, written directly so it carries a past date.
    stored = json.loads(rejections.rejections_path().read_text(encoding="utf-8"))
    stored["ccc"] = {"at": "2020-01-01", "reason": "long ago"}
    rejections.rejections_path().write_text(json.dumps(stored), encoding="utf-8")

    listed = rejections.list_rejections()

    assert [r["video_id"] for r in listed] == ["bbb", "aaa", "ccc"]


def test_re_rejecting_replaces_rather_than_accumulating(corpus):
    """The newest reading of a candidate is the one worth keeping."""
    from quotesource import rejections

    rejections.reject("aaa", "wrong person")
    rejections.reject("aaa", "actually it is intelligent-design repackaging")

    assert rejections.rejection_for("aaa")["reason"].startswith("actually")
