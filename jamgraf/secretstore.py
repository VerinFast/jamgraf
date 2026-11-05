"""Secrets that must not live in a wiki file — currently the Kahala refresh token.

Everything else Jamgraf remembers goes in one of two places: per-wiki rows in the
wiki's own SQLite file, or ``app_config.json`` beside it. Neither can hold a
credential:

* the wiki file **is** the export. "Save wiki" copies that database, and
  ``store.export_wiki_bundle`` reads from it — so a settings row containing a
  refresh token would be handed to whoever you shared a wiki with.
* ``app_config.json`` is plain JSON in the data directory. It never leaves the
  machine on its own, but it is world-readable-to-you plaintext that lands in
  Time Machine and any folder backup.

So credentials go to the operating system's own credential store instead:
the macOS Keychain, via the ``security`` tool, and the Windows Credential
Manager, via :mod:`wincreds`. Both are reached through here, because what
must be true of a stored credential is a property of this module and not of
either platform.

What this actually buys, stated honestly
----------------------------------------
The Keychain protects the token **at rest and in transit off the machine**: it
is not in a file you can accidentally export, mail, or commit, and a stolen
backup of the data directory contains nothing to sign in with.

It does **not** protect against code already running as you. A same-user process
can shell out to ``security`` exactly as this module does and read the item back
without a prompt. That is the same trust boundary the rest of the app already
sits on — ``auth.py`` grants loopback callers owner rights — so this is not a new
exposure, and it would be dishonest to describe it as one. Don't write docs that
imply more.

The secret never appears in ``argv``. ``security -i`` reads its commands from
stdin, so the token is not visible in ``ps`` output while the write happens.
That stdin is a *command stream*, though -- one command per line -- so a value
carrying a newline would end the write command and have its remainder read as
the next ``security`` command. The refresh token is minted by the sign-in server,
which makes it the one value here that arrives from off the machine, so values
are checked (:func:`_sendable`) rather than escaped: no quoting makes a newline
part of a word in that parser, and a credential that can't be stored safely must
fail loudly rather than creatively.

No silent fallback
------------------
:func:`available` reports whether there is a real secure store here. When there
isn't — a future Linux build, or a Mac where ``security`` is missing —
callers must refuse to sign in and say so. Writing the token to a file "just for
now" is exactly the outcome this module exists to prevent, so there is no file
backend to fall back to, not even a hidden one.
"""
from __future__ import annotations

import shutil
import subprocess
import sys

from . import wincreds
import sys

SERVICE = "Jamgraf"
# Where a sign-in made before the rename is stored. Read once, moved under SERVICE,
# then deleted (`get_secret`); deleted again at sign-out (`delete_secret`), since
# an old item left behind would be found and resurrected by the next read.
LEGACY_SERVICE = "Waikiki"
"""Keychain service name. Every item we own carries it, so a user can find and
delete them all in Keychain Access without guessing."""

_TIMEOUT = 10


def _security() -> str | None:
    return shutil.which("security")


def _windows() -> bool:
    """Whether this machine's store is the Windows Credential Manager.

    Decided by platform, never by probing for a tool: a Mac with no ``security``
    has no secure store and must say so, rather than quietly reaching for
    another one. Tests that exercise the ``security`` boundary pin this.
    """
    return sys.platform == "win32"


def store_name() -> str:
    """What to call this machine's credential store, in a sentence to a person.

    Exists because the sign-in path reports failures by name. "Couldn't save
    your sign-in to the Keychain" on a Windows machine is a message about
    somebody else's computer: it names a thing the reader does not have, and
    hides which store actually refused.
    """
    return wincreds.NAME if _windows() else "the macOS Keychain"


def available() -> bool:
    """True when this machine has a secure store we can use."""
    if _windows():
        return wincreds.available()
    return _security() is not None


def unavailable_reason() -> str:
    """Why :func:`available` is False, in words a person can act on."""
    if available():
        return ""
    if _windows():
        return wincreds.unavailable_reason()
    return ("This build has no secure place to keep a sign-in token. Jamgraf "
            "uses the macOS Keychain, and the `security` tool it needs isn't "
            "here.")


def _sendable(value: str) -> bool:
    """Whether ``value`` can cross into ``security`` unchanged.

    Control characters are refused rather than escaped. A newline is the one that
    matters -- ``security -i`` reads one command per line, so anything after it
    in a value would be parsed as another ``security`` command -- but none of
    them can legitimately appear in what we store: the account is a Keycloak
    issuer URL and the secret is an OAuth refresh token.

    The check is not conditional on the platform. ``CredWriteW`` has no command
    parser to confuse, but a value that could be stored on one machine and
    refused on another would be a sign-in that works on a Mac and fails on
    Windows for a reason nobody can see.

    The ``find``/``delete`` paths pass their arguments as a list, where no
    splitting happens and nothing could be injected in the first place; they
    check too, so that "what may be stored" and "what may be looked up" cannot
    drift apart into an item that can be written and never read back.
    """
    return bool(value) and not any(ch < " " or ch == "\x7f" for ch in value)


