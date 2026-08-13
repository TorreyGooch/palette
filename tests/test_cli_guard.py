"""The CLI cannot proxy to QS_REMOTE, so it must say the corpus moved.

Without this, every corpus command returned zero results as though the
corpus were merely empty — which reads like data loss rather than a
relocation.
"""
import pytest

from quotesource.cli import _warn_if_corpus_elsewhere


def test_corpus_free_commands_are_never_blocked(tmp_path, monkeypatch):
    monkeypatch.setenv("QUOTESOURCE_DATA", str(tmp_path))
    for command in ("sources", "status"):
        _warn_if_corpus_elsewhere(command)  # must not raise


def test_blocks_when_there_is_no_corpus(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("QUOTESOURCE_DATA", str(tmp_path))
    monkeypatch.delenv("QS_REMOTE", raising=False)

    with pytest.raises(SystemExit) as e:
        _warn_if_corpus_elsewhere("search")
    assert e.value.code == 1
    assert "No corpus" in capsys.readouterr().err


def test_empty_scaffolding_is_not_a_corpus(tmp_path, monkeypatch):
    """ensure_root() creates an empty episodes/, so existence proves nothing."""
    monkeypatch.setenv("QUOTESOURCE_DATA", str(tmp_path))
    (tmp_path / "episodes").mkdir()

    with pytest.raises(SystemExit):
        _warn_if_corpus_elsewhere("search")


def test_allows_a_corpus_with_sources(tmp_path, monkeypatch):
    monkeypatch.setenv("QUOTESOURCE_DATA", str(tmp_path))
    (tmp_path / "episodes" / "jordanpeterson").mkdir(parents=True)

    _warn_if_corpus_elsewhere("search")  # must not raise


def test_points_at_the_remote_when_one_is_configured(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("QUOTESOURCE_DATA", str(tmp_path))
    monkeypatch.setenv("QS_REMOTE", "http://gpu-box:7862")

    with pytest.raises(SystemExit):
        _warn_if_corpus_elsewhere("cut")
    err = capsys.readouterr().err
    assert "http://gpu-box:7862" in err
    assert "cannot proxy" in err
