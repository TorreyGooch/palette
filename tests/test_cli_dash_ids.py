"""YouTube ids that begin with a dash.

An id is 11 characters of [A-Za-z0-9_-], so a fair share start with one, and
argparse reads those as unknown options: `qs words -RXD4bTuFTo --range 10 20`
fails with "the following arguments are required: episode_id". Both
-RXD4bTuFTo and --xKsIgv7tE are real episodes in this corpus.
"""
import pytest

from quotesource.cli import _protect_leading_dash_ids as protect


def test_single_dash_id_is_protected():
    assert protect(["words", "-RXD4bTuFTo", "--range", "10", "20"]) == [
        "words", "--range", "10", "20", "--", "-RXD4bTuFTo"]


def test_double_dash_id_is_protected():
    assert protect(["words", "--xKsIgv7tE"]) == ["words", "--", "--xKsIgv7tE"]


def test_the_marker_goes_last_so_later_flags_still_parse():
    """`--` in front of the id would swallow every flag after it."""
    out = protect(["cut", "-RXD4bTuFTo", "--range", "10", "20", "--pretty"])
    assert out.index("--") == len(out) - 2
    assert "--pretty" in out[:out.index("--")]


def test_ordinary_ids_are_untouched():
    argv = ["words", "PWasTAtR6Ns", "--range", "10", "20"]
    assert protect(argv) == argv


def test_real_flags_are_not_mistaken_for_ids():
    """Nothing in this CLI has the shape of an id, and it must stay that way."""
    argv = ["pull", "abc", "--pretty", "--no-stage", "--outbox", "/tmp/x",
            "--min-duration", "30m", "--limit", "5", "--quiet", "--batch"]
    assert protect(argv) == argv


def test_an_explicit_separator_is_respected():
    """If the caller already passed --, do not add a second one."""
    argv = ["words", "--", "-RXD4bTuFTo"]
    assert protect(argv) == argv


def test_only_the_first_id_is_moved():
    """These commands take one episode id, so the first match is the one that
    matters; a second would be a usage error either way."""
    out = protect(["cut", "-RXD4bTuFTo", "-VeZp2d7mDs"])
    assert out.count("--") == 1
    assert out == ["cut", "-VeZp2d7mDs", "--", "-RXD4bTuFTo"]


def test_wrong_length_is_not_treated_as_an_id():
    for tok in ["-short", "-waytoolongtobeanid", "--range"]:
        assert protect(["words", tok]) == ["words", tok]


def test_the_parser_now_accepts_a_dash_id():
    from quotesource import cli

    with pytest.raises(SystemExit):      # --help exits; proves parsing got there
        cli.main(["words", "--help"])


def test_end_to_end_parse_of_a_dash_id(monkeypatch):
    """The whole point: this used to die with SystemExit(2), a usage error,
    before the command ever ran."""
    from quotesource import cli

    seen = {}

    def fake(args):
        seen["episode"] = args.episode_id
        seen["range"] = args.range

    # main() resolves both names when it builds the parser, so patching the
    # module attributes here is enough; the corpus guard would otherwise exit
    # first and tell us nothing about parsing.
    monkeypatch.setattr(cli, "cmd_words", fake)
    monkeypatch.setattr(cli, "_warn_if_corpus_elsewhere", lambda *_: None)

    try:
        cli.main(["words", "-RXD4bTuFTo", "--range", "10", "20"])
    except SystemExit as e:               # pragma: no cover - diagnostic
        pytest.fail(f"dash id still rejected: SystemExit({e.code})")
    assert seen["episode"] == "-RXD4bTuFTo"
    assert seen["range"] == [10.0, 20.0]