def set_secret(account: str, secret: str) -> bool:
    """Store (or replace) ``secret`` under ``account``. True when it landed.

    ``-U`` updates an existing item rather than erroring, so re-signing in
    replaces the old token instead of accumulating duplicates.
    """
    return _set(SERVICE, account, secret)


def _set(service: str, account: str, secret: str) -> bool:
    if not account or not secret:
        return False
    if not _sendable(account) or not _sendable(secret):
        return False
    if _windows():
        return wincreds.set(service, account, secret)
    return _keychain_set(service, account, secret)


def _keychain_set(service: str, account: str, secret: str) -> bool:
    sec = _security()
    if not sec:
        return False
    # Interactive mode: the command (and so the secret) arrives on stdin, never
    # in argv where `ps` would show it to every process running as this user.
    script = (f'add-generic-password -U -s {_q(service)} -a {_q(account)} '
              f'-w {_q(secret)}\n')
    try:
        done = subprocess.run([sec, "-i"], input=script, text=True,
                              capture_output=True, timeout=_TIMEOUT)
    except Exception:
        return False
    return done.returncode == 0


def get_secret(account: str) -> str | None:
    """The secret stored under ``account``, or None if there isn't one.

    A sign-in stored before the rename lives under the old service. It is moved
    the first time it is asked for: written under the new service, **read back**,
    and only then deleted from the old one. Any step failing leaves the old item
    exactly where it was, so the worst a failed move costs is doing it again next
    time — never the sign-in. This copies the token; it does not rotate it, so
    the rule that a rotated token must land (rule 11) is not in play.
    """
    current = _get(SERVICE, account)
    if current is not None:
        return current
    old = _get(LEGACY_SERVICE, account)
    if old is None:
        return None
    if _set(SERVICE, account, old) and _get(SERVICE, account) == old:
        _delete(LEGACY_SERVICE, account)
    else:
        print("[jamgraf] could not move the Kahala sign-in to the new Keychain "
              "entry; still using the old one, and will try again next time",
              file=sys.stderr)
    return old


def _get(service: str, account: str) -> str | None:
    if not account or not _sendable(account):
        return None
    if _windows():
        return wincreds.get(service, account)
    return _keychain_get(service, account)


def _keychain_get(service: str, account: str) -> str | None:
    sec = _security()
    if not sec:
        return None
    try:
        done = subprocess.run(
            [sec, "find-generic-password", "-s", service, "-a", account, "-w"],
            text=True, capture_output=True, timeout=_TIMEOUT)
    except Exception:
        return None
    if done.returncode != 0:
        return None
    # `-w` prints the password and a newline; a trailing strip is safe because
    # we only ever store tokens, which never carry meaningful whitespace.
    return done.stdout.strip() or None


def delete_secret(account: str) -> bool:
    """Remove ``account``'s secret. True if something was removed.

    Under both services. Signing out has to mean signed out: an item left under
    the old service would be found by the next `get_secret` and moved back into
    place, resurrecting the session the person just ended.
    """
    removed_new = _delete(SERVICE, account)
    removed_old = _delete(LEGACY_SERVICE, account)
    return removed_new or removed_old


def _delete(service: str, account: str) -> bool:
    if not account or not _sendable(account):
        return False
    if _windows():
        return wincreds.delete(service, account)
    return _keychain_delete(service, account)


def _keychain_delete(service: str, account: str) -> bool:
    sec = _security()
    if not sec:
        return False
    try:
        done = subprocess.run(
            [sec, "delete-generic-password", "-s", service, "-a", account],
            text=True, capture_output=True, timeout=_TIMEOUT)
    except Exception:
        return False
    return done.returncode == 0


def _q(value: str) -> str:
    r"""Quote one argument for ``security -i``'s own command parser.

    This is not a shell -- no subshell runs -- but ``-i`` does split its input
    into words and honours double quotes, so a token containing a space or a
    quote would otherwise be truncated or mis-parsed. Escape the two characters
    that mean something inside a quoted word (``\`` and ``"``) and wrap.

    Quoting is not enough on its own: a newline ends the *command*, whatever it
    is quoted inside. :func:`_sendable` refuses those before this is reached.
    """
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'
