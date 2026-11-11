r"""Resolve the user's real shell PATH for spawning local CLIs.

A macOS .app launched from Finder inherits a minimal PATH, so CLIs installed via
homebrew, npm-global, nvm, volta, etc. aren't found by `shutil.which` even though
they work in a terminal. We recover the login shell's PATH once and reuse it
wherever we look up or spawn a CLI (claude, gemini, agy).

**The separator is the platform's, not a colon.** This module existed only for
macOS, where splitting and joining on ``":"`` is right. On Windows PATH is
``;``-separated *and* every entry contains a colon (``C:\Program Files\...``),
so a colon split turned each directory into two nonexistent fragments and the
colon join produced one string ``shutil.which`` read as a single path. The
effect was not a degraded search but a total one: `which` answered None for
everything, so on Windows every remedy button became manual instructions and
every post-install check reported that the tool it had just installed was
missing. There is also no login shell to interrogate there, so that probe is
skipped rather than failed.
"""
from __future__ import annotations

import functools
import os
import shutil
import subprocess

# Common install locations, added even if the login-shell probe fails. POSIX
# only: the Windows equivalents are already on PATH, put there by the installers
# themselves, and a hand-written list of Program Files guesses would be the kind
# of invention rule 9 is about.
_EXTRA = [
    "/opt/homebrew/bin", "/opt/homebrew/sbin", "/usr/local/bin", "/usr/bin", "/bin",
    "~/.local/bin", "~/bin", "~/.claude/local", "~/.npm-global/bin", "~/.bun/bin",
    "~/.deno/bin", "~/.volta/bin", "~/.cargo/bin",
]


@functools.lru_cache(maxsize=1)
def augmented_path() -> str:
    sep = os.pathsep
    parts: list[str] = []
    if os.name != "nt":
        shell = os.environ.get("SHELL", "/bin/zsh")
        try:
            out = subprocess.run([shell, "-lic", "echo $PATH"],
                                 capture_output=True, text=True, timeout=6)
            if out.returncode == 0 and out.stdout.strip():
                parts += out.stdout.strip().split(sep)
        except Exception:
            pass
    parts += os.environ.get("PATH", "").split(sep)
    if os.name != "nt":
        parts += [os.path.expanduser(p) for p in _EXTRA]
    seen: set[str] = set()
    ordered = [p for p in parts if p and not (p in seen or seen.add(p))]
    return sep.join(ordered)


def which(cmd: str) -> str | None:
    """Resolve a CLI, honoring absolute paths and the augmented PATH."""
    if not cmd:
        return None
    if os.path.isabs(cmd):
        return cmd if os.access(cmd, os.X_OK) else None
    return shutil.which(cmd, path=augmented_path())


def env() -> dict:
    """os.environ with PATH augmented, for spawning CLIs (and their children)."""
    e = dict(os.environ)
    e["PATH"] = augmented_path()
    return e
