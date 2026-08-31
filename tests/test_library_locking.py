"""Two writers must not lose each other's work, or leave half a database.

library.json *is* the media database — every item, tag and palette. It was
loaded, modified in memory and written back with a plain overwrite, by three
sessions sharing one app. Two failures follow from that, and they are
different:

  a torn write   an overwrite truncates before it writes, so a crash or a
                 full disk leaves a fragment and the library is gone.
  a lost update  two writers each load, each change their own copy, and the
                 second save silently discards the first change.

The second is the nastier one, because it reports success. A tag that was
dropped this way is indistinguishable from a tag that was never applied.
"""
import json
import os
import threading

import pytest

from palette_app.library import (library_lock, load_library, save_library)


@pytest.fixture
def lib(tmp_path):
    from palette_app.library import create_library

    create_library(tmp_path)
    return tmp_path


def read_raw(root):
    return json.loads((root / "library.json").read_text(encoding="utf-8"))


# -- the write itself ---------------------------------------------------------

def test_a_save_replaces_rather_than_truncating(lib):
    """The old file must survive until the new one is complete."""
    save_library(lib, {"items": [{"id": "a"}], "palettes": []})
    assert read_raw(lib)["items"] == [{"id": "a"}]


def test_a_failed_write_leaves_the_previous_database_intact(lib, monkeypatch):
    """A dump that dies half way must not take the library with it."""
    save_library(lib, {"items": [{"id": "keep"}], "palettes": []})
    before = (lib / "library.json").read_bytes()

    def die(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(json, "dump", die)
    with pytest.raises(OSError):
        save_library(lib, {"items": [{"id": "new"}], "palettes": []})

    assert (lib / "library.json").read_bytes() == before


def test_a_failed_write_leaves_no_rubbish_behind(lib, monkeypatch):
    def die(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(json, "dump", die)
    with pytest.raises(OSError):
        save_library(lib, {"items": [], "palettes": []})

    leftovers = [p.name for p in lib.iterdir() if p.name.startswith(".library-")]
    assert leftovers == []


def test_the_temporary_file_shares_the_directory(lib, monkeypatch):
    """os.replace is only atomic within one filesystem."""
    seen = {}
    real = os.replace

    def watch(src, dst):
        seen["src_dir"] = os.path.dirname(os.path.abspath(src))
        seen["dst_dir"] = os.path.dirname(os.path.abspath(dst))
        return real(src, dst)

    monkeypatch.setattr(os, "replace", watch)
    save_library(lib, {"items": [], "palettes": []})
    assert seen["src_dir"] == seen["dst_dir"]


# -- the lost update, which is the point --------------------------------------

def test_concurrent_writers_do_not_lose_each_others_work(lib):
    """Each thread adds one item. All of them must be there at the end.

    Without the lock this drops items: every thread loads the same list,
    appends to its own copy, and the last save wins. It is timing-dependent,
    so the loop is wide enough to lose one reliably rather than occasionally.
    """
    workers, per_worker = 8, 25
    errors = []

    def add(worker):
        try:
            for n in range(per_worker):
                with library_lock(lib):
                    data = load_library(lib)
                    data["items"].append({"id": f"{worker}-{n}"})
                    save_library(lib, data)
        except Exception as e:      # surfaced below rather than swallowed
            errors.append(e)

    threads = [threading.Thread(target=add, args=(w,)) for w in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    ids = [i["id"] for i in read_raw(lib)["items"]]
    assert len(ids) == workers * per_worker
    assert len(set(ids)) == len(ids), "no item written twice"


def test_without_the_lock_the_same_loop_loses_writes(lib):
    """The control. If this ever passes, the test above proves nothing."""
    workers, per_worker = 8, 25

    def add(worker):
        for n in range(per_worker):
            data = load_library(lib)
            data["items"].append({"id": f"{worker}-{n}"})
            save_library(lib, data)

    threads = [threading.Thread(target=add, args=(w,)) for w in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(read_raw(lib)["items"]) < workers * per_worker


def test_a_reader_never_sees_a_partial_database(lib):
    """Atomic replace means a concurrent read is old or new, never neither."""
    stop = threading.Event()
    failures = []

    def churn():
        n = 0
        try:
            while not stop.is_set():
                with library_lock(lib):
                    save_library(lib, {"items": [{"id": str(i)}
                                                 for i in range(n % 60)],
                                       "palettes": []})
                n += 1
        except Exception as e:
            # On Windows a reader's open handle denies the rename outright.
            # An escaping exception here is a real failure, not a warning.
            failures.append(e)

    def read():
        while not stop.is_set():
            try:
                load_library(lib)          # raises if it ever sees a fragment
            except Exception as e:
                failures.append(e)
                return

    writer = threading.Thread(target=churn)
    readers = [threading.Thread(target=read) for _ in range(3)]
    writer.start()
    for r in readers:
        r.start()
    threading.Event().wait(1.5)
    stop.set()
    writer.join()
    for r in readers:
        r.join()

    assert failures == []


# -- shape of the lock itself -------------------------------------------------

def test_the_lock_is_reentrant_within_a_thread(lib):
    """A helper that locks gets called from a caller that already has."""
    with library_lock(lib):
        with library_lock(lib):
            save_library(lib, {"items": [], "palettes": []})
    assert read_raw(lib)["items"] == []


def test_two_names_for_one_library_are_one_lock(lib):
    """Otherwise a trailing slash or a different case buys two locks and none."""
    from palette_app.library import _lock_for, _lock_key

    assert _lock_for(_lock_key(lib)) is _lock_for(_lock_key(str(lib) + os.sep))


def test_the_lock_is_released_when_the_body_raises(lib):
    class Boom(Exception):
        pass

    with pytest.raises(Boom):
        with library_lock(lib):
            raise Boom()

    with library_lock(lib):        # would hang or fail if still held
        save_library(lib, {"items": [], "palettes": []})


def test_an_unwritable_root_still_lets_the_library_be_read(tmp_path,
                                                           monkeypatch):
    """A lock file that cannot be made must not stop the app working."""
    from palette_app import library as lib_mod
    from palette_app.library import create_library

    create_library(tmp_path)

    def no_open(*a, **k):
        raise OSError("read-only file system")

    monkeypatch.setattr(lib_mod.os, "open", no_open)
    with library_lock(tmp_path):
        assert load_library(tmp_path)["items"] == []


def test_reading_the_library_writes_nothing(lib):
    """A read path must not create the lock file, or anything else.

    Making readers take the full lock would have been the simpler fix for the
    Windows collision, and it would have meant a GET creating a file in
    someone's library. The in-process lock plus a retry gets the same safety
    without the side effect.
    """
    lock_file = lib / ".library.lock"
    if lock_file.exists():
        lock_file.unlink()
    before = sorted(p.name for p in lib.iterdir())

    load_library(lib)
    load_library(lib)

    assert sorted(p.name for p in lib.iterdir()) == before


def test_a_writer_does_take_the_cross_process_lock(lib):
    """The other half: writes are guarded against a second process."""
    with library_lock(lib):
        save_library(lib, {"items": [], "palettes": []})
    assert (lib / ".library.lock").exists()
