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


# ── being current is the promise, not "did HEAD move" ─────────────────────────

FAKE_APP = '''
import json, sys
from http.server import BaseHTTPRequestHandler, HTTPServer

version, port = sys.argv[1], int(sys.argv[2])


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"palette": {"version": version}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


HTTPServer(("127.0.0.1", port), Handler).serve_forever()
'''


def free_port():
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def stale_app(tmp_path):
    """An app that is up, and serving a build the checkout has moved past."""
    import time

    script = tmp_path / "fake_app.py"
    script.write_text(FAKE_APP)
    port = free_port()
    proc = subprocess.Popen([sys.executable, str(script), "0ldbuild", str(port)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        try:
            import urllib.request
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/qs/status",
                                   timeout=1).read()
            break
        except Exception:
            time.sleep(0.05)
    else:
        proc.kill()
        pytest.fail("fake app never came up")
    yield port
    proc.kill()
    proc.wait()


def test_a_running_app_on_an_older_build_is_restarted_even_when_head_did_not_move(
        deployment, stale_app, tmp_path):
    """The state `stale` reports: someone pulled by hand and never restarted.

    Answering "already on X" and walking away would leave the box serving the
    old module while claiming to be current - which is the whole failure this
    script family exists to prevent.

    Only the decision is asserted. Actually restarting needs tmux and a real
    app; the fake one here keeps its socket, so stop_app times out and
    start_app then finds something already listening. That costs this test
    about ten seconds and buys the branch being exercised at all.
    """
    _, server = deployment
    url_file = tmp_path / "api-url"
    url_file.write_text(f"http://127.0.0.1:{stale_app}\n")

    result = run_update(server, PALETTE_PORT=str(stale_app),
                        PALETTE_URL_FILE=str(url_file),
                        PALETTE_PYTHON=sys.executable)

    assert "already on" in result.stderr, result.stderr
    assert "0ldbuild" in result.stderr, "it should say what is actually running"
    assert "restarting onto" in result.stderr


def test_an_app_already_serving_the_current_build_is_left_alone(
        deployment, tmp_path):
    """Restarting a correct process would drop in-flight jobs for nothing."""
    import time

    _, server = deployment
    current = head(server)

    script = tmp_path / "fake_app.py"
    script.write_text(FAKE_APP)
    port = free_port()
    proc = subprocess.Popen([sys.executable, str(script), current, str(port)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            try:
                import urllib.request
                urllib.request.urlopen(f"http://127.0.0.1:{port}/api/qs/status",
                                       timeout=1).read()
                break
            except Exception:
                time.sleep(0.05)
        url_file = tmp_path / "api-url"
        url_file.write_text(f"http://127.0.0.1:{port}\n")

        result = run_update(server, PALETTE_PORT=str(port),
                            PALETTE_URL_FILE=str(url_file),
                            PALETTE_PYTHON=sys.executable)
    finally:
        proc.kill()
        proc.wait()

    assert result.returncode == 0, result.stderr
    assert f"already serving {current}" in result.stderr
    assert "restarting" not in result.stderr
