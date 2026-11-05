"""The Windows credential path, and the promises it must keep off Windows too.

:mod:`jamgraf.wincreds` is the Windows half of :mod:`jamgraf.secretstore`, so
these tests are the Windows counterpart of ``test_secretstore.py``. The division
of what they can prove is deliberate and worth stating: everything that is a
*decision* --- the target name, the encoding, the length limit, whether the
credential roams to the user's other machines --- is made above
``wincreds._load`` and is tested here on any platform. The struct layout and
pointer marshalling below that seam can only be exercised on Windows, which is
what the ``windows-latest`` job in ``test.yml`` is for; nothing here pretends to
cover it.
"""
from __future__ import annotations

import sys

import pytest

from jamgraf import secretstore, wincreds

ACCOUNT = "kahala:https://kc.example/realms/good-place"
TOKEN = "eyJhbGciOiJSUzI1NiJ9.a-refresh-token.signature"


class FakeCredentialManager:
    """A Credential Manager that records what crossed the ctypes seam."""

    def __init__(self):
        self.items: dict[str, dict] = {}
        self.write_error = 0
        self.read_error = 0

    def write(self, target, blob, username, persist):
        if self.write_error:
            return False, self.write_error
        self.items[target] = {"blob": blob, "username": username,
                              "persist": persist}
        return True, 0

    def read(self, target):
        if self.read_error:
            return None, self.read_error
        item = self.items.get(target)
        if item is None:
            return None, wincreds.ERROR_NOT_FOUND
        return item["blob"], 0

    def delete(self, target):
        if self.items.pop(target, None) is None:
            return False, wincreds.ERROR_NOT_FOUND
        return True, 0


@pytest.fixture
def creds(monkeypatch):
    """Windows, with ``advapi32`` replaced by a recorder."""
    fake = FakeCredentialManager()
    monkeypatch.setattr(wincreds, "_load", lambda: fake)
    wincreds._reset_for_tests()
    monkeypatch.setattr(secretstore, "_windows", lambda: True)
    yield fake
    wincreds._reset_for_tests()


def test_a_token_round_trips_through_the_credential_manager(creds):
    assert secretstore.set_secret(ACCOUNT, TOKEN) is True
    assert secretstore.get_secret(ACCOUNT) == TOKEN
    assert secretstore.delete_secret(ACCOUNT) is True
    assert secretstore.get_secret(ACCOUNT) is None


def test_the_credential_never_roams_to_the_users_other_machines(creds):
    """``CRED_PERSIST_ENTERPRISE`` would copy this machine's refresh token to
    every machine the user signs in to. Rule 11 keeps a credential from
    travelling; roaming persistence is that failure with a Win32 constant on it.
    Setting ``persist`` to 3 in ``wincreds.set`` makes this go red.
    """
    secretstore.set_secret(ACCOUNT, TOKEN)

    (item,) = creds.items.values()
    assert item["persist"] == wincreds.CRED_PERSIST_LOCAL_MACHINE
    assert item["persist"] != 3, "the token roams to the user's other machines"


def test_the_item_is_findable_by_a_person_in_credential_manager(creds):
    """Carrying the service in the target is how someone deletes what we own."""
    secretstore.set_secret(ACCOUNT, TOKEN)

    (target,) = creds.items
    assert target.startswith(f"{secretstore.SERVICE}:")
    assert ACCOUNT in target


def test_the_blob_is_utf8_so_the_size_limit_is_characters_not_half(creds):
    """UTF-16 is the convention other credential tools use and it was the first
    thing written here, but it doubles every character of an ASCII token: the
    2,560-byte store became 1,280 characters, and a real Keycloak refresh token
    can exceed that. Nothing but Jamgraf reads this item, so the encoding is
    ours to pick, and UTF-8 is the one that does not halve the capacity.
    """
    secretstore.set_secret(ACCOUNT, TOKEN)

    (item,) = creds.items.values()
    assert item["blob"] == TOKEN.encode("utf-8")
    assert secretstore.get_secret(ACCOUNT) == TOKEN


def test_a_long_but_storable_refresh_token_is_not_refused(creds):
    """The regression this encoding change is about. A 1,300-character token
    fits the Credential Manager twice over; under UTF-16 it was refused here and
    the user was told their sign-in could not be saved, for no reason they could
    act on. Encoding `secret.encode("utf-16-le")` in `wincreds.set` makes this
    go red.
    """
    token = "r" * 1300
    assert len(token) > wincreds.MAX_BLOB // 2

    assert secretstore.set_secret(ACCOUNT, token) is True
    assert secretstore.get_secret(ACCOUNT) == token


@pytest.mark.parametrize("value", ["tok\nmore", "tok\rmore", "tok\x00more",
                                   "tok\x7fmore"])
