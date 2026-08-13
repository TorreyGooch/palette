"""Job lifetime and the client/server capability handshake.

Both are consequences of the split: jobs used to die with a short-lived
process, and there used to be only one copy of the code.
"""
import time

import pytest
from fastapi import HTTPException


# ── job store ─────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def clean_jobs():
    from palette_app import main

    main._pull_jobs.clear()
    yield
    main._pull_jobs.clear()


def make_job(done=False, finished_ago=0.0, **extra):
    from palette_app import main

    job_id, job = main._new_job(**extra)
    if done:
        job["done"] = True
        job["finished_at"] = time.time() - finished_ago
    return job_id, job


def test_job_ids_carry_the_boot_id():
    from palette_app import main

    job_id, _ = make_job()
    assert job_id.startswith(f"{main.BOOT_ID}-")


def test_finished_jobs_expire(monkeypatch):
    from palette_app import main

    monkeypatch.setattr(main, "JOB_TTL_S", 60.0)
    old, _ = make_job(done=True, finished_ago=120.0)
    recent, _ = make_job(done=True, finished_ago=5.0)

    make_job()  # any new job prunes

    assert old not in main._pull_jobs, "a finished job past its TTL should go"
    assert recent in main._pull_jobs


def test_running_jobs_are_never_pruned(monkeypatch):
    from palette_app import main

    monkeypatch.setattr(main, "JOB_TTL_S", 0.0)
    monkeypatch.setattr(main, "JOB_MAX", 1)
    running, _ = make_job()          # not done
    make_job(done=True, finished_ago=999.0)

    make_job()

    assert running in main._pull_jobs, "work in flight must survive pruning"


def test_store_is_capped(monkeypatch):
    from palette_app import main

    monkeypatch.setattr(main, "JOB_TTL_S", 1e9)  # TTL must not do the work
    monkeypatch.setattr(main, "JOB_MAX", 5)
    for i in range(20):
        make_job(done=True, finished_ago=100 - i)

    assert len(main._pull_jobs) <= 6, "unbounded growth is the leak being fixed"


def test_polling_a_live_job_returns_it():
    from palette_app import main

    job_id, job = make_job()
    assert main.qs_pull_status(job_id) is job


def test_unknown_id_from_this_process_is_404():
    from palette_app import main

    with pytest.raises(HTTPException) as e:
        main.qs_pull_status(f"{main.BOOT_ID}-deadbeef")
    assert e.value.status_code == 404


def test_id_from_an_earlier_process_says_the_server_restarted():
    from palette_app import main

    with pytest.raises(HTTPException) as e:
        main.qs_pull_status("0ldb00t-deadbeef")
    assert e.value.status_code == 410, "410 Gone: it will never come back"
    assert "restarted" in e.value.detail


# ── capability handshake ──────────────────────────────────────────────────────

def test_status_advertises_capabilities(monkeypatch, library):
    from palette_app import main

    monkeypatch.delenv("QS_REMOTE", raising=False)
    monkeypatch.setattr(main, "_root", lambda: library)
    monkeypatch.setattr("quotesource.status.corpus_status", lambda: {"sources": []})

    block = main.qs_status()["palette"]
    assert block["api"] == main.API_VERSION
    assert block["boot_id"] == main.BOOT_ID
    for required in ("stage", "discard"):
        assert required in block["capabilities"]


@pytest.fixture
def caps(monkeypatch):
    """Control what the remote claims, bypassing the network."""
    from palette_app import qs_remote

    monkeypatch.setenv("QS_REMOTE", "http://server:7862")
    monkeypatch.setattr(qs_remote, "_caps", set())
    monkeypatch.setattr(qs_remote, "_caps_base", None)
    monkeypatch.setattr(qs_remote, "_caps_at", 0.0)

    def declare(capabilities):
        monkeypatch.setattr(qs_remote, "get", lambda *a, **k: {
            "palette": {"capabilities": list(capabilities)}})
    return declare


def test_supports_reads_the_advertised_list(caps):
    from palette_app import qs_remote

    caps(["stage", "discard"])
    assert qs_remote.remote_supports("stage")
    assert not qs_remote.remote_supports("teleportation")


def test_old_server_advertises_nothing(caps):
    """A server predating capabilities returns no palette block at all."""
    from palette_app import qs_remote

    caps([])
    assert qs_remote.remote_capabilities() == set()
    assert not qs_remote.remote_supports("stage")


def test_unreachable_remote_reports_no_capabilities(monkeypatch):
    from palette_app import qs_remote

    monkeypatch.setenv("QS_REMOTE", "http://server:7862")
    monkeypatch.setattr(qs_remote, "_caps_base", None)
    monkeypatch.setattr(qs_remote, "_caps_at", 0.0)

    def unreachable(*a, **k):
        raise qs_remote.RemoteError("down", 503)

    monkeypatch.setattr(qs_remote, "get", unreachable)
    assert qs_remote.remote_capabilities() == set(), "fail soft, not loud"


def test_capabilities_are_cached(caps, monkeypatch):
    from palette_app import qs_remote

    calls = []
    monkeypatch.setattr(qs_remote, "get", lambda *a, **k: (
        calls.append(1), {"palette": {"capabilities": ["stage"]}})[1])

    qs_remote.remote_capabilities()
    qs_remote.remote_capabilities()
    qs_remote.remote_capabilities()
    assert len(calls) == 1, "should not hit the network on every pull"


def test_no_remote_means_no_capabilities(monkeypatch):
    from palette_app import qs_remote

    monkeypatch.delenv("QS_REMOTE", raising=False)
    assert qs_remote.remote_capabilities() == set()
