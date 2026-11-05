"""The Keychain interface, and what it refuses to hand to ``security``.

Unlike ``test_kahala_sync.py`` — which fakes :mod:`secretstore` wholesale so no
test touches a developer's real Keychain — this exercises the module itself with
``security`` replaced by a recorder. What is being tested is the boundary between
a value we were given and a command interpreter: ``security -i`` reads **one
command per line**, and the refresh token is minted by the sign-in server, which
makes it the one value here that comes from off the machine.

The fixtures pin the backend to the Keychain rather than letting the
platform choose it: this file is about ``security``'s command parser, and
it has to go on meaning that when the suite runs on a Windows runner.
``test_wincreds.py`` is the other half.
"""
from __future__ import annotations

import subprocess

import pytest

from jamgraf import secretstore

ACCOUNT = "kahala:https://kc.example/realms/good-place"


@pytest.fixture
def security(monkeypatch):
    """A ``security`` that records what it was asked and runs nothing."""
    calls: list[dict] = []

    class Done:
        returncode = 0
        stdout = "recorded"

    def run(argv, **kwargs):
        calls.append({"argv": list(argv), "input": kwargs.get("input", "")})
        return Done()

    monkeypatch.setattr(secretstore, "_windows", lambda: False)
    monkeypatch.setattr(secretstore, "_security", lambda: "/usr/bin/security")
    monkeypatch.setattr(subprocess, "run", run)
    return calls


def test_a_secret_carrying_a_newline_never_reaches_security(security):
    """The line after a newline would be read as the *next* security command.

    Quoting cannot save this: a newline ends the command whatever it sits inside,
    so the value has to be refused instead. Removing the `_sendable` check in
    `set_secret` makes this go red with `delete-generic-password` sitting on its
    own line of the script that gets written to `security -i`'s stdin.
    """
    hostile = "good-looking-token\ndelete-generic-password -s Jamgraf"
    assert secretstore.set_secret(ACCOUNT, hostile) is False
    assert security == [], \
        f"a token containing a newline was handed to security anyway: {security}"


@pytest.mark.parametrize("value", ["tok\rmore", "tok\x00more", "tok\x7fmore",
                                   "tok\nmore"])
def test_no_control_character_is_stored_or_looked_up(security, value):
    assert secretstore.set_secret(ACCOUNT, value) is False
    assert secretstore.get_secret(value) is None
    assert secretstore.delete_secret(value) is False
    assert security == []


def test_an_ordinary_token_is_stored_on_stdin_and_never_in_argv(security):
    """`ps` shows argv to every process running as this user."""
    token = "eyJhbGciOiJSUzI1NiJ9.payload-with_url.safe-chars"
    assert secretstore.set_secret(ACCOUNT, token) is True
    (call,) = security
    assert call["argv"][1:] == ["-i"]
    assert token not in " ".join(call["argv"]), "the token was visible in argv"
    assert token in call["input"]
    assert ACCOUNT in call["input"]


def test_a_token_with_a_space_or_a_quote_stays_one_word(security):
    """`security -i` splits on whitespace and honours double quotes."""
    assert secretstore.set_secret(ACCOUNT, 'has a space and a " quote') is True
    script = security[0]["input"]
    assert '-w "has a space and a \\" quote"' in script, script
    assert script.count("\n") == 1, "the script is more than one command"


def test_a_missing_security_tool_is_reported_not_worked_around(monkeypatch):
    monkeypatch.setattr(secretstore, "_windows", lambda: False)
    monkeypatch.setattr(secretstore, "_security", lambda: None)
    assert secretstore.available() is False
    assert "Keychain" in secretstore.unavailable_reason()
    assert secretstore.set_secret(ACCOUNT, "token") is False
    assert secretstore.get_secret(ACCOUNT) is None
