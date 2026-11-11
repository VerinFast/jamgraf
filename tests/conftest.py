"""Shared test fixtures.

Each test gets an isolated temp data dir (its own wiki registry + per-wiki DBs)
and a fast, deterministic fake embedder so nothing downloads a model or hits the
network. The active wiki defaults to "main".
"""
from __future__ import annotations

import contextlib
import hashlib
import sys
import threading
import time
from pathlib import Path

import pytest

from jamgraf import config, db, embeddings, updater, wikis


def _drain_worker_threads(timeout: float = 10.0) -> int:
    """Wait for anyio worker threads to finish. Returns how many were left.

    The app's lifespan does startup work — seeding the Help wiki, the one-time
    html migration — through ``anyio.to_thread.run_sync``. Cancelling those tasks
    when the lifespan exits does **not** stop the thread: instrumenting
    ``db.get_conn`` showed 175 calls arriving *after* a TestClient context
    exited, from a thread named "AnyIO worker thread". Awaiting the cancelled
    tasks does not help either, because the event loop that would wait for the
    thread is being torn down with it.

    In the app that is harmless — nothing swaps its data directory underneath it.
    Under test it is issue #10: those late calls resolve ``config.DATA_DIR`` and
    ``db._local`` while this file's fixtures are restoring them, which surfaces as
    ``apsw.BusyError`` or ``no such table: pages`` in whatever test runs next.

    So: let the work finish before the config moves. Measured at ~180ms for the
    first TestClient in a session and ~0ms thereafter.
    """
    deadline = time.monotonic() + timeout
    while True:
        alive = [t for t in threading.enumerate()
                 if t.is_alive() and "worker" in t.name.lower()]
        if not alive or time.monotonic() >= deadline:
            return len(alive)
        time.sleep(0.005)


@pytest.fixture(autouse=True)
def _quiesce_background_work(monkeypatch):
    """Drain lifespan background work before any fixture restores global config.

    Requesting ``monkeypatch`` is load-bearing: a fixture is finalized before the
    fixtures it depends on, so this runs *before* ``monkeypatch`` undoes the
    ``config.DATA_DIR`` / ``db._local`` patches — which is the whole point.
    """
    yield
    left = _drain_worker_threads()
    if left:
        # Don't fail the test for it, but don't hide it either: a stuck worker
        # means the next test is running against a moving target.
        print(f"\n[tests] {left} worker thread(s) still running at teardown")


class FakeEmbedder:
    provider = "fake"
    model = "fake"
    dim = 8

    def embed(self, texts):
        out = []
        for t in texts:
            h = hashlib.sha256(t.encode("utf-8")).digest()
            out.append([b / 255.0 for b in h[: self.dim]])
        return out


@pytest.fixture(autouse=True)
def _no_update_checks(monkeypatch):
    """Never let the test suite reach GitHub.

    The app's maintenance loop checks for updates, and it starts with every
    TestClient lifespan. It skips its first pass so a normal run doesn't fire a
    request during startup, but that is a timing property -- this makes it
    structural, so a future change to that loop can't quietly put a network call
    back into the suite.

    Patched at the HTTP boundary rather than at ``check()``: that blocks the
    network while leaving check()'s own logic (which release counts as
    installable) testable by overriding ``_get_json`` per test.
    """
    def _no_network(*a, **k):
        raise RuntimeError("network disabled in tests")

    monkeypatch.setattr(updater, "_get_json", _no_network)


@pytest.fixture(autouse=True)
def _no_doorman(monkeypatch):
    """The suite must never reach a real Doorman on the developer's machine.

    Jamgraf now asks Doorman whether it can answer generation, chat and images,
    so a developer who happens to have Doorman open would otherwise get a
    different test run from everyone else — and a live agent call from a unit
    test. Patched at the same boundary as the update check: the default is "not
    running", and a test that wants one overrides ``_get`` itself.

    Every cache is cleared on both sides, so probe results never leak between
    tests either.
    """
    from jamgraf import doorman

    def _absent(*a, **k):
        raise RuntimeError("no Doorman in tests")

    doorman.forget()
    monkeypatch.setattr(doorman, "_get", _absent)
    yield
    doorman.forget()


@pytest.fixture(autouse=True)
def _bare_machine(monkeypatch):
    """The suite never looks at what the developer happens to have installed.

    Capability probing runs on every page render now (the feature buttons are
    gated on it), so without this the same test would report Chat as ready on a
    machine with the ``claude`` CLI and unavailable on CI — the exact way a test
    in this repo already broke once. The default is a machine with nothing on
    it; a test that wants a tool present patches ``capabilities._which`` itself.

    It also keeps the suite from spawning a login shell to recover the user's
    PATH, which ``shellenv`` does on the first lookup in a process.
    """
    from jamgraf import capabilities

    capabilities.refresh()
    monkeypatch.setattr(capabilities, "_which", lambda name: None)
    monkeypatch.setattr(capabilities, "_reachable", lambda url, **k: False)
    yield
    capabilities.refresh()


