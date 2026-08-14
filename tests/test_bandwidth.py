"""Not downloading more of YouTube than the job needs.

Every pull fetches a whole episode to extract seconds of it — a deliberate
trade, because yt-dlp section downloads stall. That makes the size of what
gets fetched, and how often it has to be fetched again, the thing to keep
honest. A median Lex episode is ~2.5 GB in av mode and ~130 MB in audio.
"""
import pytest

from quotesource import pull


# ── format ceiling ────────────────────────────────────────────────────────────

def test_audio_bitrate_is_capped_by_default(monkeypatch):
    monkeypatch.delenv("QS_AUDIO_MAX_ABR", raising=False)
    assert pull._max_abr() == 80


def test_audio_bitrate_is_tunable(monkeypatch):
    monkeypatch.setenv("QS_AUDIO_MAX_ABR", "128")
    assert pull._max_abr() == 128


# ── rate limiting ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("value,expected", [
    ("2M", 2 * 1024 ** 2),
    ("500K", 500 * 1024),
    ("1048576", 1048576),
    ("1.5M", int(1.5 * 1024 ** 2)),
])
def test_rate_limit_units(monkeypatch, value, expected):
    monkeypatch.setenv("QS_DOWNLOAD_RATE", value)
    assert pull._rate_limit() == expected


@pytest.mark.parametrize("value", ["", "   ", "fast", "abc"])
def test_bad_or_absent_rate_means_unlimited(monkeypatch, value):
    monkeypatch.setenv("QS_DOWNLOAD_RATE", value)
    assert pull._rate_limit() is None


def test_there_is_a_pause_between_requests_by_default(monkeypatch):
    monkeypatch.delenv("QS_DOWNLOAD_SLEEP_S", raising=False)
    assert pull._sleep_between() > 0


# ── cost estimate ─────────────────────────────────────────────────────────────

def test_av_is_far_more_expensive_than_audio():
    """The number that should make you pick audio unless you want picture."""
    two_hours = 2 * 3600
    audio = pull.estimate_mb(two_hours, "audio")
    av = pull.estimate_mb(two_hours, "av")
    assert av > audio * 20


# ── audio is the default; video is the deliberate exception ───────────────────

def test_pull_defaults_to_audio():
    """The expensive mode should be the one you have to ask for."""
    import inspect

    assert inspect.signature(pull.pull).parameters["mode"].default == "audio"


def test_cli_defaults_to_audio():
    from quotesource.cli import main

    with pytest.raises(SystemExit):
        main(["pull", "--help"])  # smoke: parser builds


def test_cli_parser_default(monkeypatch):
    import argparse

    from quotesource import cli

    captured = {}
    real = argparse.ArgumentParser.add_argument

    def spy(self, *args, **kw):
        if args and args[0] == "--mode" and "choices" in kw:
            captured["default"] = kw.get("default")
        return real(self, *args, **kw)

    monkeypatch.setattr(argparse.ArgumentParser, "add_argument", spy)
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    assert captured.get("default") == "audio"


def test_api_defaults_to_audio():
    """A request that omits mode must not download gigabytes."""
    import inspect

    from palette_app import main

    src = inspect.getsource(main.qs_pull)
    assert 'body.get("mode", "audio")' in src
    assert 'body.get("mode", "av")' not in src


def test_estimate_is_none_without_a_duration():
    assert pull.estimate_mb(None, "audio") is None
    assert pull.estimate_mb(0, "av") is None


def test_estimate_tracks_the_configured_ceiling(monkeypatch):
    monkeypatch.setenv("QS_AUDIO_MAX_ABR", "160")
    high = pull.estimate_mb(3600, "audio")
    monkeypatch.setenv("QS_AUDIO_MAX_ABR", "80")
    low = pull.estimate_mb(3600, "audio")
    assert high == pytest.approx(low * 2)


# ── eviction ──────────────────────────────────────────────────────────────────

@pytest.fixture
def cache(tmp_path, monkeypatch):
    d = tmp_path / "cache"
    d.mkdir()
    monkeypatch.setattr(pull, "_cache_dir", lambda: d)
    return d


def write(path, mb, atime):
    import os

    path.write_bytes(b"\0" * int(mb * 1024 * 1024))
    os.utime(path, (atime, atime))


def test_video_is_evicted_before_audio(cache, monkeypatch):
    """Plain LRU lets one video pull evict several episodes' audio, and each
    of those is a fresh full-episode download next time."""
    monkeypatch.setattr(pull, "_cache_gb", lambda: 12 / 1024)  # 12 MB cap

    write(cache / "old_audio.m4a", 4, 1_000)      # oldest, but cheap to keep
    write(cache / "new_video.mp4", 8, 9_000)      # newest, but expensive
    write(cache / "mid_audio.m4a", 4, 5_000)

    pull._evict_cache()

    names = sorted(p.name for p in cache.iterdir())
    assert "new_video.mp4" not in names, "video should go first"
    assert "old_audio.m4a" in names and "mid_audio.m4a" in names


def test_audio_still_evicted_when_that_is_not_enough(cache, monkeypatch):
    monkeypatch.setattr(pull, "_cache_gb", lambda: 5 / 1024)  # 5 MB cap

    write(cache / "a_old.m4a", 4, 1_000)
    write(cache / "b_new.m4a", 4, 9_000)

    pull._evict_cache()

    remaining = [p.name for p in cache.iterdir()]
    assert remaining == ["b_new.m4a"], "oldest audio goes when audio must go"


def test_nothing_is_touched_under_the_cap(cache, monkeypatch):
    monkeypatch.setattr(pull, "_cache_gb", lambda: 100 / 1024)

    write(cache / "keep.m4a", 4, 1_000)
    write(cache / "keep.mp4", 8, 2_000)

    pull._evict_cache()
    assert len(list(cache.iterdir())) == 2
