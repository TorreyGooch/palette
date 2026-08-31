"""`server-app.sh update`: bring the corpus box forward from its own side.

The desktop's deploy.ps1 pushes and then pulls over ssh, which needs that
machine switched on and someone to run it. This is the same step initiated
from the server, and the things worth pinning down are the refusals: it must
not fast-forward over local edits, and it must not *start* an app that was
deliberately left stopped.

Linux only, and that is not laziness. server-app.sh is the script for one
specific box - status_json reads /proc/meminfo - so it is exercised where it
is deployed. The ubuntu CI job is where this actually runs.
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux") or shutil.which("bash") is None,
    reason="server-app.sh targets the Linux corpus server",
)

SCRIPT = Path(__file__).resolve().parent.parent / "server-app.sh"


def git(cwd, *args, check=True):
    env = dict(os.environ,
               GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com")
    return subprocess.run(["git", "-C", str(cwd), *args], env=env,
                          capture_output=True, text=True, check=check)


def head(cwd):
    return git(cwd, "rev-parse", "--short", "HEAD").stdout.strip()


@pytest.fixture
def deployment(tmp_path):
    """An origin, a machine that pushes to it, and a checkout that follows."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", str(origin)], check=True)

    author = tmp_path / "author"
    subprocess.run(["git", "clone", "-q", str(origin), str(author)], check=True)
    (author / "app.py").write_text("v1\n")
    git(author, "add", "-A")
    git(author, "commit", "-qm", "first")
    git(author, "push", "-q", "origin", "HEAD:refs/heads/main")

    server = tmp_path / "server"
    subprocess.run(["git", "clone", "-q", "-b", "main", str(origin), str(server)],
                   check=True)
    return author, server


def run_update(server, **overrides):
    env = dict(os.environ, PALETTE_REPO=str(server),
               # A port nothing is listening on, so the script takes the
               # "not running" branch and never reaches tmux.
               PALETTE_PORT="59999",
               PALETTE_URL_FILE=str(server / ".api-url"))
    env.update(overrides)
    return subprocess.run(["bash", str(SCRIPT), "update"],
                          env=env, capture_output=True, text=True)


def publish(author, text):
    (author / "app.py").write_text(text)
    git(author, "add", "-A")
    git(author, "commit", "-qm", "next")
    git(author, "push", "-q", "origin", "HEAD:refs/heads/main")


# ── the ordinary case ─────────────────────────────────────────────────────────

def test_update_fast_forwards_the_checkout(deployment):
    author, server = deployment
    was = head(server)
    publish(author, "v2\n")

    result = run_update(server)

    assert result.returncode == 0, result.stderr
    assert head(server) != was
    assert (server / "app.py").read_text() == "v2\n"
    assert "updated" in result.stderr


def test_an_already_current_checkout_says_so_and_changes_nothing(deployment):
    _, server = deployment
    was = head(server)

    result = run_update(server)

    assert result.returncode == 0, result.stderr
    assert head(server) == was
    assert "already on" in result.stderr


def test_it_still_reports_status_as_json(deployment):
    """The desktop app parses this output; an update must not break that."""
    import json

    author, server = deployment
    publish(author, "v2\n")
    result = run_update(server)

    state = json.loads(result.stdout)
    assert state["repo_version"] == head(server)
    assert state["running"] is False


# ── the refusals, which are the point ─────────────────────────────────────────

def test_a_dirty_tree_is_refused_and_nothing_moves(deployment):
    """Same commit, different code is the failure this guards against."""
    author, server = deployment
    was = head(server)
    publish(author, "v2\n")
    (server / "app.py").write_text("edited here to get something working\n")

    result = run_update(server)

    assert result.returncode != 0
    assert "dirty" in result.stderr
    assert head(server) == was, "a refused update must not move HEAD"


def test_a_diverged_checkout_is_refused_rather_than_merged(deployment):
    author, server = deployment
    publish(author, "v2\n")
    (server / "local.py").write_text("only here\n")
    git(server, "add", "-A")
    git(server, "commit", "-qm", "local work")
    was = head(server)

    result = run_update(server)

    assert result.returncode != 0
    assert "fast-forward" in result.stderr
    assert head(server) == was


def test_an_update_never_starts_an_app_that_was_not_running(deployment):
    """On demand is the whole design: updating code is not a request to serve."""
    author, server = deployment
    publish(author, "v2\n")

    result = run_update(server)

    assert "nothing to restart" in result.stderr
    assert "started on" not in result.stderr
