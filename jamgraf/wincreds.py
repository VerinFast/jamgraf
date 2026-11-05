"""The Windows Credential Manager, reached through ``advapi32``.

The Windows half of :mod:`secretstore`. That module explains *why* a credential
may not live in a wiki file or in ``app_config.json``; this one is only the
store those rules point at on Windows, and it exists so that "no secure store,
no sign-in" stays a statement about a platform we have not reached rather than
about Windows (rule 11).

What this actually buys, stated honestly
----------------------------------------
Same shape as the Keychain note next door, and worth repeating because the
temptation to oversell a credential store is identical on both platforms. A
generic credential is encrypted at rest under the user's DPAPI keys, so it is
**not** in a file you can accidentally export, mail, commit, or restore from a
copied data directory — which is the exposure a refresh token in
``app_config.json`` would actually have.

It does **not** protect against code already running as you. ``CredRead``
returns the blob to any process in this user's session without a prompt,
exactly as this module calls it. That is the same trust boundary the rest of the
app sits on (``auth.py`` grants loopback callers owner rights), so it is not a
new exposure — and documentation that implies a prompt or a second factor here
would be false.

Persistence is deliberately ``CRED_PERSIST_LOCAL_MACHINE``, not
``CRED_PERSIST_ENTERPRISE``. Enterprise credentials roam with the user's profile
to every machine they sign in to, which would hand this machine's Kahala
refresh token to every other one — the same "a credential must not travel"
failure rule 11 keeps the link record out of the wiki file to avoid.

Why the Win32 call sits behind ``_load``
----------------------------------------
``ctypes.wintypes`` cannot even be imported off Windows, so every Win32 symbol
is built lazily inside :func:`_load` and cached. That keeps this module
importable on a Mac (``secretstore`` imports it unconditionally) and, just as
usefully, puts the one part that cannot be exercised anywhere but Windows —
struct layout and pointer marshalling — behind a seam a test can replace. The
logic above the seam is tested on every platform; the marshalling below it is
covered by the Windows CI job and nothing else, which is the honest division.
"""
from __future__ import annotations

import sys

NAME = "the Windows Credential Manager"

CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2
ERROR_NOT_FOUND = 1168

MAX_BLOB = 2560
"""``CRED_MAX_CREDENTIAL_BLOB_SIZE``, in bytes.

The blob is written as **UTF-8**, which is what makes that limit 2560 characters
of token rather than 1280. UTF-16 is the convention other credential tools use,
and it was the first thing written here, but it doubles every character of an
ASCII token: a Keycloak refresh token of 1,300 characters would have been
refused by this module while fitting the store twice over, and the user would
have been told their sign-in could not be saved for no reason they could act on.
Nothing but Jamgraf reads this item, so the encoding is ours to choose.

A value that genuinely does not fit is still refused here with a reason, rather
than failing inside ``CredWriteW`` with an error code nobody can act on."""

_API: object | None = None
_LOADED = False


def _load():
    """Bind ``CredWriteW``/``CredReadW``/``CredDeleteW``, or None off Windows.

    Returns an object with ``write(target, blob, username, persist)``,
    ``read(target)`` and ``delete(target)``, each returning
    ``(result, error_code)`` and raising nothing: a Win32 failure is a number we
    report, not an exception to chase. Everything that is a *decision* --- which
    target, what encoding, how long the credential may be, whether it roams ---
    stays above this seam, where a test can see it on any platform.
    """
    if sys.platform != "win32":
        return None

    import ctypes
    from ctypes import wintypes

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)

    class FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", wintypes.DWORD),
                    ("dwHighDateTime", wintypes.DWORD)]

    class CREDENTIALW(ctypes.Structure):
        _fields_ = [("Flags", wintypes.DWORD),
                    ("Type", wintypes.DWORD),
                    ("TargetName", wintypes.LPWSTR),
                    ("Comment", wintypes.LPWSTR),
                    ("LastWritten", FILETIME),
                    ("CredentialBlobSize", wintypes.DWORD),
                    ("CredentialBlob", ctypes.POINTER(ctypes.c_char)),
                    ("Persist", wintypes.DWORD),
                    ("AttributeCount", wintypes.DWORD),
                    ("Attributes", ctypes.c_void_p),
                    ("TargetAlias", wintypes.LPWSTR),
                    ("UserName", wintypes.LPWSTR)]

    advapi.CredWriteW.argtypes = [ctypes.POINTER(CREDENTIALW), wintypes.DWORD]
    advapi.CredWriteW.restype = wintypes.BOOL
    advapi.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                                 wintypes.DWORD,
                                 ctypes.POINTER(ctypes.POINTER(CREDENTIALW))]
    advapi.CredReadW.restype = wintypes.BOOL
    advapi.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                                   wintypes.DWORD]
    advapi.CredDeleteW.restype = wintypes.BOOL
    advapi.CredFree.argtypes = [ctypes.c_void_p]
    advapi.CredFree.restype = None

    class _Api:
        def write(self, target, blob, username, persist):
            cred = CREDENTIALW()
            cred.Type = CRED_TYPE_GENERIC
            cred.TargetName = target
            cred.CredentialBlobSize = len(blob)
            cred.CredentialBlob = ctypes.cast(
                ctypes.create_string_buffer(blob, len(blob)),
                ctypes.POINTER(ctypes.c_char))
            cred.Persist = persist
            cred.UserName = username
            ok = advapi.CredWriteW(ctypes.byref(cred), 0)
            return bool(ok), (0 if ok else ctypes.get_last_error())

        def read(self, target):
            out = ctypes.POINTER(CREDENTIALW)()
            ok = advapi.CredReadW(target, CRED_TYPE_GENERIC, 0,
                                  ctypes.byref(out))
            if not ok:
                return None, ctypes.get_last_error()
            try:
                cred = out.contents
                size = int(cred.CredentialBlobSize)
                blob = ctypes.string_at(cred.CredentialBlob, size) if size else b""
            finally:
                # The blob belongs to the credential buffer, so it is copied out
                # (string_at copies) before this frees it.
                advapi.CredFree(out)
            return blob, 0

        def delete(self, target):
            ok = advapi.CredDeleteW(target, CRED_TYPE_GENERIC, 0)
            return bool(ok), (0 if ok else ctypes.get_last_error())

    return _Api()


