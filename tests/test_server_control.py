"""Starting and stopping the corpus API from the desktop app.

It runs on demand so it does not hold memory and GPU that generation work
needs, which means the app has to be able to start something that is not
running - ssh, since an always-on supervisor would be the thing being
avoided. Everything here is about that shelling out behaving.
"""
import json
import subprocess

import pytest
from fastapi import HTTPException


@pytest.fixture
def ssh(monkeypatch):
    """Capture the ssh invocation and control what it returns."""
    from palette_app import main

    monkeypatch.setenv("QS_SERVER_SSH", "torrey@10.0.0.1")
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, fake_run.stdout, fake_run.stderr)

    fake_run.stdout = json.dumps({"running": True, "port": 7862, "rss_mb": 210})
    fake_run.stderr = ""
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(main, "_server_ssh", lambda: "torrey@10.0.0.1")
    return fake_run, calls


def test_status_parses_the_scripts_json(ssh):
    from palette_app import main

    state = main.qs_server_status()
    assert state["running"] is True
    assert state["rss_mb"] == 210
    assert state["action"] == "status"


def test_a_stale_server_is_visible_in_the_status(ssh):
    """A pull does not change what a running process serves; only a restart
    does. status once reported the repo's HEAD as the running version, which
    made a server 33 hours behind look current."""
    fake_run, _ = ssh
    fake_run.stdout = json.dumps({
        "running": True, "port": 7862, "rss_mb": 210,
        "version": "a29a420", "repo_version": "d94cf38", "stale": True,
    })

    from palette_app import main

    state = main.qs_server_status()
    assert state["stale"] is True
    assert state["version"] != state["repo_version"]


def test_a_current_server_is_not_flagged(ssh):
    fake_run, _ = ssh
    fake_run.stdout = json.dumps({
        "running": True, "port": 7862, "rss_mb": 210,
        "version": "d94cf38", "repo_version": "d94cf38", "stale": False,
    })

    from palette_app import main

    state = main.qs_server_status()
    assert state["stale"] is False
    assert state["version"] == state["repo_version"]
    assert state["host"] == "torrey@10.0.0.1"


@pytest.mark.parametrize("action", ["start", "stop", "restart", "status"])
def test_allowed_actions_reach_the_script(ssh, action):
    from palette_app import main

    _, calls = ssh
    main.qs_server_control({"action": action})
    assert calls[-1][-1].endswith(f"server-app.sh {action}")


@pytest.mark.parametrize("action", [
    "", "delete", "start; rm -rf ~", "status && curl evil.example",
    "$(whoami)", "../../bin/sh",
])
def test_anything_not_whitelisted_is_refused(ssh, action):
    """Nothing from the request may reach a shell."""
    from palette_app import main

    _, calls = ssh
    with pytest.raises(HTTPException) as e:
        main.qs_server_control({"action": action})
    assert e.value.status_code == 400
    assert not calls, "refused actions must not invoke ssh at all"


def test_note_from_stderr_is_surfaced(ssh):
    from palette_app import main

    fake, _ = ssh
    fake.stderr = "already running on 7862\n"
    assert main.qs_server_status()["note"] == "already running on 7862"


def test_unparseable_output_becomes_502(ssh):
    from palette_app import main

    fake, _ = ssh
    fake.stdout = "Permission denied (publickey)."
    fake.stderr = "Permission denied (publickey)."
    with pytest.raises(HTTPException) as e:
        main.qs_server_status()
    assert e.value.status_code == 502
    assert "publickey" in e.value.detail


def test_timeout_becomes_504(monkeypatch):
    from palette_app import main

    monkeypatch.setattr(main, "_server_ssh", lambda: "torrey@10.0.0.1")

    def slow(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 45)

    monkeypatch.setattr(subprocess, "run", slow)
    with pytest.raises(HTTPException) as e:
        main.qs_server_status()
    assert e.value.status_code == 504


def test_missing_ssh_binary_is_reported(monkeypatch):
    from palette_app import main

    monkeypatch.setattr(main, "_server_ssh", lambda: "torrey@10.0.0.1")

    def no_ssh(cmd, **kw):
        raise FileNotFoundError("ssh")

    monkeypatch.setattr(subprocess, "run", no_ssh)
    with pytest.raises(HTTPException) as e:
        main.qs_server_status()
    assert e.value.status_code == 500
    assert "ssh" in e.value.detail