def test_no_control_character_is_stored_on_windows_either(creds, value):
    """The same values are refusable on both platforms, on purpose.

    There is no command parser to confuse here, so this guard is not protecting
    ``CredWriteW``; it is keeping "what may be stored" from drifting apart
    between a Mac and a Windows machine. Dropping the `_sendable` checks from
    `secretstore._set` makes this go red.
    """
    assert secretstore.set_secret(ACCOUNT, value) is False
    assert creds.items == {}


def test_a_credential_too_large_to_store_is_refused_with_a_reason(creds, capsys):
    """Rather than failing inside CredWriteW with a number nobody can act on."""
    huge = "x" * (wincreds.MAX_BLOB + 1)

    assert secretstore.set_secret(ACCOUNT, huge) is False

    assert creds.items == {}
    assert "at most" in capsys.readouterr().err


def test_a_failed_write_is_reported_not_swallowed(creds, capsys):
    """A rotated refresh token that does not land ends the sign-in (rule 11),
    so the caller has to be told the truth about the write."""
    creds.write_error = 5    # ERROR_ACCESS_DENIED

    assert secretstore.set_secret(ACCOUNT, TOKEN) is False
    assert "Windows error 5" in capsys.readouterr().err


def test_nobody_signed_in_yet_is_quiet_but_a_real_failure_is_not(creds, capsys):
    assert secretstore.get_secret(ACCOUNT) is None
    assert capsys.readouterr().err == ""

    creds.read_error = 1312    # ERROR_NO_SUCH_LOGON_SESSION
    assert secretstore.get_secret(ACCOUNT) is None
    assert "Windows error 1312" in capsys.readouterr().err


def test_a_credential_written_by_something_else_is_left_alone(creds, capsys):
    creds.items[f"{secretstore.SERVICE}:{ACCOUNT}"] = {
        "blob": b"\xff\xfe\x00", "username": ACCOUNT, "persist": 2}

    assert secretstore.get_secret(ACCOUNT) is None
    assert "not readable as text" in capsys.readouterr().err


def test_without_advapi32_there_is_no_file_to_fall_back_to(monkeypatch):
    """The whole point of the module next door: no secure store, no sign-in."""
    monkeypatch.setattr(wincreds, "_load", lambda: None)
    wincreds._reset_for_tests()
    monkeypatch.setattr(secretstore, "_windows", lambda: True)
    try:
        assert secretstore.available() is False
        assert "Credential Manager" in secretstore.unavailable_reason()
        assert secretstore.set_secret(ACCOUNT, TOKEN) is False
        assert secretstore.get_secret(ACCOUNT) is None
    finally:
        wincreds._reset_for_tests()


# --- On Windows only: the part no Mac can check -------------------------------

@pytest.mark.skipif(sys.platform != "win32", reason="needs the real advapi32")
def test_a_real_credential_round_trips_through_advapi32():
    """The ctypes seam itself: struct layout, blob pointer, CredFree.

    Everything above ``wincreds._load`` is tested with a fake on every platform.
    This is the only test that runs the marshalling, which is why it exists and
    why it is skipped everywhere else. If the credential store is unreachable on
    this runner, say so and skip rather than fail: that is an environment fact,
    not a defect in the code under test.
    """
    account = "jamgraf-selftest:round-trip"
    token = "a-token-with a space and a \" quote"
    if not wincreds.available():
        pytest.skip("no Credential Manager on this runner")

    try:
        assert wincreds.set(secretstore.SERVICE, account, token) is True, \
            "CredWriteW refused a credential this user should be able to write"
        assert wincreds.get(secretstore.SERVICE, account) == token
    finally:
        wincreds.delete(secretstore.SERVICE, account)

    assert wincreds.get(secretstore.SERVICE, account) is None


@pytest.mark.skipif(sys.platform != "win32", reason="needs the real registry")
def test_the_webview2_probe_answers_without_raising():
    """The registry walk. A runner may or may not have the runtime installed, so
    what is asserted is the contract -- a version string or None, never an
    exception out of a probe -- not which answer this machine gives.
    """
    from jamgraf.desktop import windows as win_shell

    version = win_shell.webview2_version()
    assert version is None or (isinstance(version, str) and version[0].isdigit())
    problem = win_shell.runtime_problem()
    assert (problem is None) == (version is not None)


def test_the_refusal_says_why_even_where_the_store_itself_works(creds, monkeypatch):
    """``secretstore`` decides *whether* there is a usable store; this module
    only has to say *why* there isn't when it has already decided there isn't.

    Re-probing here was wrong in the one place it mattered: on a real Windows
    machine advapi32 always answers, so the second probe said "the store works"
    and returned the empty string — and the sign-in page refused with no reason
    printed at all. Restoring `if available(): return ""` to
    `wincreds.unavailable_reason` makes this go red.
    """
    monkeypatch.setattr(secretstore, "available", lambda: False)

    why = secretstore.unavailable_reason()

    assert why, "refused the sign-in without saying why"
    assert "Credential Manager" in why
