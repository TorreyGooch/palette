"""Episode audio is kept, not cached.

An eviction costs a fresh full-episode download the next time anyone cuts
from it, which is the traffic that draws rate limiting. At ~32 MB an episode
the whole corpus is ~52 GB, so keeping is affordable and evicting is not.
Video keeps its own, much smaller budget: ~20x the size, and only wanted
when you actually need the picture.
"""
import pytest

from quotesource import pull


# ── finding kept audio ────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", ["audio.m4a", "audio.webm", "audio.opus"])
def test_kept_audio_is_found_in_any_container(tmp_path, name):
    (tmp_path / name).write_bytes(b"x")
    assert pull.stored_audio(tmp_path) == tmp_path / name


def test_no_audio_means_none(tmp_path):
    (tmp_path / "metadata.json").write_text("{}", encoding="utf-8")
    (tmp_path / "transcript.json").write_text("{}", encoding="utf-8")
    assert pull.stored_audio(tmp_path) is None


def test_transcript_json_is_not_mistaken_for_audio(tmp_path):
    """audio.* globs widely; only real containers should match."""
    (tmp_path / "audio.json").write_text("{}", encoding="utf-8")
    assert pull.stored_audio(tmp_path) is None


def test_missing_episode_dir_is_survivable():
    assert pull.stored_audio(None) is None


# ── budgets are separate ──────────────────────────────────────────────────────

def test_audio_ceiling_is_generous_by_default(monkeypatch):
    """Sized for archiving podcast back catalogues, not just incidental pulls:
    a feed's worth of ~50 MB enclosures runs past the old 40 GB in one go."""
    monkeypatch.delenv("QS_AUDIO_STORE_GB", raising=False)
    assert pull._audio_store_gb() == 80


def test_video_cache_is_small_by_default(monkeypatch):
    """Sharing one budget let a 446 MB video pull evict nine episodes."""
    monkeypatch.delenv("QS_PULL_CACHE_GB", raising=False)
    assert pull._cache_gb() < pull._audio_store_gb() / 4


# ── eviction across the store ─────────────────────────────────────────────────

@pytest.fixture
def corpus(tmp_path, monkeypatch):
    monkeypatch.setenv("QUOTESOURCE_DATA", str(tmp_path))
    (tmp_path / "episodes").mkdir()
    return tmp_path


def add_episode(corpus, source, ep_id, mb, atime):
    import os

    ep = corpus / "episodes" / source / ep_id
    ep.mkdir(parents=True)
    path = ep / "audio.m4a"
    path.write_bytes(b"\0" * int(mb * 1024 * 1024))
    os.utime(path, (atime, atime))
    return path


def test_nothing_evicted_under_the_ceiling(corpus, monkeypatch):
    monkeypatch.setattr(pull, "_audio_store_gb", lambda: 100 / 1024)
    a = add_episode(corpus, "jbp", "EP1", 4, 1_000)
    b = add_episode(corpus, "lex", "EP2", 4, 2_000)

    pull._evict_audio_store()
    assert a.exists() and b.exists()


def test_least_recently_used_goes_first(corpus, monkeypatch):
    monkeypatch.setattr(pull, "_audio_store_gb", lambda: 9 / 1024)
    old = add_episode(corpus, "jbp", "OLD", 4, 1_000)
    mid = add_episode(corpus, "jbp", "MID", 4, 5_000)
    new = add_episode(corpus, "lex", "NEW", 4, 9_000)

    pull._evict_audio_store()

    assert not old.exists(), "oldest use should go first"
    assert mid.exists() and new.exists()


def test_eviction_spans_sources(corpus, monkeypatch):
    """The ceiling is on the store as a whole, not per channel."""
    monkeypatch.setattr(pull, "_audio_store_gb", lambda: 5 / 1024)
    a = add_episode(corpus, "jbp", "EP1", 4, 1_000)
    b = add_episode(corpus, "lex", "EP2", 4, 9_000)

    pull._evict_audio_store()
    assert not a.exists() and b.exists()


def test_missing_episodes_dir_is_survivable(tmp_path, monkeypatch):
    monkeypatch.setenv("QUOTESOURCE_DATA", str(tmp_path))
    pull._evict_audio_store()  # must not raise


# ── the whole point ───────────────────────────────────────────────────────────

def test_cut_prefers_kept_audio_over_the_network(tmp_path, monkeypatch):
    """A second quote from the same episode must cost no bandwidth."""
    from quotesource import cut

    ep = tmp_path / "EP1"
    ep.mkdir()
    kept = ep / "audio.m4a"
    kept.write_bytes(b"AUDIO")

    def explode(*a, **k):
        raise AssertionError("downloaded when kept audio was available")

    monkeypatch.setattr(pull, "_get_full_media", explode)
    assert cut._source_media("EP1", ep) == kept