def test_no_server_configured(monkeypatch):
    from palette_app import main

    monkeypatch.delenv("QS_SERVER_SSH", raising=False)
    monkeypatch.delenv("QS_REMOTE", raising=False)
    with pytest.raises(HTTPException) as e:
        main.qs_server_status()
    assert e.value.status_code == 409


def test_host_is_inferred_from_qs_remote(monkeypatch):
    from palette_app import main

    monkeypatch.delenv("QS_SERVER_SSH", raising=False)
    monkeypatch.setenv("QS_REMOTE", "http://100.102.79.115:7862")
    monkeypatch.setenv("QS_SERVER_USER", "torrey")
    assert main._server_ssh() == "torrey@100.102.79.115"


# ── saying which side failed ──────────────────────────────────────────────────

def test_an_ssh_failure_with_no_output_says_it_was_the_transport(ssh):
    """"no output" reads as though the server answered, and answered emptily.

    It usually means ssh never arrived - no key, no route, wrong host - which
    is a fault on this side. A session chasing that message went looking at
    the corpus server, which was fine.
    """
    from palette_app import main

    fake_run, _ = ssh
    fake_run.stdout = ""
    fake_run.stderr = ""

    with pytest.raises(HTTPException) as raised:
        main.qs_server_status()

    assert raised.value.status_code == 502
    assert "transport failed rather than the server" in raised.value.detail
    # The exit status is the one fact always available to point at ssh.
    assert "ssh exited" in raised.value.detail


def test_ssh_stderr_is_still_preferred_when_there_is_any(ssh):
    """When ssh does explain itself, that explanation is the whole answer."""
    from palette_app import main

    fake_run, _ = ssh
    fake_run.stdout = ""
    fake_run.stderr = "torrey@10.0.0.1: Permission denied (publickey)."

    with pytest.raises(HTTPException) as raised:
        main.qs_server_status()

    assert "Permission denied (publickey)" in raised.value.detail


# ── which ssh, and why it matters ─────────────────────────────────────────────

def test_ssh_is_chosen_rather_than_inherited_from_path(monkeypatch):
    """Windows has two ssh clients and they do not share credentials.

    The OpenSSH client in System32 talks to the Windows ssh-agent; Git for
    Windows ships its own under usr/bin which does not, and answers
    "Permission denied (publickey)" for a key the agent is holding. Which one
    wins was decided by whatever PATH the app was launched with, so the shell
    used to start it silently decided whether the server controls worked —
    and starting it from Git Bash broke them for everyone.
    """
    from palette_app import main

    monkeypatch.delenv("QS_SSH", raising=False)
    monkeypatch.setattr(main.os, "name", "nt")
    monkeypatch.setenv("SystemRoot", r"C:\Windows")
    monkeypatch.setattr(main.Path, "exists", lambda self: True)

    assert main._ssh_binary().endswith(r"System32\OpenSSH\ssh.exe")


def test_an_explicit_ssh_wins(monkeypatch):
    from palette_app import main

    monkeypatch.setenv("QS_SSH", "/usr/bin/ssh")
    assert main._ssh_binary() == "/usr/bin/ssh"


def test_without_the_windows_client_it_falls_back_to_path(monkeypatch):
    from palette_app import main

    monkeypatch.delenv("QS_SSH", raising=False)
    monkeypatch.setattr(main.os, "name", "posix")
    assert main._ssh_binary() == "ssh"


def test_the_chosen_binary_is_the_one_invoked(ssh, monkeypatch):
    from palette_app import main

    monkeypatch.setenv("QS_SSH", "/custom/ssh")
    _, calls = ssh
    main.qs_server_status()
    assert calls[-1][0] == "/custom/ssh"


def test_a_missing_ssh_names_what_it_looked_for(monkeypatch):
    """"ssh is not available" gave no way to tell which one was missing."""
    import subprocess as sp

    from palette_app import main

    monkeypatch.setenv("QS_SSH", "/nowhere/ssh")
    monkeypatch.setattr(main, "_server_ssh", lambda: "torrey@10.0.0.1")

    def missing(*a, **k):
        raise FileNotFoundError()

    monkeypatch.setattr(sp, "run", missing)
    with pytest.raises(HTTPException) as raised:
        main.qs_server_status()
    assert "/nowhere/ssh" in raised.value.detail
