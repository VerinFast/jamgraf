"""Resolving the user's PATH, on a platform whose separator is not a colon.

This module existed for one job — recover a login shell's PATH on macOS, where
a Finder-launched `.app` inherits almost none of it — and splitting on ``":"``
was simply correct there. On Windows it is wrong twice over: the separator is
``;``, and every entry *contains* a colon (``C:\\Windows``). The result was not a
degraded search but a total one, and the damage was downstream: every remedy
button in Settings became "manual instructions" because `which` could not find
the tool it was offering to install, and every post-install check reported that
the tool it had just installed was still missing.
"""
from __future__ import annotations

import os

import pytest

from jamgraf import shellenv


class _Os:
    """Stand-in for :mod:`os`, answering as Windows does."""

    name = "nt"
    pathsep = ";"

    def __init__(self, path: str):
        self.environ = {"PATH": path}

    def __getattr__(self, attr):
        return getattr(os, attr)


WINDOWS_PATH = (r"C:\Windows\system32;C:\Program Files\nodejs;"
                r"C:\Users\x\AppData\Local\Microsoft\WindowsApps")


@pytest.fixture
def windows(monkeypatch):
    monkeypatch.setattr(shellenv, "os", _Os(WINDOWS_PATH))
    shellenv.augmented_path.cache_clear()
    yield
    shellenv.augmented_path.cache_clear()


def test_a_windows_path_survives_being_read_and_rebuilt(windows):
    """Splitting on ":" turned `C:\\Windows\\system32` into `C` and
    `\\Windows\\system32`, neither of which exists, and joining with ":" handed
    `shutil.which` one string it read as a single directory. Setting `sep` back
    to ":" in `augmented_path` makes this go red.
    """
    built = shellenv.augmented_path()

    assert built.split(";") == WINDOWS_PATH.split(";")
    for entry in WINDOWS_PATH.split(";"):
        assert entry in built.split(";"), f"{entry} did not survive"


def test_no_login_shell_is_interrogated_on_windows(windows, monkeypatch):
    """There is no `$SHELL -lic` there, and the POSIX fallback directories are
    not Windows locations. Nothing is spawned."""
    def no_subprocess(*a, **k):
        raise AssertionError("a login shell was spawned on Windows")

    monkeypatch.setattr(shellenv.subprocess, "run", no_subprocess)

    built = shellenv.augmented_path()

    assert "/opt/homebrew/bin" not in built
    assert "/usr/local/bin" not in built


def test_the_posix_path_still_gets_its_extra_directories(monkeypatch):
    """The macOS behaviour this module was written for is unchanged: a
    Finder-launched app needs these appended even when the shell probe fails."""
    monkeypatch.setattr(shellenv.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("no shell")))
    shellenv.augmented_path.cache_clear()
    try:
        built = shellenv.augmented_path()
        assert "/opt/homebrew/bin" in built.split(os.pathsep)
        assert "/usr/local/bin" in built.split(os.pathsep)
    finally:
        shellenv.augmented_path.cache_clear()
