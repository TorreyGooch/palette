"""The pipeline outbox.

A clip destined for the video model is born on the same machine that runs
it, so shipping it to the desktop library and back is a round trip to move a
file four directories. The outbox is that shortcut - and deliberately not
the generator's own input folder, which fills with everything a pipeline is
fed and stops being curatable.
"""
import pytest

from quotesource import outbox


# ── configuration ─────────────────────────────────────────────────────────────

def test_off_unless_asked(monkeypatch):
    monkeypatch.delenv("QS_OUTBOX", raising=False)
    assert outbox.outbox_dir() is None
    assert outbox.deliver(["anything.m4a"]) == []


def test_env_sets_it_once(monkeypatch, tmp_path):
    monkeypatch.setenv("QS_OUTBOX", str(tmp_path / "narration"))
    assert outbox.outbox_dir() == tmp_path / "narration"


def test_explicit_argument_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("QS_OUTBOX", str(tmp_path / "from-env"))
    assert outbox.outbox_dir(str(tmp_path / "explicit")) == tmp_path / "explicit"


def test_blank_means_off(monkeypatch, tmp_path):
    """An empty flag must disable, not resolve to the current directory."""
    monkeypatch.setenv("QS_OUTBOX", str(tmp_path / "narration"))
    assert outbox.outbox_dir("") is None
    assert outbox.outbox_dir("   ") is None


def test_user_home_is_expanded(monkeypatch):
    monkeypatch.delenv("QS_OUTBOX", raising=False)
    got = outbox.outbox_dir("~/narration-outbox")
    assert "~" not in str(got)
    assert str(got).endswith("narration-outbox")


# ── delivery ──────────────────────────────────────────────────────────────────

def test_delivers_clip_and_manifest(tmp_path):
    src = tmp_path / "media"
    src.mkdir()
    clip = src / "qs_cut_EP_10_20.m4a"
    manifest = src / "qs_cut_EP_10_20.words.json"
    clip.write_bytes(b"AUDIO")
    manifest.write_text('{"words": []}', encoding="utf-8")
    out = tmp_path / "outbox"

    delivered = outbox.deliver([clip, manifest], str(out))

    assert len(delivered) == 2
    assert (out / clip.name).read_bytes() == b"AUDIO"
    assert (out / manifest.name).exists()


def test_creates_the_folder(tmp_path):
    clip = tmp_path / "clip.m4a"
    clip.write_bytes(b"x")
    out = tmp_path / "deep" / "nested" / "outbox"

    outbox.deliver([clip], str(out))
    assert (out / "clip.m4a").exists()


def test_copies_rather_than_moves(tmp_path):
    """The caller still has to hand the clip to whoever asked for it."""
    clip = tmp_path / "clip.m4a"
    clip.write_bytes(b"x")

    outbox.deliver([clip], str(tmp_path / "outbox"))
    assert clip.exists(), "moving would pull the file out from under the caller"


def test_missing_inputs_are_skipped(tmp_path):
    """A plain pull has no manifest; that is not an error."""
    clip = tmp_path / "clip.m4a"
    clip.write_bytes(b"x")
    absent = tmp_path / "clip.words.json"

    delivered = outbox.deliver([clip, absent], str(tmp_path / "outbox"))
    assert len(delivered) == 1


def test_no_partial_file_is_left_behind(tmp_path):
    """Anything watching the folder must never see a half-written clip."""
    clip = tmp_path / "clip.m4a"
    clip.write_bytes(b"y" * 4096)
    out = tmp_path / "outbox"

    outbox.deliver([clip], str(out))
    assert [p.name for p in out.iterdir()] == ["clip.m4a"], "a .part file survived"


def test_redelivery_overwrites(tmp_path):
    clip = tmp_path / "clip.m4a"
    out = tmp_path / "outbox"

    clip.write_bytes(b"first")
    outbox.deliver([clip], str(out))
    clip.write_bytes(b"second")
    outbox.deliver([clip], str(out))

    assert (out / "clip.m4a").read_bytes() == b"second"


# ── plumbing ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("func_name,module", [
    ("cut_quote", "quotesource.cut"),
    ("pull", "quotesource.pull"),
])
def test_both_producers_accept_an_outbox(func_name, module):
    import importlib
    import inspect

    func = getattr(importlib.import_module(module), func_name)
    params = inspect.signature(func).parameters
    assert "outbox" in params
    assert params["outbox"].default is None, "must be off unless asked"


@pytest.mark.parametrize("func_name,module", [
    ("cut_quote", "quotesource.cut"),
    ("pull", "quotesource.pull"),
])
def test_keeping_the_working_copy_is_the_default(func_name, module):
    """A remote caller has yet to download it; dropping it early loses it."""
    import importlib
    import inspect

    func = getattr(importlib.import_module(module), func_name)
    assert inspect.signature(func).parameters["keep_working_copy"].default is True


# ── who cleans up the unstaged working copy ───────────────────────────────────

class Args:
    def __init__(self, no_stage, outbox):
        self.no_stage = no_stage
        self.outbox = outbox


def test_unstaged_with_an_outbox_drops_the_duplicate(monkeypatch, tmp_path):
    from quotesource.cli import _keep_working_copy

    monkeypatch.delenv("QS_OUTBOX", raising=False)
    assert _keep_working_copy(Args(True, str(tmp_path / "out"))) is False


def test_unstaged_without_an_outbox_keeps_the_only_copy(monkeypatch):
    """The case that must never clean up: media/ holds the sole output."""
    from quotesource.cli import _keep_working_copy

    monkeypatch.delenv("QS_OUTBOX", raising=False)
    assert _keep_working_copy(Args(True, None)) is True


def test_env_outbox_counts_too(monkeypatch, tmp_path):
    from quotesource.cli import _keep_working_copy

    monkeypatch.setenv("QS_OUTBOX", str(tmp_path / "out"))
    assert _keep_working_copy(Args(True, None)) is False


def test_staged_runs_always_keep_it(monkeypatch, tmp_path):
    """Staged means the library references the file; never remove it."""
    from quotesource.cli import _keep_working_copy

    monkeypatch.setenv("QS_OUTBOX", str(tmp_path / "out"))
    assert _keep_working_copy(Args(False, None)) is True
    assert _keep_working_copy(Args(False, str(tmp_path / "out"))) is True


def test_cleanup_needs_a_delivery_not_just_a_setting(tmp_path):
    """keep_working_copy=False alone must not delete anything: the guard is
    that the clip actually landed in the outbox."""
    import inspect

    from quotesource import cut

    src = inspect.getsource(cut.cut_quote)
    assert "not keep_working_copy and delivered" in src, (
        "cleanup must be conditional on a successful delivery")