def _api():
    global _API, _LOADED
    if not _LOADED:
        try:
            _API = _load()
        except Exception:
            # A Windows without advapi32 is not a thing we can fix, and an
            # exception out of here would take the app down at import time.
            _API = None
        _LOADED = True
    return _API


def _reset_for_tests() -> None:
    """Forget the cached API so a test's ``_load`` is the one that gets used."""
    global _API, _LOADED
    _API, _LOADED = None, False


def available() -> bool:
    return _api() is not None


def unavailable_reason() -> str:
    """Why there is no usable store here, for a caller that has decided there
    isn't one.

    Deliberately *not* gated on :func:`available`. ``secretstore`` already
    answers "is there a store" and only asks this when the answer was no --- and
    on a real Windows machine advapi32 is always reachable, so a second probe
    here returned the empty string and the sign-in page refused with no reason
    at all. The macOS branch next door has always worked this way; this one
    re-asked a question it had already been given the answer to.
    """
    return ("This build has no secure place to keep a sign-in token. Jamgraf "
            "uses the Windows Credential Manager, which this machine would not "
            "let it reach.")


def _target(service: str, account: str) -> str:
    """The Credential Manager key. One flat namespace, so the service is in it.

    Carrying the service name means a person can find every item Jamgraf owns
    in Credential Manager and delete them without guessing — the same reason the
    Keychain items carry a service.
    """
    return f"{service}:{account}"


def set(service: str, account: str, secret: str) -> bool:  # noqa: A001
    """Store (or replace) ``secret``. True when it landed.

    ``CredWriteW`` replaces an existing credential with the same target, so
    re-signing in overwrites rather than accumulating duplicates.
    """
    api = _api()
    if api is None:
        return False
    blob = secret.encode("utf-8")
    if len(blob) > MAX_BLOB:
        print(f"[jamgraf] refusing to store a {len(blob)}-byte credential: "
              f"the Credential Manager takes at most {MAX_BLOB}",
              file=sys.stderr)
        return False
    ok, err = api.write(_target(service, account), blob, account,
                        CRED_PERSIST_LOCAL_MACHINE)
    if not ok:
        print(f"[jamgraf] could not write the credential "
              f"(Windows error {err})", file=sys.stderr)
    return ok


def get(service: str, account: str) -> str | None:
    """The stored secret, or None when there is not one.

    A missing credential is the ordinary case (nobody has signed in yet), so
    ``ERROR_NOT_FOUND`` is silent; any other failure is reported, because it
    means a sign-in the person made is unreachable and they are about to be
    told they are signed out.
    """
    api = _api()
    if api is None:
        return None
    blob, err = api.read(_target(service, account))
    if blob is None:
        if err and err != ERROR_NOT_FOUND:
            print(f"[jamgraf] could not read the credential "
                  f"(Windows error {err})", file=sys.stderr)
        return None
    try:
        return blob.decode("utf-8") or None
    except UnicodeDecodeError:
        # Something else wrote this target. Say so: silently treating it as
        # "not signed in" would have us overwrite someone else's item.
        print("[jamgraf] the stored credential is not readable as text; "
              "leaving it alone", file=sys.stderr)
        return None


def delete(service: str, account: str) -> bool:
    """Remove the stored secret. True if something was removed.

    A failure that is not "there was nothing there" is **reported**, because of
    what sits on top of it: signing out calls this and then tells the person
    they are signed out. If the Credential Manager refused the delete, the
    refresh token is still on the machine and the next ``signed_in`` check finds
    it — so the one thing that must not happen is this failing quietly.
    """
    api = _api()
    if api is None:
        return False
    ok, err = api.delete(_target(service, account))
    if not ok and err and err != ERROR_NOT_FOUND:
        print(f"[jamgraf] could not remove the credential — it is still stored "
              f"(Windows error {err})", file=sys.stderr)
    return ok