@pytest.fixture
def wiki(tmp_path, monkeypatch):
    """Isolated temp data dir + fake embedder; active wiki = 'main'."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "WIKIS_DIR", tmp_path / "wikis")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "waikiki.db")  # legacy, absent
    monkeypatch.setattr(db, "_local", threading.local())
    monkeypatch.setattr(db, "_schema_ready", set())
    monkeypatch.setattr(embeddings, "get_embedder", lambda: FakeEmbedder())
    monkeypatch.setattr(embeddings, "active", lambda: ("fake", "fake"))

    wikis.ensure_initialized()  # creates main + Beaconlight/Crosslake/StartupOS
    token = db.current_wiki.set("main")
    db.init_db()
    try:
        yield
    finally:
        db.current_wiki.reset(token)


@pytest.fixture
def fake_live_http(monkeypatch):
    """Stand in for the web app that MCP page reads fetch live CRDT text from.

    ``get_page`` / ``read_pages`` pull unsaved live text over one shared
    ``httpx.Client``; patching that one seam keeps the suite off whatever dev
    server happens to be listening on the default port — without it a test reads
    a *different* database and reports a spurious ``live`` — and lets a test say
    what the live text is.

    Call the fixture with ``on_get(url) -> response`` for a room with unsaved
    text, or with no argument for "the web app isn't there".
    """
    from jamgraf import mcp_server

    def install(on_get=None):
        def offline(url):
            raise RuntimeError("offline: tests never call the real web app")

        handler = on_get or offline

        class _Client:
            def __init__(self, **kw):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def get(self, url, **kw):
                return handler(url)

        monkeypatch.setattr(mcp_server.httpx, "Client", _Client)

    return install


@pytest.fixture
def pretend_platform(monkeypatch):
    """Answer one module's ``sys.platform`` differently, and nothing else.

    Platform-dependent behaviour has to be testable from one machine, and the
    obvious way — ``monkeypatch.setattr(mod.sys, "platform", ...)`` — is a trap:
    ``mod.sys`` *is* the real :mod:`sys`, so that rewrites it for the whole
    interpreter. ``shutil.which`` then takes its Windows branch and reaches for
    ``_winapi``, which a Mac has not got, and tests fail for a reason that has
    nothing to do with what they were checking.

    This swaps the module's *reference* to sys for a stand-in that answers
    ``platform`` and delegates everything else, so the blast radius is the one
    module named.

        pretend_platform("win32", capabilities, remedies)
    """
    class _Sys:
        def __init__(self, platform):
            self.platform = platform

        def __getattr__(self, name):
            return getattr(sys, name)

    def pretend(platform: str, *modules):
        stand_in = _Sys(platform)
        for module in modules:
            monkeypatch.setattr(module, "sys", stand_in)
        return stand_in

    return pretend


# --- "this OS will not move an open file" ------------------------------------
#
# Windows refuses `os.rename` and `os.unlink` on a file any handle in any
# process still has open (`WinError 32`), while POSIX renames and unlinks the
# directory entry and lets the handle go on using the inode. That difference is
# why renaming a wiki worked on macOS and could not work on Windows at all, and
# why it stayed hidden: every `conn.close()` in `db.py` was raising
# `AttributeError` into a `suppress(Exception)`, the handles all leaked, and
# POSIX did not care.
#
# A leak POSIX does not care about is one macOS cannot test, so the fixture
# below makes macOS care: it applies the Windows rule to this process's own
# handles. It does not emulate Windows — it enforces the one property the fix
# has to have, which is that nothing of ours is still open when the file moves.

def _open_fd_on(path) -> bool:
    """Does this process still hold an fd on `path`? Compares (dev, inode).

    By inode rather than by name, because that is what the OS checks and what
    an fd actually refers to — a handle on `main.db` is a handle on that inode
    whatever the directory entry has since been called.
    """
    import os as _os

    try:
        want = _os.stat(path)
    except OSError:
        return False
    for entry in _os.listdir("/dev/fd"):
        try:
            got = _os.fstat(int(entry))
        except (OSError, ValueError):
            continue
        if (got.st_dev, got.st_ino) == (want.st_dev, want.st_ino):
            return True
    return False


@contextlib.contextmanager
def windows_file_rules():
    """Apply Windows's file rules for the duration of a block.

    Both halves matter. Flipping `db.OPEN_FILES_CAN_MOVE` alone picks the
    Windows branch but still runs on an OS that would move the file regardless,
    so a test would pass with the handles wide open. Refusing the syscall alone
    would fail whatever the code does. Together they are the Windows job, run on
    macOS.

    A context manager and not only a fixture, because a test may need the two
    platforms in turn — a rename that deliberately leaves a handle open the
    POSIX way, and then a delete that has to close it the Windows way.
    """
    import os as _os

    from jamgraf import db

    real = (db.OPEN_FILES_CAN_MOVE, _os.rename, _os.unlink, _os.remove)

    def refuse(verb, path):
        raise PermissionError(
            32, f"[windows_file_rules] cannot {verb} {Path(path).name}: this "
                f"process still holds an open handle on it")

    def guard(verb, fn):
        def wrapped(path, *a, **kw):
            if _open_fd_on(path):
                refuse(verb, path)
            return fn(path, *a, **kw)
        return wrapped

    db.OPEN_FILES_CAN_MOVE = False
    _os.rename = guard("rename", real[1])
    _os.unlink = guard("unlink", real[2])
    _os.remove = guard("remove", real[3])
    try:
        yield
    finally:
        db.OPEN_FILES_CAN_MOVE, _os.rename, _os.unlink, _os.remove = real


@pytest.fixture
def as_if_windows():
    """Windows's file rules for the whole test. See `windows_file_rules`."""
    import os as _os

    if _os.name != "posix":  # pragma: no cover - the /dev/fd check is POSIX-only
        pytest.skip("the /dev/fd check this leans on is POSIX-only")
    with windows_file_rules():
        yield
